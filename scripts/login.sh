#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:99

echo "▶ 가상 디스플레이 시작"
Xvfb :99 -screen 0 1360x900x24 -nolisten tcp &
sleep 2

echo "▶ VNC 서버 시작"
x11vnc -display :99 -nopw -forever -shared -rfbport 5900 -quiet &
sleep 1

echo "▶ noVNC(웹) 시작 → http://localhost:${VNC_PORT:-6080}/vnc.html?autoconnect=1&resize=scale"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
sleep 1

python -m app.collector.login "$@"
