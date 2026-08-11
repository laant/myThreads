FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 빌드 중에만 적용 (런타임 환경은 건드리지 않음)
ARG DEBIAN_FRONTEND=noninteractive
ARG DEBCONF_NONINTERACTIVE_SEEN=true

# noVNC 스택 — 최초 1회 Threads 로그인을 브라우저로 하기 위해 필요.
# tzdata 가 지역 선택을 물어보지 않도록 설치 전에 타임존을 미리 확정한다.
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify tzdata fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
RUN chmod +x ./scripts/*.sh

RUN mkdir -p /data/media
VOLUME ["/data"]

EXPOSE 8080 6080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
