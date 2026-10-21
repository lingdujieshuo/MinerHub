#!/usr/bin/env bash
# =====================================================================
#  MinerHub 面板端安装（任意一台 Ubuntu/Debian：局域网小主机、某台矿机或 VPS 都行）
#  用法：把整个 server 目录上传后，在 server 目录里执行：
#    sudo bash install-server.sh [端口，默认 8800]
# =====================================================================
set -euo pipefail
[ "$(id -u)" = "0" ] || { echo "请用 root 或 sudo 运行"; exit 1; }
PORT="${1:-8800}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/minerhub-server

command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
mkdir -p "$DST/data"
cp -f "$SRC/server.py" "$DST/"
rm -rf "$DST/static" && cp -r "$SRC/static" "$DST/"

FIRST=0
[ -f "$DST/data/minerhub.db" ] || FIRST=1
if [ "$FIRST" = "1" ]; then
  PW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')"
  MINERHUB_DATA="$DST/data" python3 "$DST/server.py" passwd "$PW" >/dev/null
  ENT="$(python3 -c 'import secrets,string;a=string.ascii_lowercase+string.digits;print("".join(secrets.choice(a) for _ in range(14)))')"
  MINERHUB_DATA="$DST/data" python3 "$DST/server.py" entrance "$ENT" >/dev/null
fi

cat > /etc/systemd/system/minerhub-server.service <<EOF
[Unit]
Description=MinerHub Server (mining farm dashboard)
After=network-online.target

[Service]
Environment=MINERHUB_PORT=$PORT
Environment=MINERHUB_DATA=$DST/data
ExecStart=/usr/bin/python3 $DST/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable minerhub-server >/dev/null 2>&1
systemctl restart minerhub-server
sleep 1

IP="$(hostname -I | awk '{print $1}')"
echo
echo "✅ MinerHub 面板已启动（请收藏下面带后缀的地址）"
[ "$FIRST" = "1" ] && echo "   管理员密码： $PW   （请登录后在【设置】里修改）"
MINERHUB_DATA="$DST/data" MINERHUB_PORT="$PORT" MINERHUB_PUBLIC_URL="http://$IP:$PORT" python3 "$DST/server.py" info
echo "   忘记密码： sudo MINERHUB_DATA=$DST/data python3 $DST/server.py passwd 新密码"
echo "   改安全入口：sudo MINERHUB_DATA=$DST/data python3 $DST/server.py entrance 新后缀"
