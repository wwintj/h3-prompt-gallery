#!/bin/bash
# H3 提示词画廊 —— 一键升级
# 只更新程序文件；你的授权（credentials.json / token.json）、配置和提示词数据都不会动。
# 整个脚本包在 { } 里：先完整读入再执行，升级过程中覆盖自身也不会出错。
{
set -euo pipefail

REPO="wwintj/h3-prompt-gallery"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/main"
PORT="${H3_PORT:-8765}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

say()  { printf '\033[1;33m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -f server.py ] || die "$DIR 里没有 server.py，不像是画廊的安装目录"

# 1. 正在运行的话先停掉
# 只认从本文件夹启动的画廊进程，不误杀其他程序或别处安装的画廊
pids=""
for p in $(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
  ps -o command= -p "$p" 2>/dev/null | grep -q 'server\.py' || continue
  cwd="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  if [ "$(cd "$cwd" 2>/dev/null && pwd -P)" = "$(cd "$DIR" && pwd -P)" ] \
     || ps -o command= -p "$p" | grep -qF "$DIR/server.py"; then
    pids="$pids $p"
  fi
done
pids="${pids# }"
was_running=""
if [ -n "$pids" ]; then
  was_running=1
  say "先停止正在运行的画廊（端口 $PORT）"
  kill $pids 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
fi

# 2. 下载新版本
say "检查更新……"
if [ -d .git ] && command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
  old="$(git rev-parse --short HEAD)"
  git pull --quiet --ff-only \
    || die "升级失败：你可能改过程序文件。可以先卸载再重新安装（授权文件可以选择保留）"
  new="$(git rev-parse --short HEAD)"
  if [ "$old" = "$new" ]; then
    ok "已经是最新版本（$new）"
  else
    ok "已从 $old 升级到 $new，更新内容："
    git log --format='    · %s' "$old..$new"
  fi
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "$TARBALL" | tar xz -C "$tmp" --strip-components 1
  # 下载包里不含你的私密文件，直接覆盖程序文件即可
  cp -R "$tmp"/. "$DIR"/
  ok "已更新到最新版本"
fi
chmod +x "$DIR"/*.command "$DIR"/*.sh 2>/dev/null || true

# 3. 原来在运行的话重新启动
if [ -n "$was_running" ]; then
  say "重新启动画廊"
  exec bash "$DIR/启动画廊.command"
fi
echo "双击「启动画廊.command」即可使用。"
exit 0
}
