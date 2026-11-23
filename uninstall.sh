#!/bin/bash
# H3 提示词画廊 —— 一键卸载
# 只删除本程序自己的文件；Google Drive 里的图片、视频和提示词（h3-gallery-meta.json）都不受影响。
#
# 也可以不进入文件夹直接运行：
#   curl -fsSL https://raw.githubusercontent.com/wwintj/h3-prompt-gallery/main/uninstall.sh | bash
{
set -euo pipefail

PORT="${H3_PORT:-8765}"
KEPT_CREDS="$HOME/h3-gallery-credentials.json"
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  DIR="${H3_DIR:-$HOME/h3-prompt-gallery}"
fi

say()  { printf '\033[1;33m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1" a </dev/tty || a=""; [ "$a" = "y" ] || [ "$a" = "Y" ]; }

[ -f "$DIR/server.py" ] || die "$DIR 里没有找到画廊（没有 server.py）"

echo "将卸载：$DIR"
echo "  · 删除程序文件，以及本机的登录授权（token.json）、配置和本地备份"
echo "  · Google Drive 里的作品和提示词不会被删除"
echo
ask "确定卸载吗？输入 y 回车确认：" || { echo "已取消，什么都没删。"; exit 0; }

# 1. 停止正在运行的画廊
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
if [ -n "$pids" ]; then
  say "停止正在运行的画廊"
  kill $pids 2>/dev/null || true
fi

# 2. credentials.json 是在 Google Cloud 里辛苦建的，问一下要不要留着
if [ -f "$DIR/credentials.json" ]; then
  if ask "要保留 credentials.json 吗？以后重装会自动用上，不用再去 Google Cloud 创建 [y/N]："; then
    mv "$DIR/credentials.json" "$KEPT_CREDS"
    chmod 600 "$KEPT_CREDS"
    ok "已保留到 $KEPT_CREDS"
  fi
fi

# 3. 只删除本程序的文件，文件夹里你自己放的其他东西不会动
cd "$DIR"
rm -rf .git
rm -f .gitignore README.md favicon.svg index.html server.py \
      install.sh upgrade.sh uninstall.sh \
      启动画廊.command 升级.command 卸载.command \
      credentials.json token.json config.json meta-backup.json .DS_Store
cd ..
if rmdir "$DIR" 2>/dev/null; then
  ok "已卸载，文件夹 $DIR 已删除"
else
  ok "程序已卸载。文件夹里还有你自己的其他文件，所以保留了 $DIR："
  ls -A "$DIR" | sed 's/^/    · /'
fi
echo
echo "如果想同时撤销画廊对 Google Drive 的访问权限，可以到这里移除："
echo "  https://myaccount.google.com/connections"
exit 0
}
