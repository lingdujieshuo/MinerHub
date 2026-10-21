#!/usr/bin/env bash
# =====================================================================
#  MinerHub 矿机端一键安装（Ubuntu 22.04 / 24.04，NVIDIA 显卡）
#  用法（在矿机上执行，面板 → 设置 页面可直接复制带 Key 的命令）：
#    curl -fsSL http://面板地址:8800/dl/install-agent.sh | sudo bash -s -- http://面板地址:8800 FARM_KEY [矿机名]
#  可选环境变量：
#    SRB_URL=...      指定 SRBMiner Linux 压缩包下载地址（默认取 GitHub 最新版）
#    SRB_UPDATE=1     强制重新下载 / 升级 SRBMiner
# =====================================================================
set -euo pipefail

SERVER="${1:-}"; FARM_KEY="${2:-}"; RIG="${3:-$(hostname)}"
if [ -z "$SERVER" ] || [ -z "$FARM_KEY" ]; then
  echo "用法: curl -fsSL <面板>/dl/install-agent.sh | sudo bash -s -- <面板地址> <FARM_KEY> [矿机名]"; exit 1
fi
[ "$(id -u)" = "0" ] || { echo "请用 root 或 sudo 运行"; exit 1; }
SERVER="${SERVER%/}"
RIG="$(echo "$RIG" | tr -c 'A-Za-z0-9_.\n-' '-')"
DIR=/opt/minerhub
mkdir -p "$DIR/srbminer" /var/log/minerhub

echo "==> [1/5] 安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
apt-get install -y -qq python3 curl tar xz-utils util-linux ethtool iproute2 >/dev/null
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "   ⚠ 没检测到 NVIDIA 驱动。请先执行： sudo ubuntu-drivers install && sudo reboot   然后重新运行本脚本"
fi

echo "==> [2/5] 安装 SRBMiner-MULTI"
if [ -x "$DIR/srbminer/SRBMiner-MULTI" ] && [ "${SRB_UPDATE:-0}" != "1" ]; then
  echo "   已存在，跳过（SRB_UPDATE=1 可强制升级）"
else
  URL="${SRB_URL:-}"
  if [ -z "$URL" ]; then
    URL="$(curl -fsSL https://api.github.com/repos/doktor83/SRBMiner-Multi/releases/latest 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(next(a["browser_download_url"] for a in d["assets"] if "Linux" in a["name"] and a["name"].endswith((".tar.gz", ".tar.xz"))))
except Exception:
    pass' || true)"
  fi
  [ -z "$URL" ] && URL="https://github.com/doktor83/SRBMiner-Multi/releases/download/3.6.6/SRBMiner-Multi-3-6-6-Linux.tar.gz"
  echo "   下载: $URL"
  TMP="$(mktemp -d)"
  curl -fL --retry 3 "$URL" -o "$TMP/srb.tar"
  tar -xf "$TMP/srb.tar" -C "$TMP"
  BIN="$(find "$TMP" -type f -name 'SRBMiner-MULTI' | head -1)"
  [ -n "$BIN" ] || { echo "压缩包里没找到 SRBMiner-MULTI"; exit 1; }
  systemctl stop minerhub-agent 2>/dev/null || true
  cp -rf "$(dirname "$BIN")"/. "$DIR/srbminer/"
  chmod +x "$DIR/srbminer/SRBMiner-MULTI"
  rm -rf "$TMP"
fi

echo "==> [3/5] 安装 Agent"
curl -fsSL "$SERVER/dl/agent.py" -o "$DIR/agent.py"
python3 - "$DIR/agent.json" "$SERVER" "$FARM_KEY" "$RIG" <<'PY'
import json, sys, os
p, server, key, rig = sys.argv[1:5]
c = {}
if os.path.exists(p):
    try: c = json.load(open(p))
    except Exception: c = {}
c.update({"server": server, "farm_key": key, "rig_id": rig})
c.setdefault("miner_path", "/opt/minerhub/srbminer/SRBMiner-MULTI")
c.setdefault("api_port", 21550)
c.setdefault("interval", 10)
json.dump(c, open(p, "w"), indent=2, ensure_ascii=False)
os.chmod(p, 0o600)
PY

echo "==> [4/5] 注册 systemd 服务（开机自启）"
cat > /etc/systemd/system/minerhub-agent.service <<'EOF'
[Unit]
Description=MinerHub Agent (SRBMiner manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/minerhub/agent.py
Restart=always
RestartSec=5
User=root
LimitNOFILE=65535
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable minerhub-agent >/dev/null 2>&1
systemctl restart minerhub-agent

echo "==> [5/5] 系统调优：禁止休眠、开启 sysrq（卡死时强制重启用）"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
echo "kernel.sysrq=1" > /etc/sysctl.d/99-minerhub.conf
sysctl -q -p /etc/sysctl.d/99-minerhub.conf || true

echo
echo "✅ 安装完成：矿机 [$RIG] 已连接面板 $SERVER"
echo "   查看 Agent 日志： journalctl -u minerhub-agent -f"
echo "   查看矿工日志：   tail -f /var/log/minerhub/miner.log"
echo "   下一步：在面板里给这台矿机分配【飞行表】和【超频】，再点【启动挖矿】"
