#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SA_FILE="${SA_FILE:-$DIR/../service-account.json}"

cd "$DIR"

if [ ! -f "$DIR/wrangler.toml" ]; then
  echo "❌ 未找到 $DIR/wrangler.toml，请先从 wrangler.example.toml 复制并填入你的域名和 X_FOLDER_ID"
  exit 1
fi

if [ ! -f "$SA_FILE" ]; then
  echo "❌ 未找到服务账号密钥: $SA_FILE"
  exit 1
fi

mkdir -p "$DIR/src/assets"
cp "$DIR/../index.html" "$DIR/src/assets/index.html"
cp "$DIR/../favicon.svg" "$DIR/src/assets/favicon.svg"

if [ ! -d "$DIR/node_modules/wrangler" ]; then
  echo "📦 首次部署：正在安装 wrangler..."
  npm install --no-audit --no-fund
fi

echo "🔐 检查 Cloudflare 登录状态（若未登录将打开浏览器授权）..."
if ! npx wrangler whoami >/dev/null 2>&1; then
  npx wrangler login
fi

VIEW_PW="${1:-}"
if [ -z "$VIEW_PW" ]; then
  read -r -p "🔑 请输入访客访问密码（任意字符，哪怕 1 个字符也行；直接回车表示免密公开）: " VIEW_PW
fi

COOKIE_SEC="$(openssl rand -hex 32)"

echo "🚀 正在部署 Worker 并绑定自定义域名..."
npx wrangler deploy

echo "🔒 正在加密上传 SA_JSON / VIEW_PASSWORD / COOKIE_SECRET 到 Cloudflare Secrets..."
npx wrangler secret put SA_JSON < "$SA_FILE"
printf "%s" "$VIEW_PW" | npx wrangler secret put VIEW_PASSWORD
printf "%s" "$COOKIE_SEC" | npx wrangler secret put COOKIE_SECRET

DOMAIN="$(sed -n 's/.*pattern *= *"\([^"]*\)".*/\1/p' "$DIR/wrangler.toml" | head -n 1)"
echo ""
echo "✅ 全部上线完成！稍等 10~30 秒证书签发后即可访问: https://${DOMAIN:-your-domain}"
echo "💡 以后随时改密码只需一行命令："
echo "   cd $DIR && printf '新密码' | npx wrangler secret put VIEW_PASSWORD"
