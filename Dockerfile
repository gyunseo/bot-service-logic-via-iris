FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 봇 + 조회 로직 (bot.py=병상, bao.py=AI 챗봇)
COPY bot.py ccu_status.py bao.py ./

# 로그 즉시 플러시
ENV PYTHONUNBUFFERED=1

# 기본 실행은 병상 봇. compose 에서 서비스별 command 로 덮어씀.
CMD ["python3", "bot.py"]
