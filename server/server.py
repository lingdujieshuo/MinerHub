#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MinerHub Server —— 自建矿场监控面板（HiveOS 替代）
项目主页 https://github.com/lingdujieshuo/MinerHub · 作者 零度解说 https://www.freedidi.com · https://www.youtube.com/@lingdujieshuo
零依赖：只用 Python 3.9+ 标准库（Ubuntu 22.04/24.04 自带即可运行）

运行:  python3 server.py                 启动面板（默认 0.0.0.0:8800）
       python3 server.py passwd 新密码    重置管理员密码
       python3 server.py info            显示面板地址、Farm Key 和矿机安装命令
       python3 server.py entrance 后缀    设置安全入口（面板地址变成 http://IP:端口/后缀/），entrance off 关闭
环境变量: MINERHUB_PORT / MINERHUB_HOST / MINERHUB_DATA / MINERHUB_PUBLIC_URL
"""
import hashlib, hmac, html, json, os, re, secrets, sqlite3, sys, threading, time, traceback
import urllib.error, urllib.parse, urllib.request, mimetypes
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.0.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("MINERHUB_DATA", os.path.join(BASE, "data"))
STATIC_DIR = os.path.join(BASE, "static")
HOST = os.environ.get("MINERHUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("MINERHUB_PORT", "8800"))
OFFLINE_SEC = 45          # 超过多少秒无心跳判定离线
CMD_EXPIRE_SEC = 180      # 命令多久没被执行就作废（防止开机后收到昨天的关机命令）
HIST_EVERY = 60           # 历史曲线采样间隔（秒）
HIST_KEEP_DAYS = 7
EVENT_KEEP_DAYS = 30

os.makedirs(DATA_DIR, exist_ok=True)
DB = sqlite3.connect(os.path.join(DATA_DIR, "minerhub.db"), check_same_thread=False, isolation_level=None)
DB.row_factory = sqlite3.Row
LOCK = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rigs(
  id TEXT PRIMARY KEY, name TEXT, ip TEXT DEFAULT '', mac TEXT DEFAULT '',
  last_seen INTEGER DEFAULT 0, info TEXT DEFAULT '{}', stats TEXT DEFAULT '{}',
  flight_id INTEGER, oc_id INTEGER, desired TEXT DEFAULT 'stopped',
  notes TEXT DEFAULT '', created INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS flights(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, algorithm TEXT, pool TEXT, wallet TEXT,
  worker TEXT DEFAULT '%RIG%', password TEXT DEFAULT '', tls INTEGER DEFAULT 0,
  tls_cert TEXT DEFAULT '', extra TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS oc_profiles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, cclock TEXT DEFAULT '', mclock TEXT DEFAULT '',
  coffset TEXT DEFAULT '', moffset TEXT DEFAULT '', plimit TEXT DEFAULT '', fan TEXT DEFAULT '',
  reset INTEGER DEFAULT 1, extra TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT DEFAULT '*', action TEXT, at TEXT,
  days TEXT DEFAULT '1234567', enabled INTEGER DEFAULT 1, last_fire TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rig_id TEXT, action TEXT, args TEXT DEFAULT '{}',
  status TEXT DEFAULT 'pending', result TEXT DEFAULT '', created INTEGER, done INTEGER);
CREATE TABLE IF NOT EXISTS history(rig_id TEXT, ts INTEGER, hs REAL, power REAL, tmax REAL, algo TEXT);
CREATE INDEX IF NOT EXISTS ix_hist ON history(rig_id, ts);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, rig_id TEXT, ts INTEGER, level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS ix_ev ON events(ts);
"""

DEFAULTS = {
    "tz": "Asia/Shanghai",
    "temp_crit": "85",        # 任一 GPU 达到该温度 → 停止挖矿
    "temp_resume": "70",      # 降到该温度以下 → 自动恢复
    "wd_zero_min": "10",      # 算力为 0 持续 N 分钟 → 重启矿工
    "wd_max_restarts": "3",   # 1 小时内重启矿工达到 N 次 → 重启系统
    "wd_reboot": "1",         # 是否允许看门狗重启系统 / 掉卡重启
    "tg_token": "", "tg_chat": "", "tg_api": "",
    "alert_offline": "1",     # Telegram：矿机意外离线
    "tg_ev_online": "1",      # 离线后恢复在线 / 新矿机接入
    "tg_ev_temp": "1",        # 超温停机 / 降温恢复
    "tg_ev_gpu": "1",         # 掉卡 / 单卡算力为 0
    "tg_ev_watchdog": "1",    # 看门狗动作 / 矿工崩溃
    "tg_ev_hsdrop": "1",      # 算力明显下降
    "hs_drop_pct": "30",      # 算力比之前 1 小时平均低多少 % 报警
    "tg_ev_sched": "1",       # 定时任务执行结果 + 定时开机上线检查
    "tg_ev_other": "1",       # 其它警告
    "tg_daily": "1", "tg_daily_at": "09:00",   # 每日日报
    "tg_report_hours": "0",   # 每 N 小时推送一次矿场状态（0=关闭）
    "tg_bot_cmds": "1",       # 允许通过机器人命令查询 / 控制矿机
    "tg_mute_until": "0",
    "public_url": "",
}

# ---------------------------------------------------------------- DB helpers
def q(sql, args=(), one=False):
    with LOCK:
        rows = [dict(r) for r in DB.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows

def x(sql, args=()):
    with LOCK:
        return DB.execute(sql, args).lastrowid

def sget(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), True)
    return r["v"] if r else DEFAULTS.get(k, "")

def sset(k, v):
    x("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, str(v)))

def fnum(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d

def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return salt + "$" + h

def check_pw(pw, stored):
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)

def init_db(quiet=False):
    with LOCK:
        DB.executescript(SCHEMA)
        cols = [r[1] for r in DB.execute("PRAGMA table_info(flights)").fetchall()]
        if "miner" not in cols:   # v1.1：飞行表可选矿工程序（srbminer / ldminer）
            DB.execute("ALTER TABLE flights ADD COLUMN miner TEXT DEFAULT 'srbminer'")
    if not sget("secret"):
        sset("secret", secrets.token_hex(32))
    if not q("SELECT v FROM settings WHERE k='farm_key'", one=True):
        sset("farm_key", secrets.token_urlsafe(24))
    if not q("SELECT v FROM settings WHERE k='admin_pass'", one=True):
        pw = os.environ.get("MINERHUB_ADMIN_PASS") or secrets.token_urlsafe(9)
        sset("admin_pass", hash_pw(pw))
        if not quiet:
            print("=" * 60)
            print(" MinerHub 首次启动，管理员密码: %s" % pw)
            print(" Farm Key: %s" % sget("farm_key"))
            print("=" * 60, flush=True)

# ---------------------------------------------------------------- time / schedule
def tzinfo():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(sget("tz") or "UTC")
    except Exception:
        return None

def now_local():
    tz = tzinfo()
    return datetime.now(tz) if tz else datetime.now().astimezone()

def next_occurrence(s, after):
    try:
        hh, mm = [int(v) for v in s["at"].split(":")]
    except Exception:
        return None
    days = s.get("days") or "1234567"
    for d in range(0, 9):
        cand = (after + timedelta(days=d)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if str(cand.isoweekday()) in days and cand > after:
            return cand
    return None

def next_wake(rid):
    """下一次定时开机时间（epoch），交给矿机写入主板 RTC 闹钟。"""
    after = now_local() + timedelta(seconds=120)
    best = None
    for s in q("SELECT * FROM schedules WHERE enabled=1 AND action='poweron'"):
        if s["target"] not in ("*", rid):
            continue
        c = next_occurrence(s, after)
        if c and (best is None or c < best):
            best = c
    return int(best.timestamp()) if best else None

# ---------------------------------------------------------------- alerts / events
def h(s):
    return html.escape(str(s if s is not None else ""), quote=False)

def fmt_hs(v):
    if v is None:
        return "—"
    v, units, i = float(v), ["H/s", "KH/s", "MH/s", "GH/s", "TH/s", "PH/s"], 0
    while abs(v) >= 1000 and i < len(units) - 1:
        v /= 1000
        i += 1
    return ("%.1f" if v >= 100 else "%.2f") % v + " " + units[i]

def fmt_dur(s):
    s = int(max(0, s or 0))
    d, hh, mm = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return "%d天%d小时" % (d, hh) if d else "%d小时%d分" % (hh, mm) if hh else "%d分钟" % max(1, mm)

def rig_name(rid):
    r = q("SELECT name FROM rigs WHERE id=?", (rid,), True) if rid else None
    return (r or {}).get("name") or rid or "面板"

# ---------------------------------------------------------------- Telegram
TG_Q, TG_LOCK, TG_EVENT = [], threading.Lock(), threading.Event()
TG_DEDUP = {}        # (rig, msg) -> ts，同一条告警 10 分钟内只发一次
TG_CHATS = {}        # 最近给机器人发过消息的会话（用于在面板里一键获取 Chat ID）
TG_STATUS = {"ok": None, "err": "", "ts": 0, "bot": ""}
CAT_TOGGLE = {"offline": "alert_offline", "online": "tg_ev_online", "temp": "tg_ev_temp", "gpu": "tg_ev_gpu",
              "watchdog": "tg_ev_watchdog", "hsdrop": "tg_ev_hsdrop", "sched": "tg_ev_sched", "other": "tg_ev_other"}
ICON = {"error": "🔥", "warn": "⚠️", "info": "ℹ️"}

def tg_ready():
    return bool(sget("tg_token").strip() and sget("tg_chat").strip())

def tg_api(method, params=None, timeout=15):
    token = sget("tg_token").strip()
    if not token:
        raise RuntimeError("未设置 Bot Token")
    base = (sget("tg_api").strip() or "https://api.telegram.org").rstrip("/")
    req = urllib.request.Request("%s/bot%s/%s" % (base, token, method),
                                 data=json.dumps(params or {}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise RuntimeError("HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("连接 Telegram 失败：%s" % e.reason)
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or "Telegram 返回错误")
    return j.get("result")

def tg_send_now(text, chat=None, markup=None):
    chat = chat or sget("tg_chat").strip()
    if not chat:
        raise RuntimeError("未设置 Chat ID")
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    res = tg_api("sendMessage", p)
    TG_STATUS.update(ok=True, err="", ts=int(time.time()))
    return res

def tg_send_bg(text, markup=None):
    def _go():
        try:
            tg_send_now(text, markup=markup)
        except Exception as e:
            TG_STATUS.update(ok=False, err=str(e), ts=int(time.time()))
            print("telegram 发送失败:", e, flush=True)
    threading.Thread(target=_go, daemon=True).start()

def tg_muted():
    return time.time() < fnum(sget("tg_mute_until"), 0)

def tg_notify(line):
    """告警进入队列，12 秒内的多条合并成一条消息（例如定时关机时 4 台矿机同时下线）"""
    if not tg_ready() or tg_muted():
        return
    with TG_LOCK:
        TG_Q.append(line)
    TG_EVENT.set()

def tg_sender_loop():
    while True:
        TG_EVENT.wait()
        time.sleep(12)
        with TG_LOCK:
            items = TG_Q[:]
            del TG_Q[:]
            TG_EVENT.clear()
        chunks, cur = [], ""
        for line in items:
            if cur and len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + line
        if cur:
            chunks.append(cur)
        for c in chunks:
            for attempt in range(3):
                try:
                    tg_send_now(c)
                    break
                except Exception as e:
                    TG_STATUS.update(ok=False, err=str(e), ts=int(time.time()))
                    print("telegram 发送失败:", e, flush=True)
                    time.sleep(5 * (attempt + 1))

def classify(msg):
    if "超温" in msg or "温度已降到" in msg:
        return "temp"
    if "掉卡" in msg:
        return "gpu"
    if "看门狗" in msg or "意外退出" in msg:
        return "watchdog"
    return "other"

def add_event(rid, level, msg, cat=None, notify=None, icon=None):
    x("INSERT INTO events(rig_id,ts,level,msg) VALUES(?,?,?,?)", (rid, int(time.time()), level, msg[:2000]))
    cat = cat or classify(msg)
    if notify is None:
        notify = level in ("warn", "error")
        if cat == "temp" and "恢复" in msg:
            notify, icon = True, "✅"
    if not notify or sget(CAT_TOGGLE.get(cat, "tg_ev_other")) != "1":
        return
    now, key = time.time(), (rid, msg)
    if now - TG_DEDUP.get(key, 0) < 600:
        return
    TG_DEDUP[key] = now
    if len(TG_DEDUP) > 2000:
        for k in [k for k, t in TG_DEDUP.items() if now - t > 600]:
            TG_DEDUP.pop(k, None)
    tg_notify("%s <b>%s</b>  %s" % (icon or ICON.get(level, "•"), h(rig_name(rid)), h(msg)))

# ---------------------------------------------------------------- rigs / commands
ONLINE = {}         # rig_id -> bool
LAST_HIST = {}      # rig_id -> ts
LAST_POWER_CMD = {} # rig_id -> ts（主动关机/重启时，离线不报警）
OFFLINE_ALERTED = set()   # 已发过离线告警的矿机（恢复在线时再通知）
WAKE_CHECKS = []          # [(到期时间, [rig_id], 标签)] 定时开机后检查是否全部上线

def is_online(r):
    return (time.time() - (r.get("last_seen") or 0)) < OFFLINE_SEC

def rig_public(r):
    return {
        "id": r["id"], "name": r["name"] or r["id"], "ip": r["ip"], "mac": r["mac"],
        "online": is_online(r), "last_seen": r["last_seen"], "desired": r["desired"],
        "flight_id": r["flight_id"], "oc_id": r["oc_id"], "notes": r["notes"],
        "info": json.loads(r["info"] or "{}"), "stats": json.loads(r["stats"] or "{}"),
    }

OC_LIST_FIELDS = ("cclock", "mclock", "coffset", "moffset", "plimit", "fan")

def norm_list(v):
    """'1680 1560 1560' / '1680,1560' / '1680，1560' → '1680,1560,1560'（兼容 HiveOS 的空格写法）"""
    return ",".join(p for p in re.split(r"[\s,，;；|/]+", str(v or "").strip()) if p)

def rig_cfg(r):
    fl = q("SELECT * FROM flights WHERE id=?", (r["flight_id"],), True) if r["flight_id"] else None
    oc = q("SELECT * FROM oc_profiles WHERE id=?", (r["oc_id"],), True) if r["oc_id"] else None
    if oc:
        oc = dict(oc)
        for k in OC_LIST_FIELDS:
            oc[k] = norm_list(oc.get(k))
    return {
        "desired": r["desired"], "flight": fl, "oc": oc, "next_wake": next_wake(r["id"]),
        "watchdog": {
            "temp_crit": fnum(sget("temp_crit"), 85), "temp_resume": fnum(sget("temp_resume"), 70),
            "zero_min": fnum(sget("wd_zero_min"), 10), "max_restarts": int(fnum(sget("wd_max_restarts"), 3)),
            "reboot": sget("wd_reboot") == "1",
        },
    }

def add_cmd(rid, action, args=None):
    return x("INSERT INTO commands(rig_id,action,args,status,created) VALUES(?,?,?,?,?)",
             (rid, action, json.dumps(args or {}, ensure_ascii=False), "pending", int(time.time())))

ACTION_NAMES = {
    "start_miner": "启动挖矿", "stop_miner": "停止挖矿", "restart_miner": "重启矿工",
    "reboot": "重启系统", "shutdown": "关机", "wol": "网络唤醒", "poweron": "开机",
    "get_log": "读取日志", "shell": "执行命令",
}

def issue(rid, action, args=None, source="面板"):
    """统一指令入口：返回 (ok, msg, cmd_id)"""
    r = q("SELECT * FROM rigs WHERE id=?", (rid,), True)
    if not r:
        return False, "矿机不存在", None
    args = args or {}
    if action in ("start_miner", "stop_miner"):
        desired = "mining" if action == "start_miner" else "stopped"
        x("UPDATE rigs SET desired=? WHERE id=?", (desired, rid))
        add_event(rid, "info", "%s：%s" % (source, ACTION_NAMES[action]))
        return True, "已%s（下次心跳生效）" % ACTION_NAMES[action], None
    if action in ("wol", "poweron"):
        if is_online(r):
            return False, "矿机在线，无需唤醒", None
        if not r["mac"]:
            return False, "该矿机还没上报过 MAC 地址", None
        relays = [o for o in q("SELECT * FROM rigs WHERE id<>?", (rid,)) if is_online(o)]
        if not relays:
            return False, "没有其它在线矿机可代发唤醒包（WOL 需同一局域网内有一台在线矿机）", None
        relay = relays[0]
        cid = add_cmd(relay["id"], "wol", {"mac": r["mac"], "target": rid})
        add_event(rid, "info", "%s：通过 %s 发送网络唤醒包" % (source, relay["name"] or relay["id"]))
        return True, "已通过 %s 发送唤醒包" % (relay["name"] or relay["id"]), cid
    if action in ("restart_miner", "reboot", "shutdown", "get_log", "shell"):
        if not is_online(r):
            return False, "矿机离线", None
        if action == "shell" and not str(args.get("cmd", "")).strip():
            return False, "命令为空", None
        if action in ("reboot", "shutdown"):
            LAST_POWER_CMD[rid] = time.time()
        cid = add_cmd(rid, action, args)
        if action not in ("get_log",):
            add_event(rid, "info", "%s：%s%s" % (source, ACTION_NAMES[action],
                                                 (" `%s`" % args.get("cmd")) if action == "shell" else ""))
        return True, "指令已下发", cid
    return False, "未知操作: %s" % action, None

def agent_report(body, client_ip):
    rid = str(body.get("rig_id") or "").strip()[:64]
    if not re.match(r"^[\w.\-]+$", rid):
        return {"ok": False, "error": "rig_id 非法（只能用字母数字 _ - .）"}
    now = int(time.time())
    info = body.get("info") or {}
    stats = body.get("stats") or {}
    r = q("SELECT * FROM rigs WHERE id=?", (rid,), True)
    if not r:
        x("INSERT INTO rigs(id,name,created,desired) VALUES(?,?,?,?)", (rid, rid, now, "stopped"))
        add_event(rid, "info", "新矿机接入（%s）" % (info.get("ip") or client_ip), cat="online", notify=True, icon="🆕")
    elif not is_online(r):
        alerted = rid in OFFLINE_ALERTED
        OFFLINE_ALERTED.discard(rid)
        add_event(rid, "info", "矿机恢复在线（离线 %s）" % fmt_dur(now - (r["last_seen"] or now)),
                  cat="online", notify=alerted, icon="✅")
    ONLINE[rid] = True
    x("UPDATE rigs SET ip=?, mac=?, last_seen=?, info=?, stats=? WHERE id=?",
      (info.get("ip") or client_ip, info.get("mac") or (r or {}).get("mac") or "", now,
       json.dumps(info, ensure_ascii=False), json.dumps(stats, ensure_ascii=False), rid))
    if now - LAST_HIST.get(rid, 0) >= HIST_EVERY:
        LAST_HIST[rid] = now
        m = stats.get("miner") or {}
        gpus = stats.get("gpus") or []
        temps = [g.get("temp") for g in gpus if g.get("temp") is not None]
        x("INSERT INTO history(rig_id,ts,hs,power,tmax,algo) VALUES(?,?,?,?,?,?)",
          (rid, now, m.get("hs_total") or 0, sum((g.get("power") or 0) for g in gpus),
           max(temps) if temps else None, m.get("algo") or ""))
    for ev in (body.get("events") or [])[:50]:
        add_event(rid, ev.get("level", "info"), str(ev.get("msg", "")))
    if stats:
        try:
            check_hashrate(rid, stats)
        except Exception:
            traceback.print_exc()
    cmds = q("SELECT id,action,args FROM commands WHERE rig_id=? AND status='pending' ORDER BY id", (rid,))
    for c in cmds:
        x("UPDATE commands SET status='sent' WHERE id=?", (c["id"],))
        c["args"] = json.loads(c["args"] or "{}")
    r = q("SELECT * FROM rigs WHERE id=?", (rid,), True)
    return {"ok": True, "cfg": rig_cfg(r), "commands": cmds, "server_time": now}

HS_WIN = {}   # rig_id -> 算力监测窗口

def check_hashrate(rid, stats):
    """算力下降：近 5 分钟平均比之前（10~65 分钟前）平均低 N% → 告警；单卡算力为 0 超过 5 分钟 → 告警"""
    m = stats.get("miner") or {}
    w = HS_WIN.setdefault(rid, {"algo": None, "pts": [], "alerted": False, "base": 0, "gz": {}, "gz_alerted": set(), "up": 0})
    now = time.time()
    if not m.get("running") or stats.get("temp_hold"):
        w.update(pts=[], gz={}, up=0, alerted=False)
        return
    up, hs = m.get("uptime") or 0, m.get("hs_total") or 0
    if m.get("algo") != w["algo"] or up < w["up"]:          # 换算法 / 矿工重启 → 重新统计
        w.update(algo=m.get("algo"), pts=[], alerted=False, gz={}, gz_alerted=set())
    w["up"] = up
    w["pts"] = [p for p in w["pts"] if now - p[0] < 3900] + [(now, hs)]
    pct = fnum(sget("hs_drop_pct"), 30)
    if up > 900 and pct > 0:
        recent = [v for t, v in w["pts"] if now - t <= 300]
        base = [v for t, v in w["pts"] if now - t >= 600]
        c = sum(recent) / len(recent) if recent else 0
        if not w["alerted"] and len(base) >= 20:
            b = sum(base) / len(base)
            if b > 0 and c < b * (1 - pct / 100):
                w.update(alerted=True, base=b)
                add_event(rid, "warn", "算力下降 %.0f%%：%s → %s" % ((1 - c / b) * 100, fmt_hs(b), fmt_hs(c)), cat="hsdrop")
        elif w["alerted"] and c >= w["base"] * (1 - pct / 200):
            w["alerted"] = False
            add_event(rid, "info", "算力已恢复：%s" % fmt_hs(c), cat="hsdrop", notify=True, icon="✅")
    gpus = stats.get("gpus") or []
    if up > 600 and any((g.get("hs") or 0) > 0 for g in gpus):
        for g in gpus:
            i = g.get("i")
            if (g.get("hs") or 0) <= 0:
                since = w["gz"].setdefault(i, now)
                if now - since > 300 and i not in w["gz_alerted"]:
                    w["gz_alerted"].add(i)
                    add_event(rid, "warn", "GPU%s（%s）算力为 0 已超过 5 分钟" % (i, g.get("name", "")), cat="gpu")
            else:
                w["gz"].pop(i, None)
                if i in w["gz_alerted"]:
                    w["gz_alerted"].discard(i)
                    add_event(rid, "info", "GPU%s 算力已恢复" % i, cat="gpu", notify=True, icon="✅")

def agent_ack(body):
    cid = int(body.get("id") or 0)
    status = "done" if body.get("ok") else "failed"
    x("UPDATE commands SET status=?, result=?, done=? WHERE id=?",
      (status, str(body.get("result", ""))[-60000:], int(time.time()), cid))
    return {"ok": True}

# ---------------------------------------------------------------- background loop
def run_schedules():
    n = now_local()
    for s in q("SELECT * FROM schedules WHERE enabled=1"):
        try:
            hh, mm = [int(v) for v in s["at"].split(":")]
        except Exception:
            continue
        cand = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (n - cand).total_seconds()
        if not (0 <= delta < 180) or str(cand.isoweekday()) not in (s["days"] or "1234567"):
            continue
        key = cand.strftime("%Y-%m-%d %H:%M")
        if s["last_fire"] == key:
            continue
        x("UPDATE schedules SET last_fire=? WHERE id=?", (key, s["id"]))
        targets = [r["id"] for r in q("SELECT id FROM rigs")] if s["target"] == "*" else [s["target"]]
        done, skipped = [], []
        for rid in targets:
            r = q("SELECT * FROM rigs WHERE id=?", (rid,), True)
            if not r:
                continue
            name = r["name"] or rid
            if s["action"] == "poweron":
                if not is_online(r):   # RTC 闹钟是主方案，WOL 作为兜底
                    issue(rid, "wol", source="定时任务")
                    done.append(name)
                else:
                    skipped.append(name)
            elif s["action"] in ("start_miner", "stop_miner"):
                issue(rid, s["action"], source="定时任务")
                done.append(name)
            elif is_online(r):
                ok, msg, _ = issue(rid, s["action"], source="定时任务")
                (done if ok else skipped).append(name)
            else:
                skipped.append(name)
        label = "%s %s" % (s["at"], ACTION_NAMES.get(s["action"], s["action"]))
        if s["action"] == "poweron":
            WAKE_CHECKS.append((time.time() + 360, targets, label))
            text = "⏰ <b>定时任务</b> %s：%d 台待开机%s，6 分钟后检查上线情况" % (
                h(label), len(done), "（%d 台已在线）" % len(skipped) if skipped else "")
        else:
            text = "⏰ <b>定时任务</b> %s：已执行 %d 台%s" % (
                h(label), len(done), "，跳过 %d 台（离线）：%s" % (len(skipped), h("、".join(skipped))) if skipped else "")
        if sget("tg_ev_sched") == "1":
            tg_notify(text)

def run_wake_checks():
    for item in [w for w in WAKE_CHECKS if time.time() >= w[0]]:
        WAKE_CHECKS.remove(item)
        _, targets, label = item
        rigs = [r for r in (q("SELECT * FROM rigs WHERE id=?", (t,), True) for t in targets) if r]
        missing = [r["name"] or r["id"] for r in rigs if not is_online(r)]
        if sget("tg_ev_sched") != "1":
            continue
        if missing:
            tg_notify("⚠️ <b>定时开机检查</b>（%s）：%d/%d 台上线，<b>未上线：%s</b>" % (
                h(label), len(rigs) - len(missing), len(rigs), h("、".join(missing))))
        else:
            tg_notify("✅ <b>定时开机检查</b>（%s）：%d/%d 台全部上线" % (h(label), len(rigs), len(rigs)))

def run_reports():
    if not tg_ready():
        return
    n = now_local()
    if sget("tg_daily") == "1":
        try:
            hh, mm = [int(v) for v in (sget("tg_daily_at") or "09:00").split(":")]
        except Exception:
            hh, mm = 9, 0
        mins = n.hour * 60 + n.minute - (hh * 60 + mm)
        day = n.strftime("%Y-%m-%d")
        if 0 <= mins < 10 and sget("tg_daily_last") != day:
            sset("tg_daily_last", day)
            tg_send_bg(daily_text(24))
    every = fnum(sget("tg_report_hours"), 0)
    if every > 0 and time.time() - fnum(sget("tg_report_last"), 0) >= every * 3600:
        sset("tg_report_last", int(time.time()))
        tg_send_bg(status_text(), markup=STATUS_KB)

# ---------------------------------------------------------------- 状态 / 日报文本
STATUS_KB = {"inline_keyboard": [[{"text": "🔄 刷新", "callback_data": "status"}]]}

def status_text():
    rigs = [rig_public(r) for r in q("SELECT * FROM rigs ORDER BY name COLLATE NOCASE")]
    algo, pw, ngpu, lines, on = {}, 0, 0, [], 0
    for r in rigs:
        st, name = r["stats"], h(r["name"])
        m, g = st.get("miner") or {}, st.get("gpus") or []
        if not r["online"]:
            lines.append("⚫ <b>%s</b>  离线 %s" % (name, fmt_dur(time.time() - (r["last_seen"] or 0)) if r["last_seen"] else ""))
            continue
        on += 1
        p = sum((x.get("power") or 0) for x in g)
        pw, ngpu = pw + p, ngpu + len(g)
        temps = [x["temp"] for x in g if x.get("temp") is not None]
        hs = m.get("hs_total") or 0
        if m.get("running") and m.get("algo"):
            algo[m["algo"]] = algo.get(m["algo"], 0) + hs
        icon = "🔴" if st.get("temp_hold") else "🟢" if m.get("running") and hs > 0 else "🟡" if m.get("running") else "🔵"
        lines.append("%s <b>%s</b>  %s · %d卡 · %dW · %s°C" % (
            icon, name, fmt_hs(hs) if m.get("running") else "未挖矿", len(g), p, int(max(temps)) if temps else "-"))
    head = "⛏ <b>矿场状态</b>  %s\n在线 <b>%d/%d</b> · GPU %d · 功耗 %.2f kW\n" % (
        n_str(), on, len(rigs), ngpu, pw / 1000)
    head += "".join("%s：<b>%s</b>\n" % (h(a), fmt_hs(v)) for a, v in algo.items())
    if tg_muted():
        head += "🔕 告警静音至 %s\n" % datetime.fromtimestamp(fnum(sget("tg_mute_until")), tzinfo()).strftime("%m-%d %H:%M")
    return head + "\n" + ("\n".join(lines) or "还没有矿机接入") + "\n\n<i>🟢挖矿 🟡零算力 🔵停止 🔴超温 ⚫离线</i>"

def n_str():
    return now_local().strftime("%m-%d %H:%M")

def rig_text(r):
    r = rig_public(r)
    st, i = r["stats"], r["info"]
    m, g = st.get("miner") or {}, st.get("gpus") or []
    out = ["🖥 <b>%s</b>  %s" % (h(r["name"]), "在线" if r["online"] else "离线（%s前）" % fmt_dur(time.time() - (r["last_seen"] or 0))),
           "IP %s · 系统运行 %s" % (h(r["ip"]), fmt_dur(i.get("uptime"))),
           "算法 %s · 矿池 %s" % (h(m.get("algo") or "-"), h(m.get("pool") or "-")),
           "总算力 <b>%s</b> · 份额 %s/%s · 矿工运行 %s" % (
               fmt_hs(m.get("hs_total")) if m.get("running") else "未挖矿",
               int(m.get("accepted") or 0), int(m.get("rejected") or 0), fmt_dur(m.get("uptime"))), ""]
    for x in g:
        out.append("GPU%s %s  <b>%s</b> · %s°C · 风扇%s%% · %sW" % (
            x.get("i"), h(x.get("name", "")), fmt_hs(x.get("hs")) if m.get("running") else "-",
            int(x["temp"]) if x.get("temp") is not None else "-", int(x["fan"]) if x.get("fan") is not None else "-",
            int(x["power"]) if x.get("power") is not None else "-"))
    if st.get("gpu_error"):
        out.append("⚠️ " + h(st["gpu_error"][:200]))
    return "\n".join(out)

def daily_text(hours=24):
    since = time.time() - hours * 3600
    lines, tot_kwh, algo = [], 0.0, {}
    for r in q("SELECT id,name FROM rigs ORDER BY name COLLATE NOCASE"):
        hist = q("SELECT hs,power,tmax,algo FROM history WHERE rig_id=? AND ts>=?", (r["id"], since))
        name = h(r["name"] or r["id"])
        if not hist:
            lines.append("⚫ <b>%s</b>  无数据（一直离线）" % name)
            continue
        mining = [x for x in hist if (x["hs"] or 0) > 0]
        avg = sum(x["hs"] for x in mining) / len(mining) if mining else 0
        kwh = sum((x["power"] or 0) for x in hist) * HIST_EVERY / 3600 / 1000
        tot_kwh += kwh
        tmax = max((x["tmax"] or 0) for x in hist)
        a = mining[-1]["algo"] if mining else ""
        if a:
            algo[a] = algo.get(a, 0) + avg
        nwarn = q("SELECT COUNT(*) c FROM events WHERE rig_id=? AND ts>=? AND level IN ('warn','error')",
                  (r["id"], since), True)["c"]
        lines.append("<b>%s</b>  %s %s · 挖矿 %.1fh · %.1f kWh · 最高 %d°C%s" % (
            name, h(a or "-"), fmt_hs(avg) if avg else "-", len(mining) * HIST_EVERY / 3600, kwh, tmax,
            " · ⚠️%d" % nwarn if nwarn else ""))
    head = "📊 <b>矿场日报</b>  %s（过去 %d 小时）\n" % (n_str(), hours)
    head += "".join("%s 平均：<b>%s</b>\n" % (h(a), fmt_hs(v)) for a, v in algo.items())
    head += "GPU 总耗电：%.1f kWh\n\n" % tot_kwh
    return head + ("\n".join(lines) or "还没有矿机接入")

def monitor_loop():
    last_prune = 0
    while True:
        try:
            now = int(time.time())
            x("UPDATE commands SET status='expired', done=? WHERE status IN ('pending','sent') AND created<?",
              (now, now - CMD_EXPIRE_SEC))
            for r in q("SELECT id,name,last_seen FROM rigs"):
                on = is_online(r)
                if ONLINE.get(r["id"]) is True and not on:
                    planned = time.time() - LAST_POWER_CMD.get(r["id"], 0) < 900
                    if planned:
                        add_event(r["id"], "info", "矿机已关机/重启中（计划内）")
                    else:
                        OFFLINE_ALERTED.add(r["id"])
                        add_event(r["id"], "warn", "矿机意外离线（%d 秒无心跳）" % OFFLINE_SEC, cat="offline")
                ONLINE[r["id"]] = on
            run_schedules()
            run_wake_checks()
            run_reports()
            if now - last_prune > 3600:
                last_prune = now
                x("DELETE FROM history WHERE ts<?", (now - HIST_KEEP_DAYS * 86400,))
                x("DELETE FROM events WHERE ts<?", (now - EVENT_KEEP_DAYS * 86400,))
                x("DELETE FROM commands WHERE created<?", (now - 7 * 86400,))
        except Exception:
            traceback.print_exc()
        time.sleep(15)

# ---------------------------------------------------------------- Telegram 机器人命令
BOT_COMMANDS = [
    ("status", "矿场状态总览"), ("rig", "单台矿机详情：/rig 矿机名"), ("report", "过去 24 小时日报"),
    ("mine", "开始挖矿：/mine 矿机名 或 all"), ("stop", "停止挖矿：/stop 矿机名 或 all"),
    ("restart", "重启矿工：/restart 矿机名 或 all"), ("reboot", "重启系统：/reboot 矿机名 或 all"),
    ("shutdown", "关机：/shutdown 矿机名 或 all"), ("wake", "网络唤醒：/wake 矿机名 或 all"),
    ("mute", "告警静音：/mute 小时数（默认 2）"), ("unmute", "取消静音"), ("help", "帮助"),
]
BOT_ACTIONS = {"mine": "start_miner", "stop": "stop_miner", "restart": "restart_miner",
               "reboot": "reboot", "shutdown": "shutdown", "wake": "wol"}

def help_text():
    return "⛏ <b>MinerHub 机器人</b>\n\n" + "\n".join("/%s — %s" % (c, h(d)) for c, d in BOT_COMMANDS) + \
           "\n\n矿机名不区分大小写，可只写开头几个字母（唯一即可）。"

def find_rigs(target):
    rigs = q("SELECT * FROM rigs ORDER BY name COLLATE NOCASE")
    t = (target or "").strip().lower()
    if not t:
        return None, "请写上矿机名，例如 <code>/rig rig-01</code>，全部矿机用 <code>all</code>"
    if t in ("all", "*", "全部"):
        return rigs, None
    exact = [r for r in rigs if t in ((r["name"] or "").lower(), r["id"].lower())]
    if exact:
        return exact[:1], None
    part = [r for r in rigs if (r["name"] or "").lower().startswith(t) or r["id"].lower().startswith(t)] or \
           [r for r in rigs if t in (r["name"] or "").lower() or t in r["id"].lower()]
    if len(part) == 1:
        return part, None
    if not part:
        return None, "找不到矿机「%s」。现有：%s" % (h(target), h("、".join(r["name"] or r["id"] for r in rigs)) or "无")
    return None, "「%s」匹配到多台：%s，请写完整名字" % (h(target), h("、".join(r["name"] or r["id"] for r in part)))

def do_rig_action(cmd, target):
    rigs, err = find_rigs(target)
    if err:
        return "❌ " + err
    out = ["%s <b>%s</b>" % ("🤖", ACTION_NAMES[BOT_ACTIONS[cmd]])]
    for r in rigs:
        ok, msg, _ = issue(r["id"], BOT_ACTIONS[cmd], source="Telegram")
        out.append("%s %s：%s" % ("✅" if ok else "❌", h(r["name"] or r["id"]), h(msg)))
    return "\n".join(out)

def tg_command(cmd, arg):
    """返回 (文本, 按钮) 或 None"""
    if cmd in ("start", "help"):
        return help_text(), None
    if cmd == "status":
        return status_text(), STATUS_KB
    if cmd == "report":
        return daily_text(24), None
    if cmd == "rig":
        rigs, err = find_rigs(arg)
        if err:
            return "❌ " + err, None
        return "\n\n".join(rig_text(r) for r in rigs[:5]), None
    if cmd in ("reboot", "shutdown"):
        rigs, err = find_rigs(arg)
        if err:
            return "❌ " + err, None
        names = "、".join(r["name"] or r["id"] for r in rigs)
        data = "cf|%s|%s" % (cmd, arg.strip()[:40])
        return ("确定要对 <b>%s</b> 执行「%s」吗？" % (h(names), ACTION_NAMES[BOT_ACTIONS[cmd]]),
                {"inline_keyboard": [[{"text": "✅ 确定", "callback_data": data}, {"text": "取消", "callback_data": "cancel"}]]})
    if cmd in BOT_ACTIONS:
        return do_rig_action(cmd, arg), None
    if cmd == "mute":
        hrs = fnum(arg or 2, 2)
        sset("tg_mute_until", int(time.time() + hrs * 3600))
        return "🔕 已静音 %g 小时（日报和命令回复不受影响），/unmute 取消" % hrs, None
    if cmd == "unmute":
        sset("tg_mute_until", 0)
        return "🔔 已取消静音", None
    return "未知命令，发送 /help 查看可用命令", None

def tg_handle(u):
    my_chat = sget("tg_chat").strip()
    if "callback_query" in u:
        cq = u["callback_query"]
        msg = cq.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id", ""))
        try:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:
            pass
        if cid != my_chat or sget("tg_bot_cmds") != "1":
            return
        data = cq.get("data", "")
        edit = {"chat_id": cid, "message_id": msg.get("message_id"), "parse_mode": "HTML"}
        if data == "status":
            edit.update(text=status_text(), reply_markup=STATUS_KB)
        elif data == "cancel":
            edit.update(text="已取消")
        elif data.startswith("cf|"):
            _, cmd, target = data.split("|", 2)
            edit.update(text=do_rig_action(cmd, target))
        else:
            return
        try:
            tg_api("editMessageText", edit)
        except Exception as e:
            if "not modified" not in str(e):
                raise
        return
    msg = u.get("message") or {}
    chat = msg.get("chat") or {}
    cid = str(chat.get("id", ""))
    if not cid:
        return
    title = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or ""
    TG_CHATS[cid] = {"id": cid, "title": title, "type": chat.get("type", ""), "ts": int(time.time())}
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split(None, 1)
    cmd = parts[0][1:].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cid != my_chat:
        tg_send_now("这个会话的 Chat ID 是 <code>%s</code>\n请把它填到 MinerHub 面板【设置 → Telegram】里。" % cid, chat=cid)
        return
    if sget("tg_bot_cmds") != "1":
        if cmd in ("start", "help"):
            tg_send_now("✅ 告警推送已连接。机器人命令已在面板中关闭。")
        return
    res = tg_command(cmd, arg)
    if res:
        tg_send_now(res[0], markup=res[1])

def tg_bot_loop():
    """长轮询接收命令（不需要公网 HTTPS / Webhook）"""
    offset, last_token = None, None
    while True:
        token = sget("tg_token").strip()
        if not token:
            last_token = None
            time.sleep(10)
            continue
        if token != last_token:
            try:
                tg_api("deleteWebhook", {})
                me = tg_api("getMe")
                TG_STATUS["bot"] = me.get("username", "")
                tg_api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
                last_token, offset = token, None
                print("Telegram 机器人已连接：@%s" % TG_STATUS["bot"], flush=True)
            except Exception as e:
                TG_STATUS.update(ok=False, err=str(e), ts=int(time.time()))
                time.sleep(60)
                continue
        try:
            p = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                p["offset"] = offset
            ups = tg_api("getUpdates", p, timeout=65)
        except Exception as e:
            if "Conflict" in str(e):
                TG_STATUS.update(ok=False, err="同一个 Bot Token 正被别的程序使用（getUpdates 冲突）", ts=int(time.time()))
            time.sleep(15)
            continue
        for u in ups or []:
            offset = u["update_id"] + 1
            try:
                tg_handle(u)
            except Exception:
                traceback.print_exc()

# ---------------------------------------------------------------- panel API
def make_session():
    ts = str(int(time.time()))
    sig = hmac.new(sget("secret").encode(), ("admin|" + ts).encode(), hashlib.sha256).hexdigest()
    return ts + "." + sig

def check_session(tok):
    try:
        ts, sig = tok.split(".", 1)
        if time.time() - int(ts) > 30 * 86400:
            return False
    except Exception:
        return False
    good = hmac.new(sget("secret").encode(), ("admin|" + ts).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)

def downsample(rows, n=360):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]

SET_KEYS = ["tz", "temp_crit", "temp_resume", "wd_zero_min", "wd_max_restarts", "wd_reboot",
            "tg_token", "tg_chat", "tg_api", "alert_offline", "public_url",
            "tg_ev_online", "tg_ev_temp", "tg_ev_gpu", "tg_ev_watchdog", "tg_ev_hsdrop", "hs_drop_pct",
            "tg_ev_sched", "tg_ev_other", "tg_daily", "tg_daily_at", "tg_report_hours", "tg_bot_cmds", "entrance"]

def public_settings():
    d = {k: sget(k) for k in SET_KEYS}
    d["farm_key"] = sget("farm_key")
    return d

def api_state():
    rigs = [rig_public(r) for r in q("SELECT * FROM rigs ORDER BY name COLLATE NOCASE")]
    return {
        "ok": True, "version": VERSION, "server_time": int(time.time()),
        "local_time": now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "rigs": rigs,
        "flights": q("SELECT * FROM flights ORDER BY name COLLATE NOCASE"),
        "ocs": q("SELECT * FROM oc_profiles ORDER BY name COLLATE NOCASE"),
        "schedules": q("SELECT * FROM schedules ORDER BY at"),
        "settings": public_settings(),
    }

def api_rig_detail(rid, hours=24):
    r = q("SELECT * FROM rigs WHERE id=?", (rid,), True)
    if not r:
        return {"ok": False, "error": "矿机不存在"}
    since = int(time.time()) - int(hours * 3600)
    hist = q("SELECT ts,hs,power,tmax FROM history WHERE rig_id=? AND ts>=? ORDER BY ts", (rid, since))
    d = rig_public(r)
    d.update({
        "ok": True, "history": downsample(hist),
        "events": q("SELECT * FROM events WHERE rig_id=? ORDER BY id DESC LIMIT 60", (rid,)),
        "commands": q("SELECT id,action,status,created,done FROM commands WHERE rig_id=? ORDER BY id DESC LIMIT 20", (rid,)),
        "cfg": rig_cfg(r),
    })
    return d

def save_row(table, fields, body):
    vals = {f: body.get(f) for f in fields}
    for f in fields:
        if isinstance(vals[f], bool):
            vals[f] = 1 if vals[f] else 0
        if vals[f] is None:
            vals[f] = ""
    if body.get("id"):
        x("UPDATE %s SET %s WHERE id=?" % (table, ",".join("%s=?" % f for f in fields)),
          [vals[f] for f in fields] + [int(body["id"])])
        return int(body["id"])
    return x("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(fields), ",".join("?" * len(fields))),
             [vals[f] for f in fields])

FLIGHT_FIELDS = ["name", "miner", "algorithm", "pool", "wallet", "worker", "password", "tls", "tls_cert", "extra"]
OC_FIELDS = ["name", "cclock", "mclock", "coffset", "moffset", "plimit", "fan", "reset", "extra"]
SCHED_FIELDS = ["target", "action", "at", "days", "enabled", "note"]

def handle_api(method, path, body, qs):
    if path == "/api/state":
        return api_state()
    m = re.match(r"^/api/rig/([\w.\-]+)$", path)
    if m and method == "GET":
        return api_rig_detail(m.group(1), fnum((qs.get("hours") or ["24"])[0], 24))
    m = re.match(r"^/api/rig/([\w.\-]+)/(save|delete|cmd)$", path)
    if m and method == "POST":
        rid, op = m.groups()
        if op == "save":
            fid = int(body["flight_id"]) if body.get("flight_id") else None
            oid = int(body["oc_id"]) if body.get("oc_id") else None
            x("UPDATE rigs SET name=?, flight_id=?, oc_id=?, notes=? WHERE id=?",
              (str(body.get("name") or rid)[:64], fid, oid, str(body.get("notes") or "")[:500], rid))
            add_event(rid, "info", "配置已修改（飞行表/超频），矿工将自动重启应用")
            return {"ok": True}
        if op == "delete":
            for t in ("rigs",):
                x("DELETE FROM %s WHERE id=?" % t, (rid,))
            x("DELETE FROM history WHERE rig_id=?", (rid,))
            x("DELETE FROM commands WHERE rig_id=?", (rid,))
            return {"ok": True}
        ok, msg, cid = issue(rid, body.get("action", ""), body.get("args") or {})
        return {"ok": ok, "msg": msg, "error": None if ok else msg, "cmd_id": cid}
    if path == "/api/cmd/bulk" and method == "POST":
        res = []
        for rid in body.get("ids") or []:
            ok, msg, cid = issue(rid, body.get("action", ""), body.get("args") or {})
            res.append({"id": rid, "ok": ok, "msg": msg})
        return {"ok": True, "results": res}
    m = re.match(r"^/api/cmd/(\d+)$", path)
    if m:
        c = q("SELECT * FROM commands WHERE id=?", (int(m.group(1)),), True)
        return {"ok": bool(c), "cmd": c, "error": None if c else "not found"}
    m = re.match(r"^/api/(flight|oc|schedule)/(save|(\d+)/delete)$", path)
    if m and method == "POST":
        kind = m.group(1)
        table, fields = {"flight": ("flights", FLIGHT_FIELDS), "oc": ("oc_profiles", OC_FIELDS),
                         "schedule": ("schedules", SCHED_FIELDS)}[kind]
        if m.group(3):
            i = int(m.group(3))
            x("DELETE FROM %s WHERE id=?" % table, (i,))
            if kind == "flight":
                x("UPDATE rigs SET flight_id=NULL WHERE flight_id=?", (i,))
            if kind == "oc":
                x("UPDATE rigs SET oc_id=NULL WHERE oc_id=?", (i,))
            return {"ok": True}
        if kind == "oc":
            for k in OC_LIST_FIELDS:
                v = norm_list(body.get(k))
                bad = [p for p in v.split(",") if p and not re.match(r"^-?\d+$", p)]
                if bad:
                    return {"ok": False, "error": "超频「%s」里有无效的值：%s（只能填整数，用空格或逗号分隔）" % (k, " ".join(bad))}
                body[k] = v
        if kind == "flight" and not all(str(body.get(k) or "").strip() for k in ("name", "algorithm", "pool", "wallet")):
            return {"ok": False, "error": "名称 / 算法 / 矿池 / 钱包 都必须填写"}
        if kind == "schedule":
            if not re.match(r"^\d{1,2}:\d{2}$", str(body.get("at") or "")):
                return {"ok": False, "error": "时间格式应为 HH:MM"}
            hh, mm = body["at"].split(":")
            body["at"] = "%02d:%02d" % (int(hh), int(mm))
            if body.get("action") not in ("poweron", "start_miner", "stop_miner", "restart_miner", "reboot", "shutdown"):
                return {"ok": False, "error": "动作不正确"}
        return {"ok": True, "id": save_row(table, fields, body)}
    if path == "/api/events":
        return {"ok": True, "events": q("SELECT e.*, r.name AS rig_name FROM events e LEFT JOIN rigs r ON r.id=e.rig_id ORDER BY e.id DESC LIMIT 300")}
    if path == "/api/settings" and method == "POST":
        for k in SET_KEYS:
            if k in body:
                v = body[k]
                if k == "tz" and ZoneInfo:
                    try:
                        ZoneInfo(v)
                    except Exception:
                        return {"ok": False, "error": "时区无效，例如 Asia/Shanghai、America/New_York"}
                if k == "entrance":
                    v = str(v).strip().strip("/")
                    if v and not re.match(r"^[A-Za-z0-9_-]{6,64}$", v):
                        return {"ok": False, "error": "安全入口只能用字母、数字、- 和 _，长度 6~64"}
                    if v.lower() in ("api", "dl"):
                        return {"ok": False, "error": "安全入口不能是 api 或 dl"}
                if k == "tg_daily_at" and not re.match(r"^\d{1,2}:\d{2}$", str(v)):
                    return {"ok": False, "error": "日报时间格式应为 HH:MM"}
                if k in ("tg_token", "tg_chat", "tg_api") and isinstance(v, str):
                    v = v.strip()
                sset(k, "1" if v is True else "0" if v is False else v)
        if body.get("new_password"):
            if len(body["new_password"]) < 6:
                return {"ok": False, "error": "密码至少 6 位"}
            sset("admin_pass", hash_pw(body["new_password"]))
            sset("secret", secrets.token_hex(32))   # 让旧会话失效
            return {"ok": True, "relogin": True}
        return {"ok": True}
    if path == "/api/settings/rotate_key" and method == "POST":
        sset("farm_key", secrets.token_urlsafe(24))
        return {"ok": True, "farm_key": sget("farm_key")}
    if path == "/api/settings/test_tg" and method == "POST":
        try:
            tg_send_now("✅ MinerHub 测试消息：Telegram 通知已配置成功\n发送 /help 查看机器人命令")
        except Exception as e:
            return {"ok": False, "error": "发送失败：%s" % e}
        return {"ok": True, "msg": "测试消息已发送"}
    if path == "/api/settings/tg_status_now" and method == "POST":
        try:
            tg_send_now(status_text(), markup=STATUS_KB)
            if body.get("daily"):
                tg_send_now(daily_text(24))
        except Exception as e:
            return {"ok": False, "error": "发送失败：%s" % e}
        return {"ok": True, "msg": "已发送"}
    if path == "/api/settings/tg_info":
        chats = sorted(TG_CHATS.values(), key=lambda c: -c["ts"])
        return {"ok": True, "status": TG_STATUS, "chats": chats, "muted": tg_muted(),
                "mute_until": int(fnum(sget("tg_mute_until"), 0))}
    if path == "/api/settings/tg_unmute" and method == "POST":
        sset("tg_mute_until", 0)
        return {"ok": True, "msg": "已取消静音"}
    return {"ok": False, "error": "not found"}

FAILS = {}
GFAILS = []      # 全局失败记录：10 分钟内全站失败 50 次则暂停登录（防多 IP 轮换爆破）

def entrance():
    return re.sub(r"[^A-Za-z0-9_-]", "", sget("entrance") or "")

class Handler(BaseHTTPRequestHandler):
    server_version = "MinerHub/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8") or "{}") if raw else {}

    def _ip(self):
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):     # 只有本机反向代理（1Panel/Nginx）传来的才可信
            v = self.headers.get("X-Real-IP")
            if v:
                return v.strip()
            v = self.headers.get("X-Forwarded-For")
            if v:
                return v.split(",")[-1].strip()     # 取最后一段（代理追加的），前面的可被客户端伪造
        return peer

    def _redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _404(self):
        return self._send(404, b"404 page not found", "text/plain; charset=utf-8")

    def _authed(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return "mh_session" in c and check_session(c["mh_session"].value)

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)
        try:
            body = self._body() if method == "POST" else {}
            if path.startswith("/api/agent/"):
                if not hmac.compare_digest(self.headers.get("X-Farm-Key", ""), sget("farm_key")):
                    return self._json({"ok": False, "error": "Farm Key 错误"}, 403)
                if path == "/api/agent/report":
                    return self._json(agent_report(body, self._ip()))
                if path == "/api/agent/ack":
                    return self._json(agent_ack(body))
                return self._json({"ok": False, "error": "not found"}, 404)
            if path.startswith("/dl/"):                 # 矿机安装 / 更新用的公开文件（不含任何密钥）
                return self.static(path)
            ent = entrance()
            cookie_path = "/"
            if ent:                                     # 安全入口：只有 /入口/ 下面才是面板，其它路径一律 404
                pre = "/" + ent
                if path == pre:
                    return self._redirect(pre + "/" + ("?" + u.query if u.query else ""))
                if not path.startswith(pre + "/"):
                    return self._404()
                path, cookie_path = path[len(pre):], pre + "/"
            if path == "/api/login" and method == "POST":
                ip = self._ip()
                fails = [t for t in FAILS.get(ip, []) if time.time() - t < 600]
                if len(fails) >= 8 or len([t for t in GFAILS if time.time() - t < 600]) >= 50:
                    return self._json({"ok": False, "error": "失败次数过多，10 分钟后再试"}, 429)
                if check_pw(str(body.get("password", "")), sget("admin_pass")):
                    FAILS.pop(ip, None)
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                    return self._json({"ok": True}, headers={
                        "Set-Cookie": "mh_session=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=2592000%s" % (make_session(), cookie_path, secure)})
                FAILS[ip] = fails + [time.time()]
                GFAILS.append(time.time())
                del GFAILS[:-200]
                if len(FAILS[ip]) == 8:
                    add_event(None, "warn", "IP %s 连续 8 次密码错误，已锁定 10 分钟" % ip, cat="other")
                time.sleep(1)
                return self._json({"ok": False, "error": "密码错误"}, 403)
            if path == "/api/logout":
                return self._json({"ok": True}, headers={"Set-Cookie": "mh_session=; Path=%s; Max-Age=0" % cookie_path})
            if path.startswith("/api/"):
                if not self._authed():
                    return self._json({"ok": False, "error": "unauthorized"}, 401)
                return self._json(handle_api(method, path, body, qs))
            return self.static(path)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        fp = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not fp.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(fp):
            return self._send(404, b"404 not found", "text/plain")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if fp.endswith((".py", ".sh")):
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

def cli():
    init_db(quiet=True)
    if len(sys.argv) >= 3 and sys.argv[1] == "passwd":
        sset("admin_pass", hash_pw(sys.argv[2]))
        sset("secret", secrets.token_hex(32))
        print("管理员密码已更新")
    elif len(sys.argv) >= 3 and sys.argv[1] == "entrance":
        v = sys.argv[2].strip().strip("/")
        if v.lower() in ("off", "none", "-"):
            sset("entrance", "")
            print("已关闭安全入口：面板地址恢复为 http://IP:%d/" % PORT)
        elif not re.match(r"^[A-Za-z0-9_-]{6,64}$", v) or v.lower() in ("api", "dl"):
            print("安全入口只能用字母、数字、- 和 _，长度 6~64")
            sys.exit(1)
        else:
            sset("entrance", v)
            print("安全入口已设置，面板地址：http://IP:%d/%s/   （立即生效，无需重启）" % (PORT, v))
    elif len(sys.argv) >= 2 and sys.argv[1] == "info":
        url = sget("public_url") or os.environ.get("MINERHUB_PUBLIC_URL") or "http://面板IP:%d" % PORT
        print("面板地址 : %s/%s" % (url, (entrance() + "/") if entrance() else ""))
        print("Farm Key : %s" % sget("farm_key"))
        print("矿机安装 : curl -fsSL %s/dl/install-agent.sh | sudo bash -s -- %s %s" % (url, url, sget("farm_key")))
    else:
        print(__doc__)

def main():
    if len(sys.argv) > 1:
        return cli()
    init_db()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=tg_sender_loop, daemon=True).start()
    threading.Thread(target=tg_bot_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("MinerHub %s 已启动: http://%s:%d  （时区 %s）" % (VERSION, HOST, PORT, sget("tz")), flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
