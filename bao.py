#!/usr/bin/env python3
"""바오 — Iris WebSocket 기반 "방별" AI 챗봇 (Qwen3.5 Plus / MuleRouter).

동작:
  카톡 메시지 ──(Iris)──▶ ws://PHONE/ws ──▶ 이 봇
  - 모든 메시지를 chat_id(방) 단위 deque 에 실시간 누적 (방의 최근 대화)
  - 본문이 "바오"로 시작하면 호출:
      ① /query 로 "호출한 사용자"의 이 방 최근 20개 조회 (호출 시점 신선)
      ② /ws 로 우리가 쌓은 "방 자체"의 최근 20개
      → 둘을 _id 로 중복 제거하고 시간순 정렬해 Qwen3.5 Plus 에 전달
  - 응답을 http://PHONE/reply 로 전송

컨텍스트 구성:
  - 방(chat_id)마다 독립 deque. user/assistant 역할 구분.
  - caller 히스토리는 매 호출마다 /query 로 신선하게(이 방 안에서 user_id 필터).
    *복호화는 행에 v/enc 가 있어야 동작 → SELECT * 로 행 전체를 받아 보장.*
  - LLM 블로킹 호출은 asyncio.to_thread 로 이벤트 루프 비차단.
  - 답장은 send_lock + SEND_RATE 로 직렬화 (도배/BAN 회피).

사용법:
  export MULEROUTER_API_KEY='발급받은_키'
  export IRIS_HOST='10.203.131.9:3000'   # 선택
  pip install -r requirements.txt
  python3 bao.py
"""
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque

import aiohttp
import websockets
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # .env 의 MULEROUTER_API_KEY / IRIS_HOST / BOT_ID 를 환경변수로 로드

PHONE = os.environ.get("IRIS_HOST", "10.203.131.9:3000")
WS_URL = f"ws://{PHONE}/ws"
REPLY = f"http://{PHONE}/reply"
QUERY = f"http://{PHONE}/query"

BOT_ID = os.environ.get("BOT_ID", "443097014")  # curl http://PHONE/config 의 bot_id
BOT_NAME = "바오"
TRIGGER = BOT_NAME                               # 이 단어로 시작하면 발동

MODEL = "qwen3.5-plus"   # MuleRouter 실제 모델 ID (문서의 qwen3-5-plus 는 오기)
ROOM_HISTORY = 20       # /ws 로 쌓는 방별 최근 메시지 수 (deque maxlen)
USER_HISTORY = 20       # /query 로 끌어올 caller 의 이 방 최근 메시지 수
SEND_RATE = 0.3         # 답장 간 최소 간격(초) — BAN 회피 직렬화

SYSTEM_PROMPT = (
    f"너는 '{BOT_NAME}'라는 이름의 카카오톡 채팅방 AI 비서야. "
    "한국어로 친근하고 간결하게 대답해. 카톡 메시지이므로 너무 길게 쓰지 말고 핵심만. "
    "대화 기록에서 'user' 메시지는 '이름: 내용' 형식이니 누가 말했는지 참고해."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bao")

client = OpenAI(
    api_key=os.environ.get("MULEROUTER_API_KEY"),
    base_url="https://api.mulerouter.ai/vendors/openai/v1",
)

# 방(chat_id) → 최근 대화 deque[{"_id","ts","role","content"}]
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROOM_HISTORY))
send_lock = asyncio.Lock()          # 답장 직렬화


def _ts(raw) -> float:
    """created_at(문자열 unix초)을 float 로. 없거나 깨지면 현재 시각."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return time.time()


def _rows(payload) -> list[dict]:
    """/query 응답 {"data":[...]} 에서 행 리스트 추출 (관용 처리)."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    if isinstance(payload, list):
        return payload
    return []


async def fetch_user_history(
    http: aiohttp.ClientSession, chat_id: str, user_id: str
) -> list[dict]:
    """호출한 사용자의 "이 방" 최근 메시지를 /query 로 신선하게 조회.

    SELECT * 로 행 전체를 받아 message 자동 복호화(v/enc 필요)를 보장한다.
    """
    sql = (
        "SELECT * FROM chat_logs "
        f"WHERE chat_id = ? AND user_id = ? ORDER BY _id DESC LIMIT {USER_HISTORY}"
    )
    try:
        async with http.post(QUERY, json={"query": sql, "bind": [chat_id, user_id]}) as resp:
            if resp.status != 200:
                log.warning("[%s] caller 히스토리 조회 실패 status=%s", chat_id, resp.status)
                return []
            payload = await resp.json()
    except Exception:
        log.exception("[%s] caller 히스토리 조회 오류", chat_id)
        return []
    return _rows(payload)


def build_context(room_buf: deque, caller_rows: list[dict], sender: str) -> list[dict]:
    """방 deque(/ws) + caller /query 행을 _id 중복 제거 후 시간순으로 병합."""
    entries: list[dict] = []
    seen: set[str] = set()

    for e in room_buf:  # 실시간 누적분
        entries.append(e)
        if e["_id"]:
            seen.add(e["_id"])

    for row in caller_rows:  # caller 의 /query 분 (방 deque 와 겹치면 스킵)
        rid = row.get("_id")
        if rid and rid in seen:
            continue
        text = (row.get("message") or "").strip()
        if not text:
            continue
        entries.append({
            "_id": rid,
            "ts": _ts(row.get("created_at")),
            "role": "assistant" if str(row.get("user_id")) == BOT_ID else "user",
            "content": text if str(row.get("user_id")) == BOT_ID else f"{sender}: {text}",
        })
        if rid:
            seen.add(rid)

    entries.sort(key=lambda e: e["ts"])
    return [{"role": e["role"], "content": e["content"]} for e in entries]


def complete(messages: list[dict]) -> str:
    """Qwen3.5 Plus 호출 (블로킹 — 스레드에서 실행)."""
    resp = client.chat.completions.create(model=MODEL, messages=messages)
    return (resp.choices[0].message.content or "").strip()


async def send_reply(http: aiohttp.ClientSession, room: str, text: str) -> None:
    async with send_lock:  # 답장 직렬화 + 간격 유지로 도배/BAN 회피
        try:
            async with http.post(
                REPLY, json={"type": "text", "room": str(room), "data": text}
            ) as resp:
                if resp.status != 200:
                    log.warning("reply 실패 status=%s body=%s", resp.status, await resp.text())
        except Exception:
            log.exception("reply POST 오류")
        await asyncio.sleep(SEND_RATE)


async def handle(http: aiohttp.ClientSession, event: dict) -> None:
    j = event.get("json", {})
    user_id = str(j.get("user_id"))
    # 자기(봇) 메시지 무시 — LLM 이 자기 답장에 또 답하는 무한루프 방지
    if user_id == BOT_ID:
        return

    msg = (event.get("msg") or "").strip()
    chat_id = str(j.get("chat_id"))
    sender = event.get("sender", "?")
    if not msg or not chat_id:
        return

    buf = history[chat_id]
    # 트리거가 아니어도 방의 최근 대화로 누적 (그룹방 대비 '이름: 본문')
    buf.append({
        "_id": str(j["_id"]) if j.get("_id") is not None else None,
        "ts": _ts(j.get("created_at")),
        "role": "user",
        "content": f"{sender}: {msg}",
    })

    if not msg.startswith(TRIGGER):
        return

    log.info("[%s] %s: %s → 바오 호출", chat_id, sender, msg)
    caller_rows = await fetch_user_history(http, chat_id, user_id)
    convo = build_context(buf, caller_rows, sender)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *convo]

    try:
        reply = await asyncio.to_thread(complete, messages)
    except Exception as e:
        log.exception("LLM 오류")
        reply = f"({BOT_NAME} 응답 실패: {e})"
    if not reply:
        reply = f"({BOT_NAME}가 답을 못 만들었어요.)"

    # 봇 답장은 /ws 로 다시 안 들어오므로(SYNCMSG 스킵) 직접 누적
    buf.append({"_id": None, "ts": time.time(), "role": "assistant", "content": reply})
    await send_reply(http, chat_id, reply)


async def main() -> None:
    if not os.environ.get("MULEROUTER_API_KEY"):
        log.error("MULEROUTER_API_KEY 가 설정되지 않았습니다. export 후 다시 실행하세요.")
        sys.exit(1)

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                log.info("connecting to %s", WS_URL)
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    log.info("connected (bot=%s, model=%s)", BOT_NAME, MODEL)
                    async for raw in ws:
                        try:
                            await handle(http, json.loads(raw))
                        except Exception:
                            log.exception("handler error")
            except Exception as e:
                log.warning("ws dropped: %s; retry in 3s", e)
                await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
