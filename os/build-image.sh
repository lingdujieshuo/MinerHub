#!/usr/bin/env bash
# =====================================================================
#  MinerHub OS 镜像构建脚本
#  Ubuntu Server 24.04 LTS (noble) 精简版 + NVIDIA 驱动 + SRBMiner-MULTI + MinerHub Agent
#
#  在任意一台 Ubuntu 22.04 / 24.04 x86_64 机器（物理机 / 虚拟机 / VPS）上以 root 运行：
#    sudo SERVER=http://192.168.1.10:8800 FARM_KEY=你的FarmKey bash build-image.sh
#  产物：os/out/minerhub-os-日期.img.xz  —— 用 balenaEtcher / Rufus 直接刻录到 U 盘
#
#  可选环境变量：
#    SERVER / FARM_KEY   预填到 U 盘配置文件（不填也行，刻录后在 Windows 上改 minerhub.txt）
#    PASSWORD            miner 用户默认密码（默认 minerhub）
#    TIMEZONE            默认 Asia/Shanghai
#    NV                  NVIDIA 驱动大版本，例如 580；默认 auto = 自动选源里最新的 server 版
#    SRB_URL             SRBMiner Linux 压缩包地址（默认 GitHub 最新版）
#    IMG_SIZE            镜像大小（默认 6G，首次开机会自动扩到整个 U 盘）
#    MIRROR              Ubuntu 软件源（默认 http://archive.ubuntu.com/ubuntu）
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/.." && pwd)"
OUT="${OUT:-$HERE/out}"
WORK="${WORK:-$HERE/work}"
IMG_SIZE="${IMG_SIZE:-6G}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"
SEC_MIRROR="${SEC_MIRROR:-http://security.ubuntu.com/ubuntu}"
NV="${NV:-auto}"
SRB_URL="${SRB_URL:-}"
SERVER="${SERVER:-}"; FARM_KEY="${FARM_KEY:-}"
PASSWORD="${PASSWORD:-minerhub}"; TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
SUITE=noble
DATE="$(date +%Y%m%d)"
IMG="$WORK/minerhub-os-$DATE.img"
MNT="$WORK/mnt"
LOOP=""

say() { echo -e "\n\033[1;33m==> $*\033[0m"; }
die() { echo -e "\033[1;31m错误: $*\033[0m" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请用 root / sudo 运行"
[ "$(uname -m)" = x86_64 ] || die "只支持在 x86_64 机器上构建"
[ -f "$PROJ/server/static/dl/agent.py" ] || die "找不到 $PROJ/server/static/dl/agent.py（os 目录要放在 minerhub 项目里）"

# ------------------------------------------------------------------ 依赖
say "[1/9] 检查构建工具"
NEED="debootstrap sgdisk mkfs.vfat mkfs.ext4 partx losetup xz curl e2fsck mcopy zerofree"
MISSING=""
for c in $NEED; do command -v $c >/dev/null || MISSING=1; done
if [ -n "$MISSING" ]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debootstrap gdisk dosfstools e2fsprogs util-linux xz-utils curl mtools zerofree zstd >/dev/null
fi
# 老版本 debootstrap（Ubuntu 22.04 / Debian 12 自带）不认识 noble：noble 与所有 Ubuntu 版本共用 gutsy 脚本，补个链接即可
DS=/usr/share/debootstrap/scripts
if [ ! -e "$DS/noble" ]; then
  [ -e "$DS/gutsy" ] || die "debootstrap 缺少 gutsy 脚本，无法构建"
  ln -s gutsy "$DS/noble"
  echo "   已为旧版 debootstrap 添加 noble 支持"
fi
command -v zstd >/dev/null || { apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd >/dev/null; }

# ------------------------------------------------------------------ 清理
cleanup() {
  set +e
  for m in dev/pts dev proc sys run; do mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"; done
  mountpoint -q "$MNT" && umount -l "$MNT"
  if [ -n "$LOOP" ]; then partx -d "$LOOP" 2>/dev/null; losetup -d "$LOOP" 2>/dev/null; fi
}
trap cleanup EXIT

mkdir -p "$WORK" "$OUT" "$MNT"
rm -f "$IMG"

# ------------------------------------------------------------------ 分区
say "[2/9] 创建 $IMG_SIZE 镜像并分区（BIOS + UEFI 双启动）"
truncate -s "$IMG_SIZE" "$IMG"
sgdisk -Z "$IMG" >/dev/null
sgdisk -n1:2048:+1M   -t1:ef02 -c1:bios \
       -n2:0:+200M    -t2:ef00 -c2:EFI \
       -n3:0:+64M     -t3:0700 -c3:MINERHUB \
       -n4:0:0        -t4:8300 -c4:root "$IMG" >/dev/null
# 保护性 MBR 打上活动标记：部分老主板 Legacy 模式不认没有活动分区的 GPT 盘
printf '\x80' | dd of="$IMG" bs=1 seek=446 conv=notrunc status=none

LOOP="$(losetup -f --show "$IMG")"
partx -a "$LOOP" 2>/dev/null || partx -u "$LOOP"
sleep 1
# 容器 / 无 udev 环境下手动建分区设备节点
B="$(basename "$LOOP")"
awk -v b="$B" '$4 ~ "^"b"p[0-9]+$" {print $1, $2, $4}' /proc/partitions | while read -r ma mi n; do
  [ -e "/dev/$n" ] || mknod "/dev/$n" b "$ma" "$mi"
done
for i in 2 3 4; do [ -b "${LOOP}p$i" ] || die "分区设备 ${LOOP}p$i 不存在"; done

say "[3/9] 格式化"
mkfs.vfat -F 32 -n MH-EFI "${LOOP}p2" >/dev/null
mkfs.vfat -n MINERHUB "${LOOP}p3" >/dev/null
EXT4_OPTS="-O ^orphan_file,^metadata_csum_seed"
mkfs.ext4 -q -F -L mh-root $EXT4_OPTS "${LOOP}p4" 2>/dev/null || mkfs.ext4 -q -F -L mh-root "${LOOP}p4"

mount "${LOOP}p4" "$MNT"
mkdir -p "$MNT/boot/efi"   # 构建时先当普通目录用，最后用 mtools 拷进 FAT 分区（不需要挂载 vfat）

# ------------------------------------------------------------------ 基础系统
say "[4/9] debootstrap Ubuntu 24.04 基础系统（需要几分钟）"
debootstrap --arch=amd64 --variant=minbase --components=main,restricted,universe,multiverse \
  --include=ca-certificates,gnupg "$SUITE" "$MNT" "$MIRROR"

cat > "$MNT/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main restricted universe multiverse
deb $MIRROR $SUITE-updates main restricted universe multiverse
deb $SEC_MIRROR $SUITE-security main restricted universe multiverse
EOF
cat > "$MNT/etc/apt/apt.conf.d/90minerhub" <<'EOF'
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Languages "none";
EOF
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"; chmod +x "$MNT/usr/sbin/policy-rc.d"
cp -L /etc/resolv.conf "$MNT/etc/resolv.conf"
echo "minerhub" > "$MNT/etc/hostname"

for m in dev dev/pts proc sys; do mkdir -p "$MNT/$m"; mount --bind "/$m" "$MNT/$m"; done
# /run 不能绑定宿主机的：否则包安装脚本会以为在真机上运行，去操作宿主机的 systemd 而失败
mkdir -p "$MNT/run"; mount -t tmpfs -o mode=755 tmpfs "$MNT/run"
CH() { chroot "$MNT" /usr/bin/env DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 "$@"; }

say "[5/9] 安装内核、NVIDIA 驱动和系统组件"
CH apt-get update -qq
if [ "$NV" = auto ]; then
  NV="$(CH apt-cache pkgnames linux-modules-nvidia- | sed -n 's/^linux-modules-nvidia-\([0-9]\+\)-server-generic$/\1/p' | sort -n | while read -r v; do
          CH apt-cache show "nvidia-headless-no-dkms-$v-server" >/dev/null 2>&1 && CH apt-cache show "nvidia-utils-$v-server" >/dev/null 2>&1 && echo "$v"; done | tail -1)"
  [ -n "$NV" ] || die "源里找不到可用的 NVIDIA server 驱动"
fi
echo "   NVIDIA 驱动版本: $NV-server（预编译内核模块，无需 DKMS）"
PKGS="linux-image-generic linux-modules-nvidia-$NV-server-generic nvidia-headless-no-dkms-$NV-server nvidia-utils-$NV-server
  initramfs-tools systemd-sysv systemd-resolved systemd-timesyncd dbus udev kmod
  openssh-server sudo python3 tzdata curl ca-certificates util-linux ethtool iproute2 iputils-ping
  pciutils lm-sensors nano less htop gdisk cloud-guest-utils dosfstools e2fsprogs xz-utils
  grub-pc-bin grub-efi-amd64-bin grub2-common"
APTLOG="$WORK/apt-install.log"
echo "   安装日志: $APTLOG"
CH apt-get install -y -q apt-utils >>"$APTLOG" 2>&1 || true
if ! CH apt-get install -y -q $PKGS >>"$APTLOG" 2>&1; then
  echo "   第一次安装有包配置失败，自动修复重试..."
  CH dpkg --configure -a >>"$APTLOG" 2>&1 || true
  if ! CH apt-get install -y -q -f $PKGS >>"$APTLOG" 2>&1; then
    echo "----------------------------------------------------------------"
    grep -E "^(E:|dpkg: error|Errors were|update-initramfs|W: )|error|failed|No space" "$APTLOG" | tail -n 40
    echo "----------------------------------------------------------------"
    tail -n 30 "$APTLOG"
    echo "----------------------------------------------------------------"
    df -h "$WORK" "$MNT" 2>/dev/null
    die "软件包安装失败，请把上面两条横线之间的内容发给我（完整日志: $APTLOG）"
  fi
fi
KVER="$(ls "$MNT/lib/modules" | sort -V | tail -1)"
echo "   内核: $KVER"

# ------------------------------------------------------------------ 系统配置
say "[6/9] 写入 MinerHub 配置"
cp -a "$HERE/rootfs/." "$MNT/"
chmod 755 "$MNT/usr/local/sbin/minerhub-boot" "$MNT/usr/local/bin/mh"
chmod 440 "$MNT/etc/sudoers.d/miner"

mkdir -p "$MNT/opt/minerhub/srbminer" "$MNT/mnt/minerhub" "$MNT/var/log/minerhub" "$MNT/var/lib/minerhub"
cp "$PROJ/server/static/dl/agent.py" "$MNT/opt/minerhub/agent.py"

echo "   下载 SRBMiner-MULTI"
if [ -z "$SRB_URL" ]; then
  SRB_URL="$(curl -fsSL https://api.github.com/repos/doktor83/SRBMiner-Multi/releases/latest 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(next(a["browser_download_url"] for a in d["assets"] if "Linux" in a["name"] and a["name"].endswith((".tar.gz", ".tar.xz"))))
except Exception:
    pass' || true)"
  [ -n "$SRB_URL" ] || SRB_URL="https://github.com/doktor83/SRBMiner-Multi/releases/download/3.6.6/SRBMiner-Multi-3-6-6-Linux.tar.gz"
fi
echo "   $SRB_URL"
SRBTMP="$WORK/srb"; rm -rf "$SRBTMP"; mkdir -p "$SRBTMP"
curl -fL --retry 3 "$SRB_URL" -o "$SRBTMP/srb.tar"
tar -xf "$SRBTMP/srb.tar" -C "$SRBTMP"
SRBBIN="$(find "$SRBTMP" -type f -name SRBMiner-MULTI | head -1)"
[ -n "$SRBBIN" ] || die "SRBMiner 压缩包里没有 SRBMiner-MULTI"
cp -rf "$(dirname "$SRBBIN")"/. "$MNT/opt/minerhub/srbminer/"
chmod +x "$MNT/opt/minerhub/srbminer/SRBMiner-MULTI"
basename "$SRB_URL" > "$MNT/opt/minerhub/srbminer/VERSION.txt"
if [ -f "$PROJ/server/static/dl/ldminer" ]; then
  mkdir -p "$MNT/opt/minerhub/ldminer"
  install -m 755 "$PROJ/server/static/dl/ldminer" "$MNT/opt/minerhub/ldminer/ldminer"
  echo "   已加入 LDMiner（0 抽水 Quantus 矿工）"
fi

cat > "$MNT/etc/fstab" <<'EOF'
# MinerHub OS —— 针对 U 盘优化：noatime、长提交间隔、日志和临时文件放内存
LABEL=mh-root   /                  ext4   defaults,noatime,commit=60,errors=remount-ro  0 1
LABEL=MH-EFI    /boot/efi          vfat   defaults,noatime,umask=0077,nofail  0 2
LABEL=MINERHUB  /mnt/minerhub      vfat   defaults,noatime,umask=0022,nofail  0 0
tmpfs           /tmp               tmpfs  defaults,size=256M  0 0
tmpfs           /var/log/minerhub  tmpfs  defaults,size=64M   0 0
EOF

cat > "$MNT/etc/default/locale" <<'EOF'
LANG=C.UTF-8
EOF
printf '0.0 0 0.0\n0\nUTC\n' > "$MNT/etc/adjtime"
ln -sf "/usr/share/zoneinfo/$TIMEZONE" "$MNT/etc/localtime"; echo "$TIMEZONE" > "$MNT/etc/timezone"
cat > "$MNT/etc/hosts" <<'EOF'
127.0.0.1	localhost
127.0.1.1	minerhub
::1	localhost ip6-localhost ip6-loopback
EOF

CH bash -c "
set -e
id miner >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,video,adm miner
echo 'miner:$PASSWORD' | chpasswd
passwd -l root >/dev/null
systemctl enable systemd-networkd systemd-resolved systemd-timesyncd ssh minerhub-boot minerhub-agent >/dev/null 2>&1
systemctl mask apt-daily.timer apt-daily-upgrade.timer motd-news.timer e2scrub_all.timer e2scrub_reap.service >/dev/null 2>&1 || true
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
systemctl set-default multi-user.target >/dev/null
apt-mark hold 'linux-image-*' 'linux-modules-*' 'linux-generic*' 'nvidia-*' 'libnvidia-*' >/dev/null 2>&1 || true
"
# 首次开机密码标记，让 minerhub-boot 知道当前密码已是默认值
printf '%s' "$(printf 'minerhub|%s' "$PASSWORD" | sha256sum | cut -d' ' -f1)" > "$MNT/var/lib/minerhub/pw.sha"
chmod 600 "$MNT/var/lib/minerhub/pw.sha"

# ------------------------------------------------------------------ 引导
say "[7/9] 生成 initramfs 并安装 GRUB（Legacy BIOS + UEFI）"
CH update-initramfs -u -k "$KVER" >/dev/null 2>&1 || CH update-initramfs -c -k "$KVER"
CH grub-install --target=i386-pc --boot-directory=/boot --modules="part_gpt ext2" "$LOOP"
# UEFI：直接生成 EFI/BOOT/BOOTX64.EFI（可移动介质标准路径，任何主板都能识别，无需写 NVRAM）
mkdir -p "$MNT/boot/grub/x86_64-efi" "$MNT/boot/efi/EFI/BOOT"
cp "$MNT"/usr/lib/grub/x86_64-efi/*.mod "$MNT"/usr/lib/grub/x86_64-efi/*.lst "$MNT/boot/grub/x86_64-efi/"
cat > "$MNT/tmp/efi-early.cfg" <<'EOF'
search --no-floppy --label mh-root --set=root
set prefix=($root)/boot/grub
configfile ($root)/boot/grub/grub.cfg
EOF
# 在 chroot 里用镜像自带的 GRUB 生成，避免宿主机 GRUB 版本不同导致不兼容
CH grub-mkimage -d /usr/lib/grub/x86_64-efi -O x86_64-efi -o /boot/efi/EFI/BOOT/BOOTX64.EFI \
  -c /tmp/efi-early.cfg -p /boot/grub \
  part_gpt part_msdos ext2 fat search search_label search_fs_uuid normal configfile linux echo test \
  all_video efi_gop efi_uga gzio minicmd ls cat
CMDLINE="root=LABEL=mh-root ro quiet loglevel=3 fsck.repair=yes consoleblank=0 pci=noaer pcie_aspm=off"
cat > "$MNT/boot/grub/grub.cfg" <<EOF
set timeout=3
set default=0
insmod part_gpt
insmod ext2
search --no-floppy --label mh-root --set=root
terminal_output console

menuentry "MinerHub OS" {
    linux /boot/vmlinuz-$KVER $CMDLINE
    initrd /boot/initrd.img-$KVER
}
menuentry "MinerHub OS (no NVIDIA driver / rescue)" {
    linux /boot/vmlinuz-$KVER $CMDLINE modprobe.blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm systemd.mask=minerhub-agent.service
    initrd /boot/initrd.img-$KVER
}
EOF

# ------------------------------------------------------------------ 清理
say "[8/9] 清理并写入配置分区"
CH apt-get clean
rm -f "$MNT/usr/sbin/policy-rc.d"
rm -rf "$MNT"/var/lib/apt/lists/* "$MNT"/var/cache/apt/*.bin "$MNT"/var/cache/debconf/*-old \
       "$MNT"/var/log/*.log "$MNT"/var/log/apt/* "$MNT"/tmp/* "$MNT"/root/.bash_history
rm -f "$MNT"/etc/ssh/ssh_host_*                      # 每根 U 盘首次开机各自生成
: > "$MNT/etc/machine-id"; rm -f "$MNT/var/lib/dbus/machine-id"   # 每台机器首次开机生成唯一 ID
ln -sf ../run/systemd/resolve/stub-resolv.conf "$MNT/etc/resolv.conf"
echo "MinerHub OS $DATE (Ubuntu 24.04, kernel $KVER, NVIDIA $NV-server, $(basename "$SRB_URL"))" > "$MNT/etc/minerhub-release"

CFGTMP="$WORK/cfgpart"; mkdir -p "$CFGTMP"; rm -f "$CFGTMP"/*
sed -e "s|__SERVER__|${SERVER:-__SERVER__}|" -e "s|__FARM_KEY__|${FARM_KEY:-__FARM_KEY__}|" \
    -e "s|__TZ__|$TIMEZONE|" -e "s|__PASSWORD__|$PASSWORD|" "$HERE/config-partition/minerhub.txt" | sed 's/$/\r/' > "$CFGTMP/minerhub.txt"
sed 's/$/\r/' "$HERE/config-partition/README.txt" > "$CFGTMP/README.txt"
export MTOOLS_SKIP_CHECK=1
mcopy -o -i "${LOOP}p3" "$CFGTMP"/* ::/
mcopy -o -s -i "${LOOP}p2" "$MNT"/boot/efi/* ::/
rm -rf "$MNT"/boot/efi/*

USED="$(df -h --output=used "$MNT" | tail -1 | tr -d ' ')"
for m in dev/pts dev proc sys run; do umount -l "$MNT/$m"; done
fstrim "$MNT" >/dev/null 2>&1 || true
umount "$MNT"
e2fsck -fy "${LOOP}p4" >/dev/null || true
zerofree "${LOOP}p4" || true          # 空闲空间清零，压缩包更小（fstrim 不生效时的兜底）
partx -d "$LOOP" 2>/dev/null || true; losetup -d "$LOOP"; LOOP=""

say "[9/9] 压缩镜像（xz，需要几分钟）"
xz -T0 -6 -f -k "$IMG"
mv -f "$IMG.xz" "$OUT/"
( cd "$OUT" && sha256sum "$(basename "$IMG").xz" > "$(basename "$IMG").xz.sha256" )
[ "${KEEP_RAW:-0}" = 1 ] && mv -f "$IMG" "$OUT/" || rm -f "$IMG"

echo
echo "✅ 构建完成（系统占用 $USED）"
ls -lh "$OUT"/minerhub-os-"$DATE"*
echo "   用 balenaEtcher 或 Rufus（DD 模式）把 .img.xz 刻录到 U 盘即可（Etcher 可直接刻 .xz）。"
