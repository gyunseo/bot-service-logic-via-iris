# 카카오톡 봇 (Iris) — 병상 조회 + AI 챗봇 "바오"

루팅된 안드로이드 폰에서 도는 **Iris**를 다리 삼아 카카오톡에 붙는 봇 모음.

- **병상 봇 (`bot.py`)** : **"병상"** 입력 시 **삼성서울병원 심장내과중환자실(CCU) 실시간 가용병상**을 답장.
- **AI 챗봇 "바오" (`bao.py`)** : **"바오"** 로 시작하면 **방(chat_id)별 대화 맥락**을 모아 **Qwen3.5 Plus**(MuleRouter)로 답장.

```
[카톡 "병상"] ──▶ Iris(폰, 폰_IP:3000) ──ws──▶ bot.py(이 봇)
                                                           │  ccu_status.build_report()
                                                           ▼  국립중앙의료원 오픈API
                        Iris ◀── POST /reply ◀── 가용병상 현황 문자열
                          │
                          ▼
                   [카톡 자동 답장]
```

> Iris의 동작 원리·셋업 과정은 [`iris-setup-notes.md`](./iris-setup-notes.md) 참고.

---

## 구성 파일

| 파일                           | 역할                                                                                                             |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `ccu_status.py`                | 국립중앙의료원 오픈API로 CCU 가용병상 조회. `build_report(service_key)`가 결과를 문자열로 반환 (CLI 실행도 가능) |
| `bot.py`                       | **병상 봇.** Iris WebSocket(`/ws`)에 붙어 "병상" 트리거 감지 → `build_report()` 실행 → `/reply`로 답장           |
| `bao.py`                       | **AI 챗봇 "바오".** `/ws` 수신 → 방별 대화 누적 + `/query` 백필 → Qwen3.5 Plus 호출 → `/reply`로 답장            |
| `requirements.txt`             | `websockets`, `aiohttp`, `requests`, `openai`, `python-dotenv`                                                   |
| `Dockerfile`                   | `python:3.12-slim` 기반 이미지                                                                                   |
| `docker-compose.yml`           | `.env` 주입 + `restart: unless-stopped`                                                                          |
| `.env`                         | **SERVICE_KEY** / **MULEROUTER_API_KEY** / `IRIS_HOST` / `BOT_ID` (커밋 금지, `.gitignore` 처리됨)               |
| `.env.example`                 | `.env` 작성용 템플릿                                                                                             |
| `.dockerignore` / `.gitignore` | `.env`·캐시 제외                                                                                                 |

---

## 동작 원리

### 1. `ccu_status.py`
- 국립중앙의료원 **'응급실 실시간 가용병상정보 조회'** 오픈API 호출.
- 대상: 삼성서울병원(`hpid=A1100010`), 심장내과중환자실 필드 `hv34`(가용)/`hvs15`(정원).
- `build_report(service_key) -> str` : 사람이 읽을 현황 문자열 반환.
  - 네트워크/응답 오류 → `RuntimeError`, 대상 미발견 → `LookupError`.
- 단독 실행도 지원: `python3 ccu_status.py` (결과를 stdout 출력).

출력 예시:
```
[삼성서울병원] 심장내과중환자실
  가용/정원 : 2 / 8 병상   → 가용
  갱신시각  : 2026-06-29 00:30:00
  응급실전화 : 02-3410-2060
```

### 2. `bot.py` (WebSocket 방식)
- `ws://$IRIS_HOST/ws` 에 연결 (끊기면 3초 뒤 자동 재연결, `ping_interval=20`).
- 수신 메시지 본문이 **"병상"** 으로 시작하면 발동.
- 블로킹 HTTP 조회는 `asyncio.to_thread`로 실행해 이벤트 루프 비차단.
- 결과를 `http://$IRIS_HOST/reply` 로 POST (`{"type":"text","room":...,"data":...}`).
- `BOT_ID = 봇_아이디` 가드로 **자기 메시지 무시** → 답장 도배 루프 방지.

환경변수:
| 변수          | 필수 | 기본값       | 설명                                       |
| ------------- | ---- | ------------ | ------------------------------------------ |
| `SERVICE_KEY` | ✅    | —            | 국립중앙의료원 오픈API **디코딩** 서비스키 |
| `IRIS_HOST`   |      | `폰_IP:3000` | 폰에서 도는 Iris 주소                      |

### 3. `bao.py` — AI 챗봇 "바오" (방별 대화)

- `ws://$IRIS_HOST/ws` 에 연결 (끊기면 3초 뒤 자동 재연결, `ping_interval=20`).
- **모든 메시지를 방(`chat_id`)별 `deque`에 실시간 누적** → 방마다 독립된 대화 맥락 유지.
- 본문이 **"바오"** 로 시작하면 발동, 두 소스를 합쳐 **Qwen3.5 Plus**(`qwen3.5-plus`)에 전달:
  1. **`/query`** 로 *호출한 사용자*의 이 방 최근 20개 조회 (호출 시점에 신선하게, `SELECT *` 로 복호화 보장).
  2. **`/ws`** 로 누적한 *방 자체*의 최근 20개.
  - 둘을 `_id` 로 중복 제거하고 `created_at`(시간)순 정렬 → 시스템 프롬프트와 함께 호출.
- LLM 블로킹 호출은 `asyncio.to_thread`로 실행해 이벤트 루프 비차단.
- 결과를 `http://$IRIS_HOST/reply` 로 POST. 답장은 `send_lock` + `SEND_RATE`(기본 0.3s)로 **직렬화**(도배/BAN 회피).
- `BOT_ID` 가드로 **자기 메시지 무시** → 답장 루프 방지. 봇 답장은 `/ws`로 안 돌아오므로 직접 `assistant`로 누적.
- `.env` 자동 로드(`python-dotenv`) → `export` 없이 키 주입 가능.

환경변수:
| 변수                 | 필수 | 기본값       | 설명                                               |
| -------------------- | ---- | ------------ | -------------------------------------------------- |
| `MULEROUTER_API_KEY` | ✅    | —            | MuleRouter API 키 (Qwen3.5 Plus 호출용)            |
| `IRIS_HOST`          |      | `폰_IP:3000` | 폰에서 도는 Iris 주소                              |
| `BOT_ID`             |      | `봇_아이디`  | 봇 자신의 user_id. `curl $IRIS_HOST/config`로 확인 |

> 튜닝 상수는 `bao.py` 상단: `MODEL`, `ROOM_HISTORY`(20), `USER_HISTORY`(20), `SEND_RATE`(0.3s).
> caller 히스토리는 **이 방 한정**(`chat_id=? AND user_id=?`). 전체 방 합산을 원하면 `chat_id` 조건만 제거.

**MuleRouter 모델 ID 주의**: 공식 안내 문서의 예시 `qwen3-5-plus`(하이픈)는 **오기**다. 실제 `/models`가 반환하는 ID는 **`qwen3.5-plus`(점)**. 다른 ID도 `GET /vendors/openai/v1/models`로 확인 가능 (예: `qwen3.6-plus`, `qwen3.7-plus`, `qwen3-max`).

---

## 실행 방법

### Docker Compose (권장)

`bot`(병상) · `bao`(AI 챗봇) **두 서비스**가 같은 이미지를 공유하며 함께 뜬다.

```bash
# 1) .env 에 SERVICE_KEY(병상) + MULEROUTER_API_KEY(바오) 채우기
# 2) 빌드 & 기동 (두 봇 모두)
docker compose up -d --build
# 3) 로그 확인 — 각각 "connected" 뜬 뒤 카톡에 "병상" / "바오" 전송
docker compose logs -f            # 둘 다
docker compose logs -f bao        # 바오만
```

```bash
# 한쪽만 따로 띄우기/재시작
docker compose up -d bao
docker compose restart bao
```

> Docker 미설치 시:
> ```bash
> curl -fsSL https://get.docker.com | sh
> sudo usermod -aG docker $USER   # 재로그인 후 sudo 없이 사용
> ```

### 로컬 직접 실행
```bash
pip install -r requirements.txt

# 병상 봇
export SERVICE_KEY='발급받은_디코딩키'
python3 bot.py

# AI 챗봇 "바오" — .env 에 MULEROUTER_API_KEY 적어두면 export 불필요
python3 bao.py
```

> `bot.py`(병상)와 `bao.py`(바오)는 **서로 독립**이라 동시에 띄워도 됩니다.
> 둘 다 같은 `/ws`에 붙어도 Iris는 SharedFlow fan-out으로 여러 구독자를 지원합니다.

---

## Iris 동작 메모 (실측)

### `/query` 엔드포인트 (방별 로그 조회의 핵심)
- `POST /query` `{ "query": "...", "bind": [...] }` 로 카톡 DB에 **임의 SQL 실행** 가능. `bao.py`의 caller 백필이 이걸 사용.
- 응답: `{ "data": [ { "컬럼": "값", ... } ] }`. **모든 값이 문자열**(숫자 컬럼도) → 받는 쪽에서 파싱 필요. 에러 시 **HTTP 500** + `{ "message": ... }`.
- DB는 `:memory:` 에 3개 ATTACH: **`db1`**(KakaoTalk.db, `chat_logs`/`chat_rooms`), **`db2`**(KakaoTalk2.db, 오픈챗 관련), **`db3`**(멀티프로필). → 조회 시 `db1.chat_logs` 처럼 **DB 접두어** 필수(`sqlite_master`도 `db1.sqlite_master`).
- `message` 컬럼 자동 복호화는 **행에 `v`/`enc` 가 같이 있을 때만** 동작 → `SELECT *` 로 행 전체를 받아야 안전(필드 좁히면 암호문이 남을 수 있음).

### ⚠️ 발신자 이름(`sender`)은 best-effort — 틀릴 수 있음
- 이 폰의 카톡 DB엔 **`friends` 테이블이 없고 `open_chat_member`도 비어 있음.** `chat_logs.v`/`chat_rooms.members` 어디에도 사람 이름이 없음 → **DB로 user_id→이름 매핑 불가**.
- 그래서 Iris는 이름을 **NotificationPoller 캐시**(3초마다 카톡 알림에서 추출한 user_id→이름)로 채운다. 따라서:
  - 그 방에서 **알림을 띄운 적 있는 사람의 이름만** 학습됨. 한 명만 떠든 방은 **아는 이름이 그 한 명뿐**이라, 다른 사람(본인 포함) 메시지에도 그 이름이 잘못 붙는다.
  - 본인(봇 계정) 발신은 자기 폰에 알림이 안 생겨 그 경로로는 학습 안 됨.
  - 런타임 캐시라 **Iris 재시작 시 초기화**될 수 있음(추정).
- **결론**: 잘못된 이름은 `bao.py` 버그가 아니라 **Iris 이름 해석 한계**다(`bao.py`는 `event["sender"]`를 그대로 사용). 사람 구분이 필요한 로직은 항상 **`json.user_id`** 로 건다(이미 그렇게 구현됨). 방에 사람들이 더 떠들수록 이름은 점차 정확해진다.

---

## 검증 현황

- ✅ `폰_IP:3000/config` 응답 확인 — `bot_id=봇_아이디` (포트 80은 닫힘 → 3000 사용)
- ✅ `bot.py` / `ccu_status.py` / `bao.py` 문법 검증
- ✅ `docker-compose.yml` 구성 검증 (`docker compose config` 통과, `bot`+`bao` 2서비스)
- ✅ **MuleRouter 호출 검증** — `qwen3.5-plus` 로 실제 chat completion 응답 확인 (문서의 `qwen3-5-plus` 는 400 오류).
- ✅ **`/query` 실측** — 응답 형식·DB ATTACH 구조·이름 소스 부재 확인 (위 "Iris 동작 메모" 참고).
- ⚠️ **바오 WS 왕복 최종 확인 권장** — 카톡 "바오 ..." → 방별 컨텍스트 조합 → 답장까지 첫 라이브 1회.
- ⚠️ **`reply` payload 형식**(`type/room/data`)은 `iris-setup-notes.md` 6.3 예시 기준. 첫 테스트로 확정 필요.
