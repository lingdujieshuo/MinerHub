#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MinerHub Agent —— 运行在每台 Ubuntu 矿机上（root，systemd 托管）
项目主页 https://github.com/lingdujieshuo/MinerHub · 作者 零度解说 https://www.freedidi.com · https://www.youtube.com/@lingdujieshuo
零依赖：只用 Python 3 标准库，4G 内存的矿机也毫无压力。

职责：
  1. 管理 SRBMiner-MULTI 进程（启动 / 停止 / 崩溃自动拉起 / 配置变更自动重启）
  2. 采集 nvidia-smi + SRBMiner API 数据，每 10 秒上报面板
  3. 执行面板下发的指令：重启矿工、重启系统、关机、读日志、执行命令、WOL 唤醒其它矿机
  4. 本地看门狗：零算力重启、超温停机、掉卡告警/重启
  5. 定时开机：把面板算好的下一次开机时间写入主板 RTC 闹钟（rtcwake）
配置文件: /opt/minerhub/agent.json
"""
import json, os, re, shlex, signal, socket, subprocess, sys, threading, time
import urllib.parse, urllib.request

VERSION = "1.1.0"
BASE = "/opt/minerhub"
CONF_PATH = os.environ.get("MINERHUB_CONF", BASE + "/agent.json")
STATE_PATH = os.environ.get("MINERHUB_STATE", BASE + "/state.json")
LOG_DIR = os.environ.get("MINERHUB_LOGDIR", "/var/log/minerhub")
MINER_LOG = LOG_DIR + "/miner.log"


def load_json(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def save_json(p, o):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False)
    os.replace(tmp, p)


CONF = load_json(CONF_PATH, {})
SERVER = str(CONF.get("server", "")).rstrip("/")
FARM_KEY = CONF.get("farm_key", "")
RIG_ID = CONF.get("rig_id") or socket.gethostname()
MINER_BIN = CONF.get("miner_path", BASE + "/srbminer/SRBMiner-MULTI")
LDMINER_BIN = CONF.get("ldminer_path", BASE + "/ldminer/ldminer")
MINER_PROC_NAMES = ("SRBMiner-MULTI", "ldminer")


def miner_bin(cfg):
    fl = (cfg or {}).get("flight") or {}
    return LDMINER_BIN if str(fl.get("miner") or "").lower() == "ldminer" else MINER_BIN


def ensure_ldminer():
    """LDMiner 不在 U 盘镜像里时，从面板 /dl/ldminer 下载（面板升级后删除本地文件即可自动更新）"""
    if os.path.exists(LDMINER_BIN):
        return True
    try:
        os.makedirs(os.path.dirname(LDMINER_BIN), exist_ok=True)
        tmp = LDMINER_BIN + ".download"
        with urllib.request.urlopen(SERVER + "/dl/ldminer", timeout=120) as r, open(tmp, "wb") as f:
            f.write(r.read())
        if os.path.getsize(tmp) < 1000000:
            raise RuntimeError("文件太小，面板上可能没有放 ldminer")
        os.chmod(tmp, 0o755)
        os.replace(tmp, LDMINER_BIN)
        event("info", "已从面板下载 LDMiner")
        return True
    except Exception as e:
        event("error", "下载 LDMiner 失败（%s）：请把 ldminer 放到面板服务器 static/dl/ 目录" % e)
        return False
API_PORT = int(CONF.get("api_port", 21550))
INTERVAL = int(CONF.get("interval", 10))

EVENTS = []
EV_LOCK = threading.Lock()
SHUTTING_DOWN = False
RESTART_REQ = False


def log(msg):
    print(time.strftime("%F %T"), msg, flush=True)


def event(level, msg):
    log("[%s] %s" % (level, msg))
    with EV_LOCK:
        EVENTS.append({"level": level, "msg": msg})
        del EVENTS[:-200]


def sh(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -2, "timeout"
    except Exception as e:
        return -1, str(e)


def post(path, obj, timeout=15):
    req = urllib.request.Request(SERVER + path, data=json.dumps(obj, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json", "X-Farm-Key": FARM_KEY,
                                          "User-Agent": "MinerHub-Agent/" + VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def num(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None

# ------------------------------------------------------------------ 系统信息


def read(p, d=""):
    try:
        with open(p) as f:
            return f.read()
    except Exception:
        return d


def net_info():
    host = urllib.parse.urlparse(SERVER).hostname or "1.1.1.1"
    try:
        dst = socket.gethostbyname(host)
    except Exception:
        dst = "1.1.1.1"
    rc, out = sh(["ip", "-o", "route", "get", dst], 5)
    dev = re.search(r"\bdev (\S+)", out)
    src = re.search(r"\bsrc (\S+)", out)
    iface = dev.group(1) if dev else ""
    ip = src.group(1) if src else ""
    mac = read("/sys/class/net/%s/address" % iface).strip() if iface else ""
    brd = ""
    if iface:
        rc, out = sh(["ip", "-o", "-4", "addr", "show", "dev", iface], 5)
        m = re.search(r"\bbrd (\S+)", out)
        brd = m.group(1) if m else ""
    return {"iface": iface, "ip": ip, "mac": mac, "brd": brd}


STATIC_INFO = {}


def static_info():
    if STATIC_INFO:
        return STATIC_INFO
    osr = read("/etc/os-release")
    m = re.search(r'PRETTY_NAME="([^"]+)"', osr)
    cpu = re.search(r"model name\s*:\s*(.+)", read("/proc/cpuinfo"))
    rc, drv = sh(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], 10)
    STATIC_INFO.update({
        "os": m.group(1) if m else "Linux", "kernel": os.uname().release,
        "cpu": cpu.group(1).strip() if cpu else "", "driver": drv.strip().splitlines()[0] if rc == 0 and drv.strip() else "",
        "agent": VERSION, "hostname": socket.gethostname(),
    })
    return STATIC_INFO


def dyn_info(net):
    mem = read("/proc/meminfo")
    def mi(k):
        m = re.search(r"^%s:\s+(\d+)" % k, mem, re.M)
        return int(m.group(1)) // 1024 if m else 0
    d = dict(static_info())
    d.update({"ip": net["ip"], "mac": net["mac"], "iface": net["iface"],
              "uptime": int(num(read("/proc/uptime", "0").split()[0]) or 0),
              "load": read("/proc/loadavg", "0 0 0").split()[:3],
              "mem_total": mi("MemTotal"), "mem_avail": mi("MemAvailable")})
    disk = os.statvfs("/")
    d["disk_free_gb"] = round(disk.f_bavail * disk.f_frsize / 1e9, 1)
    return d

# ------------------------------------------------------------------ GPU / 矿工 API


GPU_Q = "index,name,pci.bus_id,temperature.gpu,fan.speed,power.draw,power.limit,clocks.sm,clocks.mem,utilization.gpu,memory.total"


def gpu_stats():
    """返回 (gpus, error, lost)"""
    rc, out = sh(["nvidia-smi", "--query-gpu=" + GPU_Q, "--format=csv,noheader,nounits"], 25)
    if rc != 0:
        lost = any(k in out for k in ("GPU is lost", "Unable to determine", "Unknown Error", "timeout"))
        return [], out.strip()[:300], lost
    gpus = []
    for line in out.strip().splitlines():
        p = [s.strip() for s in line.split(",")]
        if len(p) < 11:
            continue
        m = re.match(r"^[0-9A-Fa-f]+:([0-9A-Fa-f]{2}):", p[2])
        gpus.append({
            "i": int(p[0]), "name": p[1].replace("NVIDIA GeForce ", "").replace("NVIDIA ", ""),
            "bus": int(m.group(1), 16) if m else None, "temp": num(p[3]), "fan": num(p[4]),
            "power": num(p[5]), "plimit": num(p[6]), "core": num(p[7]), "mem": num(p[8]),
            "util": num(p[9]), "vram": num(p[10]),
        })
    return gpus, None, False


def miner_api_raw():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % API_PORT, timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def fnum(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("now", "total", "10s", "60s", "hashrate", "value"):
            if k in v:
                r = fnum(v[k])
                if r is not None:
                    return r
        return None
    return num(v)


def find_gpu_keys(obj, out, depth=0):
    """在 SRBMiner JSON 里递归找 gpu0/gpu1… 形式的单卡算力（兼容不同版本结构）"""
    if depth > 4 or not isinstance(obj, dict):
        return
    for k, v in obj.items():
        m = re.match(r"^gpu(\d+)$", str(k))
        if m and fnum(v) is not None:
            out.setdefault(int(m.group(1)), fnum(v))
        elif isinstance(v, dict):
            find_gpu_keys(v, out, depth + 1)


def parse_api(j):
    res = {"algo": None, "hs_total": None, "gpu_hs": {}, "accepted": None, "rejected": None,
           "pool": None, "version": None, "uptime": None, "bus_map": {}}
    if not isinstance(j, dict):
        return res
    res["version"] = j.get("miner_version") or j.get("version")
    res["uptime"] = fnum(j.get("mining_time") or j.get("uptime"))
    algos = j.get("algorithms") or []
    a = algos[0] if algos and isinstance(algos[0], dict) else {}
    res["algo"] = a.get("name") or a.get("algorithm")
    hr = a.get("hashrate") or {}
    g = hr.get("gpu") if isinstance(hr, dict) else None
    find_gpu_keys(g if isinstance(g, dict) else hr, res["gpu_hs"])
    if isinstance(g, dict) and fnum(g.get("total")) is not None:
        res["hs_total"] = fnum(g.get("total"))
    elif res["gpu_hs"]:
        res["hs_total"] = sum(v or 0 for v in res["gpu_hs"].values())
    elif isinstance(hr, dict):
        res["hs_total"] = fnum(hr.get("total") or hr.get("now"))
    s = a.get("shares") or {}
    if isinstance(s, dict):
        res["accepted"], res["rejected"] = fnum(s.get("accepted")), fnum(s.get("rejected"))
    pool = a.get("pool") or {}
    res["pool"] = (pool.get("pool") or pool.get("url")) if isinstance(pool, dict) else str(pool)
    for d in j.get("gpu_devices") or []:
        if isinstance(d, dict):
            did = d.get("id", d.get("device_id"))
            bus = d.get("bus_id", d.get("bus"))
            try:
                res["bus_map"][int(did)] = int(bus)
            except Exception:
                pass
    return res


def merge_hs(gpus, p):
    by_bus = {b: i for i, b in p["bus_map"].items()}
    for g in gpus:
        sid = by_bus.get(g["bus"]) if by_bus and g["bus"] is not None else g["i"]
        g["hs"] = p["gpu_hs"].get(sid) if sid is not None else None

# ------------------------------------------------------------------ 矿工进程


def expand(v, n):
    parts = [s for s in re.split(r"[\s,，;；|/]+", str(v or "").strip()) if s]   # 兼容 HiveOS 空格写法
    if not parts or n <= 0:
        return None
    if len(parts) == 1:
        parts = parts * n
    elif len(parts) < n:
        parts += [parts[-1]] * (n - len(parts))
    return ",".join(parts[:n])


def build_args(cfg, ngpu):
    fl = (cfg or {}).get("flight")
    if not fl or not fl.get("algorithm") or not fl.get("pool") or not fl.get("wallet"):
        return None
    a = ["--disable-cpu", "--algorithm", fl["algorithm"].strip(), "--pool", fl["pool"].strip(),
         "--wallet", fl["wallet"].strip().replace("%RIG%", RIG_ID),
         "--api-enable", "--api-port", str(API_PORT), "--api-rig-name", RIG_ID]
    worker = str(fl.get("worker") or "").strip().replace("%RIG%", RIG_ID)
    if worker:
        a += ["--worker", worker]
    if str(fl.get("password") or "").strip():
        a += ["--password", fl["password"].strip()]
    if str(fl.get("tls")) in ("1", "true", "True"):
        a += ["--tls", "true"]
    if str(fl.get("tls_cert") or "").strip():
        a += ["--tls-cert-sha256", fl["tls_cert"].strip()]
    oc = cfg.get("oc")
    if oc:
        if str(oc.get("reset", 1)) in ("1", "true", "True"):
            a += ["--gpu-reset-oc"]
        pairs = [("cclock", "--gpu-cclock0"), ("mclock", "--gpu-mclock0"), ("coffset", "--gpu-coffset0"),
                 ("moffset", "--gpu-moffset0"), ("plimit", "--gpu-plimit0"), ("fan", "--gpu-fan0")]
        oc_args = []
        for k, flag in pairs:
            v = expand(oc.get(k), ngpu)
            if v:
                oc_args += [flag, v]
        if oc_args:
            a += ["--gpu-id", ",".join(str(i) for i in range(ngpu))] + oc_args
        if str(oc.get("extra") or "").strip():
            a += shlex.split(oc["extra"])
    else:
        a += ["--gpu-reset-oc"]
    if str(fl.get("extra") or "").strip():
        a += shlex.split(fl["extra"])
    return a


class Miner:
    def __init__(self):
        self.proc = None
        self.args = None
        self.started = 0
        self.logf = None
        self.fails = 0
        self.next_try = 0
        self.lock = threading.RLock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args, binary=None):
        binary = binary or miner_bin(CUR_CFG)
        with self.lock:
            if binary == LDMINER_BIN and not ensure_ldminer():
                self.next_try = time.time() + 120
                return False
            if not os.path.exists(binary):
                event("error", "找不到矿工程序 %s，请重新运行安装脚本" % binary)
                self.next_try = time.time() + 120
                return False
            os.makedirs(LOG_DIR, exist_ok=True)
            try:
                if os.path.getsize(MINER_LOG) > 20 * 1024 * 1024:
                    os.replace(MINER_LOG, MINER_LOG + ".1")
            except OSError:
                pass
            self.logf = open(MINER_LOG, "ab", buffering=0)
            self.logf.write(("\n===== %s MinerHub 启动矿工 =====\n%s %s\n" % (
                time.strftime("%F %T"), binary, " ".join(shlex.quote(s) for s in args))).encode())
            env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", NO_COLOR="1")   # 混卡时 GPU 编号按插槽顺序，与 nvidia-smi 一致
            self.proc = subprocess.Popen([binary] + args, stdout=self.logf, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, cwd=os.path.dirname(binary),
                                         start_new_session=True, env=env)
            self.args, self.started, self.binary = args, time.time(), binary
            log("矿工已启动 pid=%s" % self.proc.pid)
            return True

    def stop(self, reason=""):
        with self.lock:
            if self.running():
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.proc.wait(20)
                except Exception:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                        self.proc.wait(5)
                    except Exception:
                        pass
                if reason:
                    event("info", "矿工已停止：%s" % reason)
            self.proc, self.args = None, None
            if self.logf:
                try:
                    self.logf.close()
                except Exception:
                    pass
                self.logf = None


M = Miner()

# ------------------------------------------------------------------ 电源 / WOL / RTC


def set_wake(epoch):
    rc, out = sh(["rtcwake", "-m", "no", "-t", str(int(epoch))], 15)
    when = time.strftime("%F %H:%M", time.localtime(epoch))
    if rc == 0:
        log("RTC 定时开机已设置: %s" % when)
    else:
        event("warn", "设置 RTC 定时开机失败（%s）：%s" % (when, out.strip()[:200]))
    return rc == 0


def sysrq(seq):
    try:
        with open("/proc/sys/kernel/sysrq", "w") as f:
            f.write("1")
        for ch in seq:
            with open("/proc/sysrq-trigger", "w") as f:
                f.write(ch)
            time.sleep(1.5)
    except Exception as e:
        log("sysrq 失败: %s" % e)


def do_power(kind, wake_at=None):
    """kind: reboot / poweroff。正常走 systemctl，90 秒没反应就用 sysrq 强制（矿机驱动卡死常见）"""
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    M.stop("系统%s" % ("重启" if kind == "reboot" else "关机"))
    if kind == "poweroff":
        wake = wake_at or (CUR_CFG or {}).get("next_wake")
        if wake and wake > time.time() + 60:
            set_wake(wake)
    flush_events()
    sh("sync", 30)
    subprocess.Popen(["systemctl", kind], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def fallback():
        time.sleep(90)
        log("systemctl %s 超时，使用 sysrq 强制执行" % kind)
        sysrq("su" + ("b" if kind == "reboot" else "o"))
    threading.Thread(target=fallback, daemon=True).start()


def send_wol(mac, brd=""):
    m = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(m) != 12:
        raise ValueError("MAC 地址无效: %s" % mac)
    pkt = b"\xff" * 6 + bytes.fromhex(m) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for addr in filter(None, {"255.255.255.255", brd}):
        for port in (9, 7):
            s.sendto(pkt, (addr, port))
    s.close()

# ------------------------------------------------------------------ 指令执行


def tail(path, lines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 96 * 1024))
            data = f.read().decode("utf-8", "ignore")
        data = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data)   # 去掉终端颜色码
        return "\n".join(data.splitlines()[-lines:])
    except Exception as e:
        return "（读取日志失败：%s）" % e


def ack(cid, ok, result):
    for _ in range(3):
        try:
            post("/api/agent/ack", {"rig_id": RIG_ID, "id": cid, "ok": ok, "result": str(result)[-60000:]})
            return
        except Exception:
            time.sleep(2)


def run_cmd(c):
    global RESTART_REQ
    cid, a, args = c.get("id"), c.get("action"), c.get("args") or {}
    try:
        if a == "restart_miner":
            RESTART_REQ = True
            ack(cid, True, "已安排重启矿工")
        elif a in ("reboot", "shutdown"):
            ack(cid, True, "即将执行")
            time.sleep(1)
            do_power("reboot" if a == "reboot" else "poweroff", args.get("wake_at"))
        elif a == "get_log":
            raw = miner_api_raw()
            ack(cid, True, json.dumps({"log": tail(MINER_LOG, int(args.get("lines", 200))),
                                       "api": raw}, ensure_ascii=False))
        elif a == "shell":
            rc, out = sh(["bash", "-lc", str(args.get("cmd", ""))], int(args.get("timeout", 120)))
            ack(cid, rc == 0, "$ %s\n%s\n[exit %s]" % (args.get("cmd"), out, rc))
        elif a == "wol":
            send_wol(args.get("mac"), NET.get("brd", ""))
            ack(cid, True, "已向 %s 发送唤醒包" % args.get("mac"))
        else:
            ack(cid, False, "未知指令 %s" % a)
    except Exception as e:
        ack(cid, False, "执行失败: %s" % e)

# ------------------------------------------------------------------ 主循环

CUR_CFG = None
NET = {}
WD = {"zero_since": None, "restarts": [], "temp_hold": False, "boot_gpus": None,
      "gpu_drop_warned": False, "lost_warned": False, "no_flight_warned": False,
      "wake_set": None, "exit_warned": 0}


def flush_events():
    with EV_LOCK:
        evs = EVENTS[:]
    if not evs:
        return
    try:
        post("/api/agent/report", {"rig_id": RIG_ID, "info": dyn_info(NET), "stats": {}, "events": evs}, 8)
        with EV_LOCK:
            del EVENTS[:len(evs)]
    except Exception:
        pass


def wd_reboot_allowed(state):
    today = time.strftime("%F")
    lst = [d for d in state.get("wd_reboots", []) if d == today]
    if len(lst) >= 3:
        return False
    state["wd_reboots"] = lst + [today]
    save_json(STATE_PATH, state)
    return True


def reconcile(cfg, gpus, gerr, lost, p, state):
    global RESTART_REQ
    if SHUTTING_DOWN or not cfg:
        return
    wd = cfg.get("watchdog") or {}
    now = time.time()
    ngpu = len(gpus)

    # --- 掉卡检测
    if WD["boot_gpus"] is None and ngpu:
        WD["boot_gpus"] = ngpu
    if lost and not WD["lost_warned"]:
        WD["lost_warned"] = True
        event("error", "GPU 掉卡 / 驱动异常：%s" % (gerr or ""))
        if wd.get("reboot") and wd_reboot_allowed(state):
            event("error", "看门狗：检测到掉卡，重启系统")
            return do_power("reboot")
    if WD["boot_gpus"] and 0 < ngpu < WD["boot_gpus"] and not WD["gpu_drop_warned"]:
        WD["gpu_drop_warned"] = True
        event("warn", "掉卡：当前识别 %d 张，开机时 %d 张" % (ngpu, WD["boot_gpus"]))

    # --- 温度保护
    temps = [g["temp"] for g in gpus if g.get("temp") is not None]
    tmax = max(temps) if temps else 0
    crit, resume = wd.get("temp_crit", 85), wd.get("temp_resume", 70)
    if tmax >= crit and not WD["temp_hold"]:
        WD["temp_hold"] = True
        event("error", "超温保护：最高 %.0f°C ≥ %.0f°C，已停止挖矿" % (tmax, crit))
        M.stop()
    elif WD["temp_hold"] and tmax and tmax < resume:
        WD["temp_hold"] = False
        event("info", "温度已降到 %.0f°C，恢复挖矿" % tmax)

    # --- RTC 定时开机
    nw = cfg.get("next_wake")
    if nw and nw != WD["wake_set"]:
        WD["wake_set"] = nw          # 无论成败只尝试一次，失败只告警一次，不刷屏
        set_wake(nw)

    # --- 矿工进程
    args = build_args(cfg, ngpu or (WD["boot_gpus"] or 0))
    want = cfg.get("desired") == "mining" and args is not None and not WD["temp_hold"]
    if cfg.get("desired") == "mining" and args is None:
        if not WD["no_flight_warned"]:
            WD["no_flight_warned"] = True
            event("warn", "已设为挖矿，但没有分配飞行表（或飞行表缺少算法/矿池/钱包）")
    else:
        WD["no_flight_warned"] = False

    if not want:
        if M.running():
            M.stop("期望状态为停止" if not WD["temp_hold"] else "")
        return
    if not M.running():
        if M.proc is not None:
            code = M.proc.poll()
            M.proc = None
            M.fails += 1
            if now - WD["exit_warned"] > 600:
                WD["exit_warned"] = now
                event("warn", "矿工进程意外退出（code %s），自动重启" % code)
            M.next_try = now + min(60, 10 * M.fails)
        if now >= M.next_try:
            M.start(args)
        return
    if args != M.args or miner_bin(cfg) != getattr(M, "binary", None) or RESTART_REQ:
        RESTART_REQ = False
        event("info", "应用新配置，重启矿工" if (args != M.args or miner_bin(cfg) != getattr(M, "binary", None)) else "按指令重启矿工")
        M.stop()
        M.start(args)
        WD["zero_since"] = None
        return
    if now - M.started > 300:
        M.fails = 0

    # --- 零算力看门狗（启动 3 分钟后才开始判断）
    if now - M.started > 180:
        if not p["hs_total"]:
            if WD["zero_since"] is None:
                WD["zero_since"] = now
            elif now - WD["zero_since"] > wd.get("zero_min", 10) * 60:
                WD["zero_since"] = None
                WD["restarts"] = [t for t in WD["restarts"] if now - t < 3600] + [now]
                if len(WD["restarts"]) >= wd.get("max_restarts", 3) and wd.get("reboot") and wd_reboot_allowed(state):
                    event("error", "看门狗：1 小时内零算力重启 %d 次仍无效，重启系统" % len(WD["restarts"]))
                    return do_power("reboot")
                event("warn", "看门狗：算力为 0 超过 %.0f 分钟，重启矿工" % wd.get("zero_min", 10))
                M.stop()
                M.start(args)
        else:
            WD["zero_since"] = None


def prepare():
    os.makedirs(LOG_DIR, exist_ok=True)
    sh(["nvidia-smi", "-pm", "1"], 30)                     # 持久模式，OC 才稳定
    if NET.get("iface"):
        sh(["ethtool", "-s", NET["iface"], "wol", "g"], 10)  # 开启网卡 WOL
    for n in MINER_PROC_NAMES:
        sh(["pkill", "-9", "-x", n], 10)                     # 清掉残留进程（按进程名精确匹配），防止重复挖


def on_term(*_):
    log("收到退出信号，停止矿工")
    M.stop()
    sys.exit(0)


def main():
    global CUR_CFG, NET
    if not SERVER or not FARM_KEY:
        log("配置缺失：请检查 %s 中的 server / farm_key" % CONF_PATH)
        sys.exit(1)
    signal.signal(signal.SIGTERM, on_term)
    state = load_json(STATE_PATH, {})
    CUR_CFG = state.get("cfg")                     # 面板连不上时用上次的配置继续挖
    NET = net_info()
    prepare()
    log("MinerHub Agent %s 启动，rig_id=%s，面板=%s" % (VERSION, RIG_ID, SERVER))
    fails, loops = 0, 0
    while True:
        t0 = time.time()
        loops += 1
        try:
            if loops % 30 == 0:
                NET = net_info()
            gpus, gerr, lost = gpu_stats()
            raw = miner_api_raw() if M.running() else None
            p = parse_api(raw)
            merge_hs(gpus, p)
            stats = {
                "ts": int(t0), "gpu_error": gerr, "temp_hold": WD["temp_hold"],
                "miner": {"running": M.running(), "uptime": int(t0 - M.started) if M.running() else 0,
                          "algo": p["algo"], "pool": p["pool"], "version": p["version"],
                          "hs_total": p["hs_total"], "accepted": p["accepted"], "rejected": p["rejected"],
                          "api_ok": raw is not None},
                "gpus": gpus,
            }
            with EV_LOCK:
                evs = EVENTS[:]
            try:
                resp = post("/api/agent/report", {"rig_id": RIG_ID, "info": dyn_info(NET), "stats": stats, "events": evs})
                with EV_LOCK:
                    del EVENTS[:len(evs)]
                if resp.get("ok") is False:
                    log("面板拒绝: %s" % resp.get("error"))
                if resp.get("cfg"):
                    CUR_CFG = resp["cfg"]
                    if CUR_CFG != state.get("cfg"):
                        state["cfg"] = CUR_CFG
                        save_json(STATE_PATH, state)
                for c in resp.get("commands") or []:
                    threading.Thread(target=run_cmd, args=(c,), daemon=True).start()
                if fails:
                    log("已重新连上面板")
                fails = 0
            except Exception as e:
                fails += 1
                if fails in (1, 6) or fails % 60 == 0:
                    log("上报失败（第 %d 次）：%s —— 继续按本地缓存配置运行" % (fails, e))
            reconcile(CUR_CFG, gpus, gerr, lost, p, state)
        except Exception as e:
            log("主循环异常: %r" % e)
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
