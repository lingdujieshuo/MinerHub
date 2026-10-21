# MinerHub OS — a flash-and-boot mining system on a USB stick

[中文](README.md) · English

A minimal Ubuntu Server 24.04 LTS with:

- NVIDIA server driver (precompiled kernel modules, works on first boot without DKMS)
- SRBMiner-MULTI
- MinerHub agent (connects to your dashboard on boot)

Boots on Legacy BIOS and UEFI, fits any USB stick of 8 GB or more, and grows to fill the stick on first boot.

## Flashing and first boot

1. Flash the image with **balenaEtcher** (accepts `.img.xz` directly) or **Rufus** (DD image mode).
2. A drive named **MINERHUB** appears on your computer. Open `minerhub.txt` in a text editor:
   - `SERVER=` dashboard address, e.g. `http://192.168.1.10:8800` (without the security suffix)
   - `FARM_KEY=` the Farm Key from the dashboard Settings page
   - `PASSWORD=` SSH password; change the default
   - Put comments on their own line, never after a value.
3. Plug the stick into the rig and boot from it. The rig appears in the dashboard after about a minute.
4. Assign a flight sheet and overclock profile in the dashboard, then click **Start mining**.

Notes:

- **One config can be flashed to any number of sticks.** The rig name defaults to `rig-<last 6 hex digits of the NIC MAC>`, so names never collide. Set `RIG_NAME=` for a custom name. Recommended: a fixed name survives a motherboard swap.
- **You can change the config any time.** Edit `minerhub.txt` on another computer and plug it back, or run `mh config` on the rig.
- **Which stick belongs to which rig?** See `rig-info.txt` on the stick; the rig writes it back on boot.

## Login and commands

The console shows the IP and rig name. Log in on the console or via SSH as `miner` with the `PASSWORD` from `minerhub.txt` (default `minerhub`).

| Command | Action |
|---|---|
| `mh status` | Rig name, IP, dashboard, agent / miner status, per-GPU temperature and power |
| `mh log` | Follow the miner log |
| `mh agent` | Agent log |
| `mh config` | Edit the USB config and apply it immediately |
| `mh update` | Download the latest agent.py from the dashboard and restart |
| `mh update-srb [URL]` | Upgrade SRBMiner (latest GitHub release if no URL) |

The boot menu has a second entry, **"no NVIDIA driver / rescue"**, for troubleshooting a broken driver or a hang at boot.

## USB-friendly tuning

- System logs, miner logs and `/tmp` live in RAM, so the stick is barely written to.
- Root mounted with `noatime` and a 60 s commit interval; no swap.
- Automatic updates disabled; kernel and driver versions pinned so background upgrades cannot break the driver.
- Filesystem repaired automatically on boot (`fsck.repair=yes`), so power cuts are safe.
- Kernel parameters `pci=noaer pcie_aspm=off` reduce PCIe errors and dropped GPUs caused by risers.

## Building the image yourself

Rebuild to change the driver version, upgrade SRBMiner, or pre-fill the dashboard address. You need an Ubuntu 22.04 / 24.04 x86_64 machine (VM or VPS is fine) with about 15 GB free:

```bash
cd MinerHub/os
sudo SERVER=http://192.168.1.10:8800 FARM_KEY=yourFarmKey bash build-image.sh
# Output: os/out/minerhub-os-DATE.img.xz
```

Optional environment variables:

| Variable | Purpose |
|---|---|
| `NV=580` | NVIDIA driver major version (default: latest server branch) |
| `SRB_URL=...` | SRBMiner archive to bundle |
| `PASSWORD` | Default login password |
| `TIMEZONE` | Time zone |
| `IMG_SIZE=6G` | Image size |
| `MIRROR` | Ubuntu mirror, e.g. `http://mirrors.aliyun.com/ubuntu` in mainland China |

## Recommended BIOS settings

- Boot from the USB stick; CSM/Legacy or UEFI both work; disable Secure Boot.
- Multi-GPU boards: enable **Above 4G Decoding**; if GPUs go missing, set the PCIe link speed to Gen1/Gen2.
- Scheduled power-on: enable **Wake on RTC / Resume by RTC Alarm** (By OS) and **Wake on LAN**.
- After a power cut: **Restore on AC Power Loss = Power On**.
