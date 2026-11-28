#!/usr/bin/env bash
set -euo pipefail

# Ubuntu 1c1g VPS 一键部署「𝕏 灵感库」公网只读服务（监听 127.0.0.1:8766，完全不碰 80/443，与 3x-ui 零冲突）
APP_DIR="${1:-/opt/h3-x-public}"
SERVICE_NAME="h3-x-readonly"

echo "=== 𝕏 灵感库公网只读服务安装 ($APP_DIR) ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未检测到 python3，请先运行: apt-get update && apt-get install -y python3 openssl"
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "❌ 未检测到 openssl，请先运行: apt-get update && apt-get install -y openssl"
  exit 1
fi

for f in server.py index.html favicon.svg; do
  if [ ! -f "$APP_DIR/$f" ]; then
    echo "❌ 缺少文件: $APP_DIR/$f"
    exit 1
  fi
done

if [ ! -f "$APP_DIR/service-account.json" ]; then
  echo "❌ 缺少 Google 只读服务账号密钥: $APP_DIR/service-account.json"
  echo "   请先在 Google Cloud 创建服务账号并下载 JSON 传到 $APP_DIR/service-account.json"
  exit 1
fi
chmod 600 "$APP_DIR/service-account.json"

if [ ! -f "$APP_DIR/readonly-config.json" ]; then
  if [ -f "$APP_DIR/readonly-config.example.json" ]; then
    cp "$APP_DIR/readonly-config.example.json" "$APP_DIR/readonly-config.json"
    echo "⚠️ 已从模板生成 $APP_DIR/readonly-config.json，请先修改里面的 password 和 allowed_hosts 后再重新运行本脚本！"
    exit 1
  fi
  echo "❌ 缺少配置文件: $APP_DIR/readonly-config.json"
  exit 1
fi
chmod 600 "$APP_DIR/readonly-config.json"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=H3 X-Inspiration Readonly Public Gallery
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/python3 -u ${APP_DIR}/server.py --readonly --config ${APP_DIR}/readonly-config.json
Restart=always
RestartSec=3
MemoryMax=256M
NoNewPrivileges=true
PrivateTmp=false

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 1.5
systemctl --no-pager --full status "${SERVICE_NAME}" | head -n 15

PORT=$(python3 -c "import json; print(json.load(open('${APP_DIR}/readonly-config.json')).get('port', 8766))")
echo ""
echo "=== 本机探活 http://127.0.0.1:${PORT}/api/status ==="
curl -sS "http://127.0.0.1:${PORT}/api/status" && echo ""
echo ""
echo "✅ 服务已常驻运行在 127.0.0.1:${PORT}（不影响 3x-ui 任何端口）！"
echo "👉 下一步在 Cloudflare Zero Trust -> Networks -> Tunnels 里添加一条 Public Hostname："
echo "   Service: HTTP   URL: 127.0.0.1:${PORT}"
echo "   改密码只需编辑 ${APP_DIR}/readonly-config.json 后执行: systemctl restart ${SERVICE_NAME}"
