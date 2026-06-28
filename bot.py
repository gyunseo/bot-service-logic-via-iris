#!/usr/bin/env python3
"""Iris 카카오톡 봇 — "병상" 입력 시 심장내과중환자실 가용병상 현황 응답.

동작:
  카톡 메시지 ──(Iris)──▶ ws://PHONE/ws ──▶ 이 봇
  본문이 "병상"이면 ccu_status.build_report() 실행
  결과를 http://PHONE/reply 로 POST ──▶ Iris ──▶ 카톡 자동 송신

사용법:
  export SERVICE_KEY='발급받은_디코딩키'
  pip install websockets aiohttp requests
  python3 bot.py
"""
import asyncio
import json
import logging
import os
import sys

import aiohttp
import requests
import websockets

import ccu_status

PHONE = os.environ.get("IRIS_HOST", "10.203.131.9:3000")
WS_URL = f"ws://{PHONE}/ws"
REPLY = f"http://{PHONE}/reply"
BOT_ID = "443097014"            # curl http://PHONE/config 의 bot_id
TRIGGER = "병상"               # 이 단어로 시작하면 발동

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")


def make_report() -> str:
    """ccu_status 를 호출해 응답 문자열 생성 (블로킹 — 스레드에서 실행)."""
    service_key = os.environ.get("SERVICE_KEY")
    if not service_key:
        return "서버 설정 오류: SERVICE_KEY 가 설정되지 않았습니다."
    try:
        return ccu_status.build_report(service_key)
    except requests.RequestException as e:
        return f"병상 조회 실패(네트워크): {e}"
    except LookupError as e:
        return str(e)
    except Exception as e:  # ET.ParseError, RuntimeError 등
        return f"병상 조회 실패: {e}"


async def send_reply(http: aiohttp.ClientSession, room: str, text: str) -> None:
    try:
        async with http.post(
            REPLY, json={"type": "text", "room": str(room), "data": text}
        ) as resp:
            if resp.status != 200:
                log.warning("reply 실패 status=%s body=%s", resp.status, await resp.text())
    except Exception:
        log.exception("reply POST 오류")


async def handle(http: aiohttp.ClientSession, event: dict) -> None:
    # 자기 메시지 무시 (도배 루프 방지)
    if str(event.get("json", {}).get("user_id")) == BOT_ID:
        return

    msg = (event.get("msg") or "").strip()
    room = event["json"]["chat_id"]
    sender = event.get("sender", "?")

    if not msg.startswith(TRIGGER):
        return

    log.info("[%s] %s → 병상 조회", sender, msg)
    # 블로킹 HTTP 호출은 이벤트 루프 밖(스레드)에서
    report = await asyncio.to_thread(make_report)
    await send_reply(http, room, report)


async def main() -> None:
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                log.info("connecting to %s", WS_URL)
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    log.info("connected")
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
