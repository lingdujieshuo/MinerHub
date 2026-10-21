<div align="center">

# ⛏️ MinerHub

**Self-hosted mining farm dashboard · a free HiveOS alternative**

Hashrate & temperature monitoring · Overclocking · Scheduled power on/off · Remote reboot/shutdown · Watchdog · Telegram alerts & bot

[中文](README.md) · [English](README_EN.md)

By **LINGDU (零度解说)** · Blog <https://www.freedidi.com> · YouTube <https://www.youtube.com/@lingdujieshuo>

</div>

---

![Rigs overview](docs/images/01-rigs.png)

> The dashboard UI is currently in Simplified Chinese; this guide names each screen in English.

## Contents

- [Why MinerHub](#why-minerhub)
- [Features](#features)
- [Screenshots](#screenshots)
- [How it works](#how-it-works)
- [Step 1: Deploy the dashboard](#step-1-deploy-the-dashboard)
- [Step 2: Connect your rigs](#step-2-connect-your-rigs)
  - [Option A: MinerHub OS USB image (recommended)](#option-a-minerhub-os-usb-image-recommended)
  - [Option B: Install the agent on an existing Ubuntu](#option-b-install-the-agent-on-an-existing-ubuntu)
- [Step 3: Start mining](#step-3-start-mining)
- [User guide](#user-guide)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)
- [License & disclaimer](#license--disclaimer)

## Why MinerHub

HiveOS started charging extra, while a small farm only needs a handful of things: see hashrate and temperatures, overclock, power rigs on and off on a schedule, reboot them remotely when something breaks, and get notified.

MinerHub is **one Python file for the dashboard and one Python file for the rig agent**. You run it on your own server, so all data stays with you: no subscription and no third party in between.

- The dashboard and agent use **only the Python 3 standard library**: no pip, no Docker, no database server (everything lives in a single SQLite file).
- For rigs there is **MinerHub OS**, a minimal Ubuntu Server 24.04 with the NVIDIA driver, SRBMiner-MULTI and the agent built in. Flash it to a USB stick and boot.
- Out of the box it mines **Quantus (algorithm `quantus`)** and **Pearl (algorithm `pearlhash`)** with SRBMiner-MULTI. Any other algorithm supported by SRBMiner can be entered in a flight sheet.

## Features

| Area | What you get |
|---|---|
| **Monitoring** | Farm / rig / per-GPU hashrate, temperature, fan, power, core & memory clocks, efficiency, accepted/rejected shares, miner uptime; 24 h / 7 day charts; works well on phones |
| **Flight sheets** | Save "coin + pool + wallet" as a template and assign it to many rigs; `%RIG%` is replaced by the rig name; built-in templates for Quantus · Kryptex, Quantus · LuckyPool and Pearl · Kryptex |
| **Overclocking** | Locked core/memory clocks, core/memory offsets, power limit, fan; **HiveOS-style per-GPU values** (`1680 1560 1560 …`); works headless (applied via NVML) |
| **Remote control** | Start/stop mining, restart miner, reboot, shut down, Wake-on-LAN; bulk actions on selected rigs; live miner log |
| **Scheduler** | Power on, power off, start/stop mining or reboot at set times on chosen weekdays; one-click "daytime mining" template; power-on uses the motherboard RTC alarm plus WOL as a fallback |
| **Watchdog** | Restarts a crashed miner; restarts the miner after N minutes of zero hashrate; reboots the system if that keeps failing; stops on over-temperature and resumes when cool; detects lost GPUs; forces a reboot via sysrq if the system hangs |
| **Telegram** | Unexpected offline, back online, over-temperature, lost GPU, watchdog actions, hashrate drop, scheduler results, daily report; **bot commands**: `/status` `/rig` `/report` `/mine` `/stop` `/restart` `/reboot` `/shutdown` `/wake` `/mute` |
| **Security** | Secret URL suffix (security entrance) against scanning and brute force; login rate limiting; rigs authenticate with a Farm Key |
| **Offline tolerance** | If the dashboard or network goes down, rigs keep mining with their last configuration |

## Screenshots

| Flight sheet | Overclocking (per-GPU values) |
|---|---|
| ![Flight sheet](docs/images/02-flight-sheet.png) | ![Overclocking](docs/images/03-overclock.png) |

| Scheduler | Event log |
|---|---|
| ![Scheduler](docs/images/04-schedule.png) | ![Events](docs/images/05-events.png) |

| Telegram notifications / bot | Mobile |
|---|---|
| ![Telegram](docs/images/06-telegram.png) | <img src="docs/images/07-mobile.png" width="300"> |

## How it works

```
┌──────────────────────┐        HTTP(S)          ┌────────────────────────────┐
│  MinerHub dashboard   │ ◀── heartbeat/stats ─── │  Rig 1: agent + SRBMiner    │
│  server.py + SQLite   │ ─── config/commands ──▶ │  Rig 2: agent + SRBMiner    │
│  (LAN box or VPS)     │                         │  … any number of rigs       │
└──────────┬───────────┘                         └────────────────────────────┘
           │ browser / phone                     ▲
           ▼                                     │ RTC alarm wake-up / Wake-on-LAN
          you  ──────── Telegram bot ─────────────┘
```

- The **agent** on each rig sends a heartbeat every 10 seconds with hashrate and sensor data, and picks up its flight sheet, overclock profile and pending commands.
- Every connection is **started by the rig**, so rigs need no public IP and no open ports.
- The agent starts and supervises the miner and runs the watchdog, thermal protection and scheduled shutdowns locally.

## Step 1: Deploy the dashboard

Any Ubuntu / Debian machine works: a small LAN box, one of your rigs, or a VPS. It only needs Python 3 and uses very few resources.

```bash
# 1. Get the code
git clone https://github.com/lingdujieshuo/MinerHub.git
cd MinerHub/server

# 2. Install (default port 8800; for another port: sudo bash install-server.sh 9000)
sudo bash install-server.sh
```

When it finishes, the installer prints:

- **The admin password.** Change it under Settings after logging in.
- **The dashboard URL with its security suffix**, e.g. `http://192.168.1.10:8800/a7k2m9x4q1/`. Bookmark it. The bare URL without the suffix returns 404, which protects against scanning and brute force.
- **The rig install command**, used in Option B.

Installed files:

| Path | Purpose |
|---|---|
| `/opt/minerhub-server/server.py`, `static/` | Dashboard program |
| `/opt/minerhub-server/data/minerhub.db` | **All data** (SQLite). Back up this one file |
| systemd service `minerhub-server` | Starts on boot; logs via `journalctl -u minerhub-server -f` |

Maintenance:

```bash
# Reset the admin password
sudo MINERHUB_DATA=/opt/minerhub-server/data python3 /opt/minerhub-server/server.py passwd NEW_PASSWORD
# Change the security suffix ("off" disables it)
sudo MINERHUB_DATA=/opt/minerhub-server/data python3 /opt/minerhub-server/server.py entrance NEW_SUFFIX
# Upgrade: pull the latest code and run the installer again (data and password are kept)
cd MinerHub && git pull && sudo bash server/install-server.sh
```

### Running it on a public VPS (optional)

1. Put a reverse proxy (Nginx, 1Panel, aaPanel …) in front of `127.0.0.1:8800` with HTTPS.
2. In the dashboard, set **Settings → Public URL** to `https://your-domain`. The rig install command then uses that address.
3. Do **not** expose port 8800 directly; open only 443.

## Step 2: Connect your rigs

Log in and copy the **Farm Key** from the Settings page. Rigs use this key to join your dashboard.

### Option A: MinerHub OS USB image (recommended)

MinerHub OS is a minimal Ubuntu Server 24.04 that **boots straight into mining from a USB stick**:

- NVIDIA driver (precompiled kernel modules, works on first boot), SRBMiner-MULTI and the MinerHub agent are built in.
- Boots on Legacy BIOS and UEFI; any USB stick of 8 GB or more; the root partition grows to fill the stick on first boot.
- Tuned for USB sticks: logs and temp files in RAM, no swap, no automatic updates, kernel and driver versions pinned, automatic filesystem repair after power loss.

**1) Build the image**

The image is about 1.5 GB, so it is not stored in this repository; build it once yourself. You need an Ubuntu 22.04 / 24.04 x86_64 machine (VM, VPS or any Linux box) with about 15 GB free:

```bash
git clone https://github.com/lingdujieshuo/MinerHub.git
cd MinerHub/os
sudo SERVER=http://your-dashboard:8800 FARM_KEY=yourFarmKey bash build-image.sh
# Output: os/out/minerhub-os-DATE.img.xz
```

> `SERVER` and `FARM_KEY` are optional here; you can set them in the config file after flashing.
> More build options are in [os/README_EN.md](os/README_EN.md).

**2) Flash it**

Use [balenaEtcher](https://etcher.balena.io/) (it accepts `.img.xz` directly) or Rufus in DD image mode.

**3) Edit the config**

After flashing, a drive named **MINERHUB** appears on your computer. Open `minerhub.txt` in a text editor:

```ini
# Dashboard address reachable from the rig (WITHOUT the security suffix)
SERVER=http://192.168.1.10:8800
# Farm Key from the dashboard Settings page
FARM_KEY=yourFarmKey
# Rig name: empty = rig-<last 6 hex digits of the NIC MAC>
RIG_NAME=
# Console / SSH password for user "miner" - change the default
PASSWORD=minerhub
# Empty = DHCP (recommended)
STATIC_IP=
```

> Put comments on their own line, never after a value.

**4) Boot the rig from the stick**

Select the USB stick as the boot device in the BIOS. About a minute later, the rig shows up under **Rigs**.

> 💡 You can flash the same config onto as many sticks as you like; names are generated from the NIC MAC, so they never collide.
> ⚠️ After **swapping the motherboard** the MAC changes and the rig reappears under a new name. Set `RIG_NAME=` on each stick to keep a stable name.

**Recommended BIOS settings:**

- Disable Secure Boot; put the USB stick first in the boot order.
- On multi-GPU boards enable **Above 4G Decoding**. If GPUs go missing or the board hangs at POST, set the PCIe link speed to **Gen1 / Gen2**.
- **Restore on AC Power Loss = Power On**, so rigs come back after a power cut.
- For scheduled power-on: enable **Wake on RTC / Resume by RTC Alarm** (By OS) and **Wake on LAN**.

### Option B: Install the agent on an existing Ubuntu

Requirements: Ubuntu Server 22.04 / 24.04 with a working NVIDIA driver (`nvidia-smi` prints your GPUs). If the driver is missing, run `sudo ubuntu-drivers install && sudo reboot` first.

Copy the install command from **Settings** and run it on each rig. The rig name at the end is optional:

```bash
curl -fsSL http://your-dashboard:8800/dl/install-agent.sh | sudo bash -s -- http://your-dashboard:8800 <FARM_KEY> rig-01
```

The script installs dependencies, downloads the latest SRBMiner, installs the agent, enables it on boot, disables sleep and enables sysrq. The rig appears in the dashboard a few seconds later.

To upgrade SRBMiner, run the same command with `SRB_UPDATE=1` in front.

## Step 3: Start mining

1. **Create a flight sheet**: **Flight sheets → New**, click a quick template, enter your **wallet address** and save.
2. **Create an overclock profile** (optional): **Overclock → New**. See the overclocking notes below.
3. **Assign them**: open a rig's **Details → Config** and pick the flight sheet and overclock profile. The dashboard previews the final miner command line.
4. Click **Start mining**. Per-GPU hashrate appears after about 10 seconds.

## User guide

### Flight sheets

| Field | Meaning |
|---|---|
| Algorithm | `quantus` or `pearlhash` (or any other SRBMiner algorithm) |
| Pool | `host:port` |
| Wallet | Your address; `address.%RIG%` works, and `%RIG%` becomes the rig name |
| Worker | Defaults to `%RIG%`; leave it empty if the pool wants the worker appended to the wallet |
| TLS | Tick for SSL pool ports |
| Extra arguments | Appended to the miner command line as-is, e.g. `--gpu-intensity 30` |

### Overclocking

- **One number** applies to every GPU in the rig.
- **Several numbers** (separated by spaces or commas) set each GPU in PCI slot order, e.g. `1680 1560 1560 1560` for a 3060 followed by 3060 Tis. If there are fewer values than GPUs, the last value is repeated.
- Empty = leave that setting alone.
- On Linux, **locked clocks + power limit** are more reliable than offsets. Keep "reset to defaults before starting the miner" ticked so old settings don't linger.
- Changes are applied automatically: the miner restarts by itself.

Starting points (RTX 30 series on Pearl; tune for stability on your own rigs):

| GPU | Core lock | Memory lock | Power limit |
|---|---|---|---|
| RTX 3060 | 1680 | 5001 | not set |
| RTX 3060 Ti | 1560 | 5001 | not set |
| RTX 3070 | 1560 | 5001 | not set |

### Scheduler

- Click **"Template: daytime mining (power on + off)"** to create a daily power-on and power-off pair. This suits time-of-use electricity tariffs and solar power.
- Actions: power on, power off, start mining, stop mining, restart miner, reboot. Target all rigs or a single rig, on any weekdays.
- **How scheduled power-on works:** a powered-off rig can't run the agent, so there are two layers. ① Before shutting down, the agent writes the next power-on time into the motherboard RTC alarm, which works even without network. ② At that time, the dashboard asks another online rig on the same LAN to send a Wake-on-LAN packet as a fallback. RTC wake and WOL must be enabled in the BIOS.
- The time zone is set under Settings; schedules run in that time zone.

### Watchdog and protection (Settings page)

| Setting | Effect |
|---|---|
| Over-temp stop °C / resume °C | Mining pauses when any GPU exceeds the stop temperature and resumes once it cools to the resume temperature |
| Restart miner after N minutes of zero hashrate | Recovers from a hung miner |
| Reboot after N miner restarts within 1 hour | Escalates to a system reboot (at most 3 per day) |
| Lost GPU detection | Alerts when a GPU has had no hashrate for 5 minutes (can be pushed to Telegram) |

### Telegram notifications and bot

1. In Telegram, message **@BotFather**, send `/newbot` and copy the bot token.
2. Open your bot and press **Start** (or add it to a group and send `/start` there).
3. In **Settings → Telegram**, paste the bot token, click **Get Chat ID**, save, then click **Send test**.
4. Tick the notification types you want. To control rigs from Telegram, enable **Allow bot commands**.

| Command | Action |
|---|---|
| `/status` | Farm overview |
| `/rig NAME` | One rig in detail |
| `/report` | Send the daily report now |
| `/mine` `/stop` `/restart` | Start mining / stop mining / restart miner |
| `/reboot` `/shutdown` | Reboot / shut down (asks for confirmation) |
| `/wake NAME or all` | Wake-on-LAN |
| `/mute HOURS` `/unmute` | Silence alerts / resume alerts |

The bot only answers the chat ID you configured. If your server cannot reach Telegram, put a reverse-proxy address in **API URL**.

### MinerHub OS commands (SSH user `miner`)

| Command | Action |
|---|---|
| `mh status` | Rig name, IP, dashboard, agent/miner status, per-GPU temperature and power |
| `mh log` | Follow the miner log |
| `mh agent` | Agent log |
| `mh config` | Edit the USB config and apply it immediately |
| `mh update` | Download the latest agent from the dashboard and restart it |
| `mh update-srb [URL]` | Upgrade SRBMiner |

## Security

- **The Farm Key is equivalent to root on your rigs** (it can send commands). Keep it private. If it leaks, regenerate it under Settings and reconfigure the rigs.
- Keep the **security suffix** enabled and change the initial admin password.
- On a public server, use an HTTPS reverse proxy and don't expose port 8800.
- Change the default MinerHub OS password (`PASSWORD=` in `minerhub.txt`).
- Never post your Telegram bot token publicly.

## Troubleshooting

**Rig doesn't come online / shows offline**
1. Look for a rig with a **new name** under Rigs. Swapping the motherboard or NIC changes the name (see above).
2. Connect a monitor. After boot the console shows the rig name, IP and dashboard address. If it stops in the BIOS, check the boot order and the multi-GPU BIOS settings.
3. Over SSH, run `mh status`, `mh agent` or `journalctl -u minerhub-agent -n 50`.

**Missing GPUs / GPUs dropping out**
Run `nvidia-smi` and count the GPUs. Missing GPUs are usually a riser, cable or power issue. Boot with fewer cards, then add them back one at a time to find the faulty one.

**Per-GPU hashrate shows "—"**
In **Details → Miner log**, expand "SRBMiner API raw JSON" to check that the miner API is responding.

**Logs**

```bash
journalctl -u minerhub-agent -f          # agent on the rig
tail -f /var/log/minerhub/miner.log      # miner
journalctl -u minerhub-server -f         # dashboard
```

## Repository layout

```
MinerHub/
├── server/                   Dashboard (copy to the dashboard server)
│   ├── server.py             Backend: API, scheduler, watchdog policy, Telegram
│   ├── install-server.sh     One-step install (/opt/minerhub-server + systemd)
│   └── static/
│       ├── index.html        Web UI (single file, mobile friendly)
│       └── dl/               Files rigs download from the dashboard
│           ├── agent.py          Rig agent
│           └── install-agent.sh  Rig install script
├── os/                       MinerHub OS image builder
│   ├── build-image.sh        Build script (outputs os/out/*.img.xz)
│   ├── config-partition/     Template for the USB MINERHUB config partition
│   └── rootfs/               Files baked into the OS (boot config, systemd units, mh tool)
└── docs/images/              Screenshots
```

## License & disclaimer

Released under the [MIT License](LICENSE).

- MinerHub **does not include any mining software**. SRBMiner-MULTI is published separately by its author, downloaded from its official GitHub at install time, and charges its own developer fee.
- Mining involves electricity costs, hardware wear and price risk, and overclocking can make systems unstable. Make sure mining is legal where you live. Use at your own risk.

---

<div align="center">

If MinerHub helps you, please give it a ⭐ Star and follow the author:

📝 Blog: **[freedidi.com](https://www.freedidi.com)** · 📺 YouTube: **[零度解说 @lingdujieshuo](https://www.youtube.com/@lingdujieshuo)**

</div>
