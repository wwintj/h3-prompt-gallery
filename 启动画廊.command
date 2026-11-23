#!/bin/bash
# 双击此文件即可启动本地画廊并在浏览器中打开
cd "$(dirname "$0")"
PORT=8765
URL="http://127.0.0.1:$PORT"

if curl -s -o /dev/null "$URL"; then
  echo "画廊已经在运行，直接打开：$URL"
  open "$URL"
  exit 0
fi

( sleep 1; open "$URL" ) &
echo "正在启动画廊……关闭这个窗口即可停止。"
exec python3 server.py "$PORT"
