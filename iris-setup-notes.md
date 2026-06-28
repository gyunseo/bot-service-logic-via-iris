# Iris 카카오톡 봇 셋업 노트

루팅된 안드로이드 폰 + 라즈베리파이로 카카오톡 봇을 구축한 과정 정리.

---

## 1. Iris 프로젝트가 동작하는 원리

### 1.1 한 줄 요약

Iris는 APK 형태로 빌드되지만 **안드로이드 앱으로 설치되지 않습니다**. `app_process`로 직접 실행되는 **루트 권한 데몬**이며, 카카오톡의 SQLite DB를 읽고 복호화한 뒤 HTTP·WebSocket으로 외부 봇 로직에 전달하고, 답장은 카카오톡의 알림 답장(RemoteInput)을 가로채는 방식으로 보냅니다.

### 1.2 실행 트릭

```bash
su root sh -c "CLASSPATH=/data/local/tmp/Iris.apk app_process / party.qwer.iris.Main"
```

- `pm install` 안 함. APK는 그냥 DEX 컨테이너.
- `app_process`(ART 런처)가 `CLASSPATH`의 DEX에서 `main()`을 실행.
- `su root`로 격상 → 카톡의 보호된 DB 접근 + hidden ActivityManager API 호출 가능.
- `AndroidManifest.xml`이 사실상 비어있음 (Activity/Service 없음).

### 1.3 부팅 시 켜지는 컴포넌트

```
Main.main()
 ├─ Replier.startMessageSender()      메시지 전송 큐 (Channel + 코루틴)
 ├─ KakaoDB()                          카카오 DB 3개 attach
 ├─ ObserverHelper(db, wsFlow)         새 로그 처리·복호화·브로드캐스트
 ├─ DBObserver(...).startPolling()    100ms마다 chat_logs 폴링
 ├─ NotificationPoller().startPolling() 3초마다 카톡 알림 추출 → 이름 캐시
 ├─ ImageDeleter(...).startDeletion() 1시간마다 임시 이미지 청소
 └─ IrisServer(...).startServer()     Ktor/Netty, 기본 3000포트
```

설정은 `/data/local/tmp/config.json`에 저장.

### 1.4 DB 접근 구조

```kotlin
val connection = SQLiteDatabase.openDatabase(":memory:", ...)
connection.execSQL("ATTACH DATABASE '.../KakaoTalk.db'  AS db1")
connection.execSQL("ATTACH DATABASE '.../KakaoTalk2.db' AS db2")
connection.execSQL("ATTACH DATABASE '.../multi_profile_database.db' AS db3")
```

- `:memory:` DB를 만들고 3개의 카톡 DB를 ATTACH → cross-db join 가능, 원본 무변경.
- 경로는 `/data_mirror/data_ce/null/<uid>/com.kakao.talk/` 우선, 없으면 `/data/data/com.kakao.talk/`.

폴링 루프:
```sql
SELECT count(*) FROM chat_logs WHERE _id > ?
SELECT *        FROM chat_logs WHERE _id > ? ORDER BY _id ASC
```

`v` 컬럼 JSON의 `enc`, `origin`을 보고 `SYNCMSG`/`MCHATLOGS`는 스킵.

### 1.5 메시지 복호화 원리 (KakaoDecrypt.kt)

카카오톡은 **PKCS#12 키 유도 + AES-CBC**를 씁니다.

**결정론적 입력:**
- 16바이트 `keyBytes` (모든 사용자 공통, 하드코딩)
- 16바이트 `ivBytes` (모든 사용자 공통, 하드코딩)
- **salt**만 사용자별·암호화방식별로 다름

**Salt 생성:**
```
salt = (prefixes[encType] + user_id) 의 앞 16바이트
부족하면 \x00 패딩
```

`prefixes`는 사람 이름 배열(isabel, kale, sulli, ...) + `incept(830819)`로 만들어지는 ARM64 어셈블리 니모닉 기반 난독화 문자열.

**키 유도 (deriveKey):**
1. password를 UTF-16BE + NUL 종결로 인코딩
2. 64바이트 단위로 `D`/`S`/`P` 버퍼 구성
3. `SHA1(D || I)`를 반복 (iterations=2)
4. `pkcs16adjust`로 PKCS#12 표준 캐리 연산

**복호화:**
```kotlin
Cipher.getInstance("AES/CBC/NoPadding")
   .init(DECRYPT_MODE, SecretKeySpec(key,"AES"), IvParameterSpec(ivBytes))
val padded = cipher.doFinal(Base64.decode(ciphertext))
val plaintext = padded.dropLast(padded.last())   // 수동 PKCS#7 strip
```

`NoPadding` + 수동 strip → 카톡의 비표준 패딩에서도 원문 폴백 가능.

**어떤 컬럼이 어떤 키로 복호화되나:**
- `chat_logs.message`, `chat_logs.attachment` → **발신자 user_id**로
- `friends.name`, `open_chat_member.nickname`, `profile_image_url` 등 → **본인 bot_id**로

bot_id는 부팅 시 `SELECT user_id FROM chat_logs WHERE v LIKE '%"isMine":true%'`로 자동 추출.

### 1.6 답장 송신 — Notification RemoteInput 가로채기

카톡 내부 API를 호출하지 않고, **사용자가 알림창 '답장' 버튼 누르는 동작을 시뮬레이션**합니다.

1. `shared_prefs/KakaoTalk.hw.perferences.xml`에서 `NotificationReferer` 토큰 추출
2. `NotificationActionService`/`REPLY_MESSAGE` 액션의 Intent에 `RemoteInput`으로 메시지 첨부
3. hidden `IActivityManager.startService`를 리플렉션으로 호출 (calling package를 `com.android.shell`로 위장)
4. 전송은 `Channel<SendMessageRequest>` + `Mutex` + delay로 직렬화 (도배 차단 회피)

### 1.7 전체 흐름

```
[카톡 사용자] ──메시지──> KakaoTalk.db (chat_logs)
                              │   (100ms 폴링)
                              ▼
                       ObserverHelper (복호화)
                              │
                ┌─ MutableSharedFlow ─> /ws (WebSocket fan-out)
                └─ OkHttp POST ─> web_server_endpoint
                              │
                              ▼
                    [외부 봇 서버 / 사용자 로직]
                              │
                     HTTP POST /reply
                              ▼
                          IrisServer
                              │
                              ▼
                            Replier (RemoteInput Intent)
                              │
                              ▼
              hidden ActivityManager → 카톡 NotificationActionService
                              │
                              ▼
                       [카톡 메시지 발송]
```

---

## 2. 권한 모델 정리

- **카카오톡은 일반 실행** — root 모드로 띄울 필요 없음. Magisk DenyList에 `com.kakao.talk` 넣어서 **루팅을 카톡한테 숨기는** 게 일반적.
- **Iris만 root로 실행** — 카톡 보호된 DB 읽기 + hidden API 사용을 위해.

---

## 3. 설치 & 첫 실행 (macOS + ADB)

### 3.1 폰 사전 준비
- 개발자 옵션 → USB 디버깅 ON
- 카톡 한 번 띄워서 NotificationReferer 생성된 상태
- USB-C는 그냥 USB임 — **데이터 전송 지원 케이블**이면 OK (충전 전용은 안 됨)

### 3.2 macOS bash 3.2 이슈

`./iris_control install` 실행 시:
```
mapfile: command not found
Error: No adb devices found in 'device' state.
```

macOS의 `/bin/bash`가 GPLv3 라이선스 문제로 3.2에 묶여 있어 `mapfile`(bash 4+ 빌트인)이 없음. 폰 연결 문제 아님.

**해결책: 수동 실행 (제일 빠름)**

```bash
# install
curl -LO https://github.com/dolidolih/Iris/releases/latest/download/Iris.apk
adb -s <DEVICE_ID> push Iris.apk /data/local/tmp/Iris.apk

# start (백그라운드)
adb -s <DEVICE_ID> shell "su root sh -c 'CLASSPATH=/data/local/tmp/Iris.apk app_process / party.qwer.iris.Main > /dev/null 2>&1' &"

# 디버깅용 포그라운드 시작
adb -s <DEVICE_ID> shell "su root sh -c 'CLASSPATH=/data/local/tmp/Iris.apk app_process / party.qwer.iris.Main'"

# status
adb -s <DEVICE_ID> shell "ps -ef | grep app_process | grep -v grep"
```

### 3.3 정상 실행 시그널

```
shell  4305     1  su root sh -c CLASSPATH=...     ← 래퍼
root   4385  1091  sh -c CLASSPATH=...               ← 래퍼
root   4386  4385  app_process / party.qwer.iris.Main  ← 실제 Iris
```

세 번째 줄(`app_process / party.qwer.iris.Main`)이 살아있으면 OK.

### 3.4 동작 확인

```bash
adb -s <DEVICE_ID> forward tcp:3000 tcp:3000
curl http://127.0.0.1:3000/config
```

`bot_id`가 0이 아닌 숫자로 떠야 정상. 0이면 본인이 카톡에서 메시지 한 번 보내고 Iris 재시작.

대시보드: `http://127.0.0.1:3000/dashboard`

### 3.5 종료

```bash
# 주의: su 인자 파싱 문제로 -9가 가로채일 수 있음
# ❌ adb shell "su root kill -9 4386 4385"      → "invalid option -- 9"

# ✅ -c로 묶기
adb shell "su -c 'kill -9 4386 4385'"

# 또는 시그널 이름으로
adb shell "su root kill -KILL 4386 4385"

# 또는 -- 로 옵션 종료
adb shell "su root -- kill -9 4386 4385"
```

---

## 4. 자동 답장 — Iris와 봇 로직의 역할 분리

Iris는 다리(bridge)만 깔아주고, **"무엇에 어떻게 답할지" 결정하는 두뇌는 본인이 만들어야** 함.

```
[카톡 메시지 도착]
      ↓ (Iris가 감지·복호화)
   Iris ──POST/WS──▶  [본인 봇 서버]   ← 명령어 처리, LLM, 등
                          ↓
                   POST /reply ──▶ Iris ──▶ [카톡 자동 송신]
```

### 4.1 봇 서버로 들어오는 이벤트 JSON

```json
{
  "msg": "복호화된 메시지 본문",
  "room": "방 이름 (또는 1:1이면 상대 이름)",
  "sender": "보낸 사람 이름",
  "json": {
    "_id": "...",
    "chat_id": "1234567890",
    "user_id": "보낸사람_user_id",
    "message": "본문 (복호화됨)",
    "attachment": "{...}",
    "v": "{\"enc\":..., \"origin\":\"...\"}",
    "type": "1"
  }
}
```

### 4.2 주의사항

- **답장 도배 루프 방지**: 본인이 보낸 메시지(`v.origin == "SYNCMSG"`)는 Iris가 자동 스킵하지만, 봇 쪽에서도 `user_id == bot_id`면 무시하는 가드 추가 권장
- **카톡 BAN 위험**: `message_send_rate`(기본 50ms)를 너무 낮추지 말 것. 실전 200~500ms 권장
- **카톡 죽으면 같이 죽음**: NotificationReferer가 무효화됨 → 카톡 켜고 Iris 재시작 필요

---

## 5. HTTP POST vs WebSocket

| 항목 | HTTP POST | WebSocket (`/ws`) |
|---|---|---|
| 봇 서버 공개 엔드포인트 필요 | 필요 (Iris가 들어옴) | 불필요 (봇이 나감) |
| 동시 구독자 | 하나만 | 여러 개 (SharedFlow fan-out) |
| 레이턴시 | 매 요청마다 핸드셰이크 | 한 번 연결, 프레임만 |
| Iris 설정 | endpoint URL 박아야 | 0 (그냥 `/ws`에 붙기) |
| 끊긴 동안 메시지 | 매 요청 독립 (실패는 로그만) | **유실됨** (replay buffer 없음) |
| 재연결 로직 | 불필요 | 필요 |

**둘 다 동시 사용 가능** — 코드상 WS는 항상 emit, HTTP는 endpoint가 설정돼 있을 때만 추가로 POST.

전형적 구성:
- 프로덕션 봇: HTTP endpoint
- 개발/디버그 콘솔: WebSocket으로 동시 접속

---

## 6. 최종 운영 구성 — 같은 네트워크 (폰 방에 박아둠 + 라즈베리파이)

### 6.1 폰 IP 고정

공유기 관리 페이지 → DHCP 설정 → 폰 MAC 주소에 IP 고정 할당.
(예: `192.168.0.50`)

### 6.2 폰 — Iris 상시 가동

**방법 A: Magisk 부팅 스크립트 (추천)**

```bash
# /data/adb/service.d/iris.sh
#!/system/bin/sh
sleep 60
CLASSPATH=/data/local/tmp/Iris.apk app_process / party.qwer.iris.Main > /dev/null 2>&1 &
```

```bash
chmod 755 /data/adb/service.d/iris.sh
```

`sleep 60`은 카톡이 먼저 떠서 NotificationReferer 준비될 시간.

**방법 B: Termux:Boot**

```bash
# ~/.termux/boot/start-iris.sh
#!/data/data/com.termux/files/usr/bin/sh
sleep 60
su -c "CLASSPATH=/data/local/tmp/Iris.apk app_process / party.qwer.iris.Main" > /dev/null 2>&1 &
```

```bash
chmod +x ~/.termux/boot/start-iris.sh
```

**추가:**
- 카톡 배터리 최적화 → **제한 없음**
- Wi-Fi 절전 OFF
- 상시 충전 연결

### 6.3 파이 — 봇 셋업 (Python + WebSocket)

```bash
sudo apt update
sudo apt install python3-pip
pip3 install websockets aiohttp
mkdir -p ~/bot && cd ~/bot
```

`~/bot/bot.py`:

```python
import asyncio, json, logging, websockets, aiohttp

PHONE  = "192.168.0.50:3000"
WS_URL = f"ws://{PHONE}/ws"
REPLY  = f"http://{PHONE}/reply"
BOT_ID = "내봇_user_id"   # curl http://192.168.0.50:3000/config 로 확인

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

async def handle(http, event):
    # 자기 메시지 무시 (도배 루프 방지)
    if event["json"].get("user_id") == BOT_ID:
        return

    msg    = event.get("msg") or ""
    room   = event["json"]["chat_id"]
    sender = event.get("sender", "?")
    log.info(f"[{sender}] {msg}")

    if msg.startswith("!ping"):
        await http.post(REPLY, json={
            "type": "text", "room": str(room), "data": "pong"
        })

async def main():
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                log.info(f"connecting to {WS_URL}")
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    log.info("connected")
                    async for raw in ws:
                        try:
                            await handle(http, json.loads(raw))
                        except Exception:
                            log.exception("handler error")
            except Exception as e:
                log.warning(f"ws dropped: {e}; retry in 3s")
                await asyncio.sleep(3)

asyncio.run(main())
```

`ping_interval=20`은 공유기 idle connection 차단 방지.

### 6.4 systemd로 상시 실행

`/etc/systemd/system/kakaobot.service`:

```ini
[Unit]
Description=Kakao Bot via Iris
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/bot
ExecStart=/usr/bin/python3 /home/pi/bot/bot.py
Restart=always
RestartSec=5
StandardOutput=append:/home/pi/bot/bot.log
StandardError=append:/home/pi/bot/bot.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kakaobot
sudo systemctl status kakaobot
tail -f ~/bot/bot.log
```

→ 파이 재부팅해도 자동 실행, 봇 죽으면 5초 뒤 자동 부활, 폰 Wi-Fi 끊겼다 돌아오면 자동 재연결.

### 6.5 동작 검증

1. 파이: `sudo systemctl status kakaobot` → active
2. 폰: `ps -ef | grep app_process` → Iris 떠있음
3. 카톡 아무 방에 `!ping` 전송
4. 파이 `tail -f ~/bot/bot.log` → 메시지 찍힘 + 카톡에 `pong` 답장

---

## 7. 자주 막히는 지점

| 증상 | 원인/해결 |
|---|---|
| `SQLiteException: permission to access KakaoTalk Database` | Iris가 root로 안 떴음. Magisk에서 권한 거부했는지 확인 |
| `failed to extract referer from data` | 카톡이 알림 한 번도 안 만든 상태. 외부에서 본인에게 메시지 보내서 알림 한 번 띄우고 Iris 재시작 |
| 답장이 안 감 / `failed to get startService Method` | 안드로이드 SDK 버전 안 맞음. issue로 보고 필요 |
| 카톡이 갑자기 종료/로그아웃 | DenyList에 `com.kakao.talk` 들어있는지 재확인 |
| 3000번 포트 안 열림 | 공유기 AP isolation 켜져있을 수 있음 |
| `bot_id`가 0 | 본인이 보낸 메시지가 DB에 없음. 카톡에서 한 번 보내고 재시작 |
| `su: invalid option -- 9` | su가 -9를 자기 옵션으로 먹음. `su -c 'kill -9 ...'`로 묶기 |
| Wi-Fi 끊겼다 돌아온 후 봇 묵묵부답 | `ping_interval` 설정 + 재연결 루프 확인 |

---

## 8. 참고 — 주요 파일 위치

| 파일 | 역할 |
|---|---|
| `Main.kt:14` | 진입점, 컴포넌트 부팅 |
| `KakaoDB.kt:15` | 메모리 DB + 카톡 DB ATTACH |
| `KakaoDecrypt.kt:239` | AES-CBC 복호화 진입점 |
| `KakaoDecrypt.kt:141` | salt 생성 (user_id + prefixes) |
| `KakaoDecrypt.kt:175` | PKCS#12 키 유도 |
| `ObserverHelper.kt:25` | 폴링 + 복호화 + 브로드캐스트 |
| `Replier.kt:58` | RemoteInput Intent 합성 |
| `AndroidHiddenApi.kt` | hidden ActivityManager 리플렉션 |
| `IrisServer.kt:51` | Ktor HTTP/WS 라우팅 |
| `Configurable.kt:13` | `/data/local/tmp/config.json` |
| `iris_control:7` | `app_process` 부팅 명령 |
