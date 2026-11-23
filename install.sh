#!/bin/bash
# H3 提示词画廊 —— 一键安装（已安装时自动改为升级）
#
#   curl -fsSL https://raw.githubusercontent.com/wwintj/h3-prompt-gallery/main/install.sh | bash
#
# 可选环境变量：
#   H3_DIR=安装位置（默认 ~/h3-prompt-gallery）
#   H3_NO_START=1   安装后不自动启动
{
set -euo pipefail

REPO="wwintj/h3-prompt-gallery"
REPO_URL="${H3_REPO_URL:-https://github.com/$REPO.git}"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/main"
DIR="${H3_DIR:-$HOME/h3-prompt-gallery}"
KEPT_CREDS="$HOME/h3-gallery-credentials.json"

say()  { printf '\033[1;33m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 1. 检查系统和 Python
[ "$(uname -s)" = "Darwin" ] || die "目前只支持 macOS"
command -v python3 >/dev/null 2>&1 || die "没找到 python3。macOS 请先运行 xcode-select --install，或到 https://www.python.org 安装 Python 3.9+"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "需要 Python 3.9 或更高版本，当前是 $(python3 -V 2>&1)"

# 2. 已安装 → 升级
if [ -f "$DIR/server.py" ]; then
  say "检测到已安装在 $DIR，改为执行升级"
  [ -f "$DIR/upgrade.sh" ] || die "旧版本没有升级脚本，请先卸载（删除 $DIR）后重新安装"
  exec bash "$DIR/upgrade.sh"
fi
if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  die "$DIR 已存在且不是空文件夹。请换个位置：H3_DIR=其他路径 再运行一次"
fi

# 3. 下载
say "下载到 $DIR"
if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
  git clone --quiet --depth 1 "$REPO_URL" "$DIR"
else
  mkdir -p "$DIR"
  curl -fsSL "$TARBALL" | tar xz -C "$DIR" --strip-components 1
fi
chmod +x "$DIR"/*.command "$DIR"/*.sh 2>/dev/null || true

# 4. 找回卸载时保留的授权文件
if [ -f "$KEPT_CREDS" ] && [ ! -f "$DIR/credentials.json" ]; then
  mv "$KEPT_CREDS" "$DIR/credentials.json"
  chmod 600 "$DIR/credentials.json"
  ok "已找回之前保留的 credentials.json"
fi

ok "安装完成：$DIR"
echo
echo "  以后启动：双击 $DIR/启动画廊.command"
echo "           或运行 python3 \"$DIR/server.py\""
echo "  一键升级：双击 升级.command，或再运行一次安装命令"
echo "  一键卸载：双击 卸载.command"
echo

# 5. 启动
if [ "${H3_NO_START:-}" = "1" ]; then exit 0; fi
exec bash "$DIR/启动画廊.command"
}
