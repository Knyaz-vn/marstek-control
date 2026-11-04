#!/usr/bin/env python3
# Marstek Venus E 3.0: агент керування зарядом.
# Тарифи день/ніч, графік ВОЕ, температура, Telegram і дашборд на GitHub Pages
# (обмін даними через приватний GitHub Gist). Лише стандартна бібліотека Python 3.
#
#   python agent.py          запуск
#   python agent.py --setup  майстер налаштування
#   python agent.py --test   перевірка зв'язку зі станцією, GitHub і Telegram

import datetime as dt
import json
import os
import random
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

VERSION = "2.0"
BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
SETTINGS_PATH = os.path.join(BASE, "settings.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "agent.log")
GIST_DESC = "marstek-agent"

DEFAULTS = {
    "marstek_ip": "",
    "marstek_port": 30000,
    "local_port": 30000,
    "udp_timeout": 5,

    "capacity_wh": 5120,
    "min_soc": 12,
    "charge_power_w": 2500,
    "charge_sign": -1,
    "bat_power_charge_positive": True,
    "eff_charge": 0.92,
    "eff_discharge": 0.94,

    "day_target": 50,
    "night_target": 100,
    "outage_target": 80,
    "min_outage_hours": 3,
    "night_start": "23:00",
    "night_end": "07:00",
    "prep_margin_min": 30,
    "reserve_pct": 10,
    "hysteresis": 3,
    "hold_mode": "passive0",
    "off_mode": "auto",

    "queue": "3.1",
    "schedule_url": "https://raw.githubusercontent.com/vn-progr/gpv-voe-vinnytsia/main/data/Vinnytsiaoblenerho.json",
    "schedule_refresh_sec": 600,

    "temp_warn": 45,
    "temp_crit": 52,
    "temp_low": 5,
    "temp_repeat_min": 30,
    "low_soc_alert": 25,
    "default_load_w": 250,
    "outage_detect_w": 40,

    "github_token": "",
    "gist_id": "",
    "github_api": "https://api.github.com",
    "push_live_sec": 60,
    "push_idle_sec": 900,
    "poll_sec": 30,

    "tg_token": "",
    "tg_chat_id": 0,
    "tg_api": "https://api.telegram.org",

    "sample_sec": 30,
    "control_sec": 60,
    "history_step_sec": 120,
    "passive_seconds": 300,
    "resend_sec": 120,
}

# Що можна міняти з дашборда і Telegram: ключ -> (мін, макс, тип)
EDITABLE = {
    "day_target": (10, 100, int),
    "night_target": (10, 100, int),
    "outage_target": (10, 100, int),
    "min_outage_hours": (0.5, 12, float),
    "night_start": (None, None, str),
    "night_end": (None, None, str),
    "charge_power_w": (200, 2500, int),
    "temp_warn": (30, 60, int),
    "temp_crit": (35, 65, int),
    "temp_low": (-10, 15, int),
    "low_soc_alert": (5, 60, int),
    "queue": (None, None, str),
    "hold_mode": (None, None, str),
}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


USER_CFG = load_json(CFG_PATH, {})
SETTINGS = load_json(SETTINGS_PATH, {})
CFG = {}


def rebuild_cfg():
    CFG.clear()
    CFG.update(DEFAULTS)
    CFG.update(USER_CFG)
    CFG.update(SETTINGS)


rebuild_cfg()

STATE = load_json(STATE_PATH, {})
for k, v in {
    "enabled": True, "force_target": None, "tg_offset": 0,
    "outages": [], "profile": None, "history": [], "alerts": [],
    "acked": [], "temp_level": "ok", "bat_sign": None,
}.items():
    STATE.setdefault(k, v)
if not STATE["profile"] or len(STATE["profile"]) != 24:
    STATE["profile"] = [float(CFG["default_load_w"])] * 24

# Оперативні дані, що не зберігаються на диск
RT = {
    "tele": {}, "online": False, "last_seen": 0, "fail_count": 0,
    "samples": [], "hour_acc": [None, 0.0, 0],
    "outage": None, "out_cnt": 0, "end_cnt": 0,
    "decision": {"action": "init", "target": None, "reason": "запуск"},
    "last_key": None, "last_sent": 0, "charging": False, "charge_start_soc": None,
    "temp_alert_ts": 0, "viewer_ts": 0, "push_ts": 0, "push_now": True,
    "poll_ts": 0, "sample_ts": 0, "control_ts": 0, "save_ts": 0,
    "sample_n": 0, "last_cmd": None, "low_soc_sent": False, "prep": {},
}


def now_ts():
    return time.time()


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fmt_t(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def fmt_dt(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} год {m:02d} хв" if h else f"{m} хв"


# ---------------- Marstek Open API (UDP JSON-RPC) ----------------
_rpc_id = random.randint(1000, 90000)


def rpc(method, params, timeout=None, retries=2):
    global _rpc_id
    timeout = timeout or float(CFG.get("udp_timeout", 5))
    last_err = None
    for attempt in range(retries + 1):
        _rpc_id += 1
        req = json.dumps({"id": _rpc_id, "method": method, "params": params}).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(timeout)
        try:
            try:
                s.bind(("", int(CFG.get("local_port", 0))))
            except OSError:
                s.bind(("", 0))
            s.sendto(req, (CFG["marstek_ip"], int(CFG["marstek_port"])))
            deadline = time.time() + timeout
            while time.time() < deadline:
                data, _ = s.recvfrom(16384)
                try:
                    resp = json.loads(data.decode(errors="ignore"))
                except ValueError:
                    continue
                if resp.get("id") != _rpc_id or "method" in resp:   # чужа відповідь або власне відлуння
                    continue
                if "error" in resp:
                    raise RuntimeError(f"помилка станції {resp['error']}")
                return resp.get("result", {}) or {}
            raise TimeoutError("немає відповіді")
        except Exception as e:
            last_err = e
            time.sleep(0.6 + attempt)
        finally:
            s.close()
    raise RuntimeError(f"{method}: {last_err}")


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def read_station():
    """Зчитує стан станції. bat_w > 0 означає заряд, < 0 розряд."""
    t = {"ts": now_ts()}
    es, bat = {}, {}
    errors = []
    try:
        es = rpc("ES.GetStatus", {"id": 0})
    except Exception as e:
        errors.append(str(e))
    time.sleep(0.25)
    try:
        bat = rpc("Bat.GetStatus", {"id": 0})
    except Exception as e:
        errors.append(str(e))
    if not es and not bat:
        raise RuntimeError("; ".join(errors))

    t["soc"] = num(es.get("bat_soc"))
    if t["soc"] is None:
        t["soc"] = num(bat.get("soc"))
    temp = num(bat.get("bat_temp"))
    if temp is not None and abs(temp) > 100:   # деякі прошивки віддають десяті частки
        temp = temp / 10.0
    t["temp"] = temp
    t["load_w"] = num(es.get("offgrid_power"))
    t["grid_w"] = num(es.get("ongrid_power"))
    t["cap_wh"] = num(es.get("bat_cap")) or num(bat.get("rated_capacity"))
    t["charge_ok"] = bat.get("charg_flag")
    t["discharge_ok"] = bat.get("dischrg_flag")

    raw = num(es.get("bat_power"))
    if raw is not None:
        sign = STATE.get("bat_sign")
        if sign is None:
            sign = 1 if CFG["bat_power_charge_positive"] else -1
        t["bat_w"] = raw * sign
        t["bat_raw"] = raw
    else:
        t["bat_w"] = None

    RT["sample_n"] += 1
    if RT["sample_n"] % 5 == 1:
        try:
            time.sleep(0.25)
            t["mode"] = rpc("ES.GetMode", {"id": 0}).get("mode")
        except Exception:
            pass
    else:
        t["mode"] = RT["tele"].get("mode")
    if RT["sample_n"] % 20 == 1:
        try:
            time.sleep(0.25)
            t["rssi"] = rpc("Wifi.GetStatus", {"id": 0}).get("rssi")
        except Exception:
            pass
        try:
            time.sleep(0.25)
            d = rpc("Marstek.GetDevice", {"ble_mac": "0"})
            t["device"], t["fw"] = d.get("device"), d.get("ver")
            mac = d.get("wifi_mac") or d.get("ble_mac")
            if mac and USER_CFG.get("marstek_mac") != mac:
                USER_CFG["marstek_mac"] = mac
                save_json(CFG_PATH, USER_CFG)
                rebuild_cfg()
        except Exception:
            pass
    for k in ("rssi", "device", "fw"):
        t.setdefault(k, RT["tele"].get(k))
    return t


def set_passive(power_w, seconds):
    cfg = {"mode": "Passive", "passive_cfg": {"power": int(power_w), "cd_time": int(seconds)}}
    return rpc("ES.SetMode", {"id": 0, "config": cfg})


def set_auto():
    return rpc("ES.SetMode", {"id": 0, "config": {"mode": "Auto", "auto_cfg": {"enable": 1}}})


def set_ups():
    return rpc("ES.SetMode", {"id": 0, "config": {"mode": "UPS", "ups_cfg": {"enable": 1}}})


def apply_hold():
    m = CFG["hold_mode"]
    if m == "auto":
        set_auto()
    elif m == "ups":
        set_ups()
    else:
        set_passive(0, CFG["passive_seconds"])


def apply_off():
    set_ups() if CFG["off_mode"] == "ups" else set_auto()


# ---------------- Графік відключень ----------------
_sched = {"data": None, "ts": 0, "err": None}


def fetch_schedule(force=False):
    if not force and _sched["data"] and now_ts() - _sched["ts"] < CFG["schedule_refresh_sec"]:
        return _sched["data"]
    try:
        url = CFG["schedule_url"]
        url += ("&" if "?" in url else "?") + f"t={int(now_ts())}"
        req = urllib.request.Request(url, headers={"User-Agent": "marstek-agent"})
        with urllib.request.urlopen(req, timeout=20) as r:
            _sched["data"] = json.loads(r.read().decode())
        _sched["ts"] = now_ts()
        _sched["err"] = None
    except Exception as e:
        _sched["err"] = str(e)
        _sched["ts"] = now_ts() - CFG["schedule_refresh_sec"] + 120  # повтор через 2 хв
        log(f"Графік не завантажився: {e}")
    return _sched["data"]


def outage_blocks():
    """Список (start_ts, end_ts) відключень своєї черги на сьогодні і завтра."""
    sched = fetch_schedule()
    if not sched:
        return []
    key = "GPV" + str(CFG["queue"]).replace("GPV", "")
    halves = []
    for day_ts, groups in sched.get("fact", {}).get("data", {}).items():
        g = groups.get(key)
        if not g:
            continue
        day = dt.datetime.fromtimestamp(int(day_ts))
        for h in range(1, 25):
            v = g.get(str(h), "yes")
            base = (day + dt.timedelta(hours=h - 1)).timestamp()
            if v == "no":
                halves += [base, base + 1800]
            elif v == "first":      # нема світла другу половину години
                halves.append(base + 1800)
            elif v == "second":     # нема світла першу половину години
                halves.append(base)
    halves.sort()
    blocks = []
    for t in halves:
        if blocks and abs(blocks[-1][1] - t) < 1:
            blocks[-1][1] = t + 1800
        else:
            blocks.append([t, t + 1800])
    return [(s, e) for s, e in blocks]


def schedule_updated():
    s = _sched.get("data") or {}
    return (s.get("fact") or {}).get("update")


# ---------------- Прогноз і логіка ----------------
def parse_hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def is_night(when):
    m = when.hour * 60 + when.minute
    a, b = parse_hm(CFG["night_start"]), parse_hm(CFG["night_end"])
    return (m >= a or m < b) if a > b else (a <= m < b)


def forecast_wh(start, end):
    """Очікуване споживання квартири між start і end за профілем по годинах."""
    wh, t = 0.0, start
    prof = STATE["profile"]
    while t < end:
        step = min(900, end - t)
        h = dt.datetime.fromtimestamp(t).hour
        wh += max(50.0, prof[h]) * step / 3600
        t += step
    return wh


def usable_wh(soc):
    return max(0.0, (soc - CFG["min_soc"]) / 100 * CFG["capacity_wh"] * CFG["eff_discharge"])


def required_soc(start, end):
    need = forecast_wh(start, end) * 1.15
    soc = CFG["min_soc"] + need / (CFG["capacity_wh"] * CFG["eff_discharge"]) * 100 + CFG["reserve_pct"]
    return min(100, int(round(soc + 0.49)))


def temp_level(t, prev):
    if t is None:
        return prev or "ok"
    crit, warn, low = CFG["temp_crit"], CFG["temp_warn"], CFG["temp_low"]
    if t >= crit:
        return "crit"
    if t >= warn:
        return "warn"
    if prev == "crit" and t >= crit - 3:
        return "crit"
    if prev in ("warn", "crit") and t >= warn - 3:
        return "warn"
    if t <= low:
        return "low"
    if prev == "low" and t <= low + 2:
        return "low"
    return "ok"


def charge_power():
    p = CFG["charge_power_w"]
    if STATE["temp_level"] == "warn":
        p = p // 2
    return p


def decide(now, soc):
    """Повертає словник: action (charge|hold|off|outage), target, reason."""
    if not STATE["enabled"]:
        return {"action": "off", "target": None, "reason": "автоматика вимкнена"}
    if RT["outage"]:
        return {"action": "outage", "target": None, "reason": "немає світла, живлення від батареї"}
    if STATE["temp_level"] == "crit":
        return {"action": "hold", "target": None,
                "reason": f"заряд зупинено: температура понад {CFG['temp_crit']} °C"}

    if STATE.get("force_target") is not None:
        target, reason = int(STATE["force_target"]), "ручна ціль"
    elif is_night(now):
        target, reason = int(CFG["night_target"]), "нічний тариф"
    else:
        target, reason = int(CFG["day_target"]), "денний тариф"

    n = now.timestamp()
    RT["prep"] = {k: v for k, v in RT["prep"].items() if v[0] > n}
    min_len = CFG["min_outage_hours"] * 3600
    kw = charge_power() / 1000 * CFG["eff_charge"]
    for s, e in outage_blocks():
        if e <= n or s - n > 24 * 3600:
            continue
        req = required_soc(max(s, n), e)
        t_for = max(int(CFG["outage_target"]) if (e - s) >= min_len else 0, req)
        if t_for <= target:
            continue
        need_h = max(0.0, t_for - soc) / 100 * CFG["capacity_wh"] / 1000 / max(kw, 0.1)
        start_at = s - need_h * 3600 - CFG["prep_margin_min"] * 60
        latched = RT["prep"].get(int(s))
        if latched or n >= start_at:
            # підготовку, що вже почалась, тримаємо до кінця блоку, інакше заряд смикався б
            if latched:
                t_for = max(t_for, latched[1])
            RT["prep"][int(s)] = [e, t_for]
            target = t_for
            where = "зараз за графіком" if s <= n else f"{fmt_dt(s)}"
            reason = f"підготовка до відключення {where}-{fmt_t(e)}"
            if req > int(CFG["outage_target"]):
                reason += f" (прогноз потреби {req}%)"

    hyst = CFG["hysteresis"]
    if RT["charging"]:
        action = "charge" if soc < target else "hold"
    else:
        action = "charge" if soc <= target - hyst else "hold"
    return {"action": action, "target": target, "reason": reason}


# ---------------- Сповіщення ----------------
def tg_api(method, params, timeout=20):
    token = CFG.get("tg_token")
    if not token:
        return None
    url = f"{CFG['tg_api']}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg_send(text):
    if not (CFG.get("tg_token") and CFG.get("tg_chat_id")):
        return
    try:
        tg_api("sendMessage", {"chat_id": CFG["tg_chat_id"], "text": text}, timeout=15)
    except Exception as e:
        log(f"Telegram: {e}")


def alert(level, text):
    """level: info | warn | crit. Пише в журнал дашборда і в Telegram."""
    icon = {"info": "ℹ️", "warn": "⚠️", "crit": "🔥"}.get(level, "")
    STATE["alerts"] = (STATE["alerts"] + [{"ts": int(now_ts()), "level": level, "text": text}])[-30:]
    RT["push_now"] = True
    log(f"[{level}] {text}")
    tg_send(f"{icon} {text}".strip())


def check_temperature(t):
    temp = t.get("temp")
    prev = STATE["temp_level"]
    lvl = temp_level(temp, prev)
    if lvl != prev:
        STATE["temp_level"] = lvl
        RT["temp_alert_ts"] = now_ts()
        if lvl == "crit":
            alert("crit", f"Критична температура батареї: {temp:.1f} °C. Примусовий заряд зупинено.")
        elif lvl == "warn" and prev == "ok":
            alert("warn", f"Висока температура батареї: {temp:.1f} °C. Потужність заряду знижено вдвічі.")
        elif lvl == "warn" and prev == "crit":
            alert("warn", f"Температура знизилась до {temp:.1f} °C, але ще висока. Заряд на половині потужності.")
        elif lvl == "low":
            alert("warn", f"Низька температура батареї: {temp:.1f} °C. При 0 °C і нижче станція не заряджається.")
        elif lvl == "ok":
            alert("info", f"Температура батареї в нормі: {temp:.1f} °C.")
    elif lvl in ("crit", "warn", "low") and temp is not None:
        repeat = CFG["temp_repeat_min"] * 60 * (1 if lvl == "crit" else 4)
        if now_ts() - RT["temp_alert_ts"] > repeat:
            RT["temp_alert_ts"] = now_ts()
            name = {"crit": "Критична", "warn": "Висока", "low": "Низька"}[lvl]
            alert("crit" if lvl == "crit" else "warn", f"{name} температура тримається: {temp:.1f} °C.")


# ---------------- Відключення і профіль споживання ----------------
def track_outage(t):
    bat = t.get("bat_w")
    if bat is None:
        return
    discharge = -bat if bat < 0 else 0.0
    load = t.get("load_w") or 0.0
    thr = CFG["outage_detect_w"]
    o = RT["outage"]
    if o is None:
        if discharge > thr and discharge > 0.4 * max(load, 1):
            RT["out_cnt"] += 1
        else:
            RT["out_cnt"] = 0
        if RT["out_cnt"] >= 2:
            soc = t.get("soc") or 0
            RT["outage"] = {"start": int(t["ts"] - CFG["sample_sec"]), "wh": 0.0,
                            "soc_start": soc, "last": t["ts"]}
            RT["end_cnt"] = 0
            RT["low_soc_sent"] = False
            alert("info", f"Світло зникло близько {fmt_t(RT['outage']['start'])}. Заряд {soc:.0f}%, "
                          f"навантаження {max(load, discharge):.0f} Вт, вистачить приблизно на "
                          f"{fmt_dur(usable_wh(soc) / max(load, discharge, 50) * 3600)}.")
    else:
        dt_h = max(0.0, min(t["ts"] - o["last"], 300)) / 3600
        o["wh"] += max(load, discharge) * dt_h
        o["last"] = t["ts"]
        soc = t.get("soc") or 0
        if not RT["low_soc_sent"] and soc <= CFG["low_soc_alert"]:
            RT["low_soc_sent"] = True
            alert("warn", f"Заряд батареї {soc:.0f}% під час відключення. Варто зменшити навантаження.")
        if discharge < thr / 2:
            RT["end_cnt"] += 1
        else:
            RT["end_cnt"] = 0
        if RT["end_cnt"] >= 2:
            rec = {"start": o["start"], "end": int(t["ts"]), "wh": round(o["wh"]),
                   "soc_start": o["soc_start"], "soc_end": soc}
            STATE["outages"] = (STATE["outages"] + [rec])[-30:]
            RT["outage"] = None
            alert("info", f"Світло повернулось о {fmt_t(rec['end'])}. Тривалість {fmt_dur(rec['end'] - rec['start'])}, "
                          f"витрачено {rec['wh']} Вт·год, заряд {rec['soc_start']:.0f}% → {soc:.0f}%.")


def track_profile(t):
    load = t.get("load_w")
    if load is None or (load <= 0 and not RT["outage"]):
        return
    h = dt.datetime.fromtimestamp(t["ts"]).hour
    acc = RT["hour_acc"]
    if acc[0] != h:
        if acc[0] is not None and acc[2] >= 10:
            avg = acc[1] / acc[2]
            p = STATE["profile"]
            p[acc[0]] = round(0.7 * p[acc[0]] + 0.3 * avg, 1)
        RT["hour_acc"] = [h, 0.0, 0]
        acc = RT["hour_acc"]
    acc[1] += load
    acc[2] += 1


def track_history(t):
    RT["samples"].append(t)
    step = CFG["history_step_sec"]
    first = RT["samples"][0]["ts"]
    if t["ts"] - first < step:
        return
    pts = RT["samples"]
    RT["samples"] = []

    def avg(k):
        vals = [p[k] for p in pts if p.get(k) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None
    STATE["history"].append([int(t["ts"]), avg("soc"), avg("bat_w"), avg("load_w"), avg("temp")])
    cutoff = now_ts() - 26 * 3600
    STATE["history"] = [p for p in STATE["history"] if p[0] >= cutoff]


def calibrate_sign(t):
    """Під час примусового заряду, коли заряд реально росте, знак потужності відомий."""
    start = RT["charge_start_soc"]
    if (not RT["charging"] or t.get("bat_raw") is None or abs(t["bat_raw"]) < 300
            or start is None or (t.get("soc") or 0) < start + 1):
        RT["calib"] = 0
        return
    if t["bat_w"] < 0:
        RT["calib"] = RT.get("calib", 0) + 1
        if RT["calib"] >= 3:
            cur = STATE.get("bat_sign") or (1 if CFG["bat_power_charge_positive"] else -1)
            STATE["bat_sign"] = -cur
            RT["calib"] = 0
            log(f"Знак bat_power виправлено автоматично на {STATE['bat_sign']}")
    else:
        RT["calib"] = 0


# ---------------- Основний крок ----------------
def rediscover():
    """Якщо роутер видав станції нову IP-адресу, знаходимо її і переналаштовуємось."""
    found = discover()
    mac = CFG.get("marstek_mac")
    match = [f for f in found if mac and f.get("mac") == mac] or (found if len(found) == 1 else [])
    if not match:
        log("Пошук станції в мережі: не знайдено")
        return False
    ip = match[0]["ip"]
    if ip == CFG["marstek_ip"]:
        return False
    old = CFG["marstek_ip"]
    USER_CFG["marstek_ip"] = ip
    save_json(CFG_PATH, USER_CFG)
    rebuild_cfg()
    alert("info", f"Станція змінила IP-адресу з {old} на {ip}. Агент переналаштувався сам.")
    return True


def sample_step():
    try:
        t = read_station()
    except Exception as e:
        RT["fail_count"] += 1
        log(f"Опитування станції: {e}")
        if RT["fail_count"] >= 2 and now_ts() - RT.get("disc_ts", 0) > 300:
            RT["disc_ts"] = now_ts()
            if rediscover():
                return None
        if RT["online"] and RT["fail_count"] >= 3:
            RT["online"] = False
            alert("warn", f"Станція не відповідає ({e}). Перевір Wi-Fi і живлення роутера.")
        return None
    if not RT["online"] and RT["last_seen"]:
        alert("info", "Зв'язок зі станцією відновлено.")
    RT["online"], RT["fail_count"], RT["last_seen"] = True, 0, t["ts"]
    RT["tele"] = t
    calibrate_sign(t)
    check_temperature(t)
    track_outage(t)
    track_profile(t)
    track_history(t)
    return t


def control_step():
    t = RT["tele"]
    if not RT["online"] or t.get("soc") is None:
        return
    soc = t["soc"]
    d = decide(dt.datetime.now(), soc)
    action = d["action"]
    key = f"{action}|{d['target']}|{d['reason']}|{charge_power()}|{CFG['hold_mode']}"
    changed = key != RT["last_key"]
    since = now_ts() - RT["last_sent"]

    try:
        if action == "charge":
            if not RT["charging"]:
                RT["charge_start_soc"] = soc
            if changed or since > CFG["resend_sec"]:
                set_passive(CFG["charge_sign"] * charge_power(), CFG["passive_seconds"])
                RT["last_sent"] = now_ts()
            start = RT["charge_start_soc"]
            if start is not None and soc <= start - 2:
                STATE["enabled"] = False
                apply_off()
                alert("crit", "Під час примусового заряду батарея розряджається. Автоматику вимкнено. "
                              "Перевір charge_sign у config.json (-1 або 1).")
                return
        elif action in ("hold", "outage"):
            refresh = CFG["resend_sec"] if CFG["hold_mode"] == "passive0" else 1800
            if changed or since > refresh:
                apply_hold()
                RT["last_sent"] = now_ts()
        elif action == "off" and changed:
            apply_off()
            RT["last_sent"] = now_ts()
    except Exception as e:
        log(f"Команда станції ({action}): {e}")
        return

    RT["charging"] = action == "charge"
    if action != "charge":
        RT["charge_start_soc"] = None
    if changed:
        RT["decision"] = d
        RT["last_key"] = key
        RT["push_now"] = True
        tgt = f" до {d['target']}%" if d["target"] is not None and action == "charge" else ""
        names = {"charge": "заряд", "hold": "утримання", "off": "вимкнено", "outage": "відключення"}
        log(f"SOC {soc:.0f}% | {names.get(action, action)}{tgt} | {d['reason']}")
        tg_key = f"{action}|{d['target']}|{d['reason']}"
        if tg_key != RT.get("tg_key") and action in ("charge", "off"):
            tg_send(f"🔋 {soc:.0f}% · {names.get(action, action)}{tgt} · {d['reason']}")
        RT["tg_key"] = tg_key
    RT["decision"] = d


# ---------------- Команди (Telegram і дашборд) ----------------
def apply_setting(key, value):
    if key not in EDITABLE:
        raise ValueError(f"невідомий параметр {key}")
    lo, hi, typ = EDITABLE[key]
    if typ is str:
        value = str(value).strip()
        if key in ("night_start", "night_end"):
            parse_hm(value)
            value = f"{parse_hm(value) // 60:02d}:{parse_hm(value) % 60:02d}"
        if key == "hold_mode" and value not in ("passive0", "auto", "ups"):
            raise ValueError("hold_mode: passive0, auto або ups")
        if key == "queue":
            value = value.replace("GPV", "")
            if value not in [f"{a}.{b}" for a in range(1, 7) for b in (1, 2)]:
                raise ValueError("черга у форматі 3.1")
    else:
        value = typ(value)
        value = max(lo, min(hi, value))
    SETTINGS[key] = value
    save_json(SETTINGS_PATH, SETTINGS)
    rebuild_cfg()
    RT["prep"] = {}
    if key == "queue":
        fetch_schedule(force=True)
    return value


def handle_command(cmd, value=None):
    cmd = (cmd or "").lower().lstrip("/")
    if cmd == "on":
        STATE["enabled"] = True
        RT["last_key"] = None
        return "Автоматику увімкнено"
    if cmd == "off":
        STATE["enabled"] = False
        RT["last_key"] = None
        return "Автоматику вимкнено, станція переходить у звичайний режим"
    if cmd == "charge":
        v = max(10, min(100, int(float(value))))
        STATE["force_target"] = v
        STATE["enabled"] = True
        RT["last_key"] = None
        return f"Ручна ціль {v}% до команди «скинути»"
    if cmd in ("auto", "reset"):
        STATE["force_target"] = None
        RT["last_key"] = None
        return "Ручну ціль скинуто, працює розклад"
    if cmd == "set":
        done = []
        for k, v in (value or {}).items():
            done.append(f"{k}={apply_setting(k, v)}")
        RT["last_key"] = None
        return "Збережено: " + ", ".join(done)
    if cmd in ("ping", "refresh"):
        RT["viewer_ts"] = now_ts()
        RT["push_now"] = True
        return "ok"
    if cmd == "status":
        return status_text()
    if cmd == "temp":
        t = RT["tele"].get("temp")
        lvl = {"ok": "норма", "warn": "висока", "crit": "критична", "low": "низька"}[STATE["temp_level"]]
        return (f"Температура батареї: {t:.1f} °C ({lvl})\n"
                f"Пороги: увага {CFG['temp_warn']} °C, критично {CFG['temp_crit']} °C, низька {CFG['temp_low']} °C"
                if t is not None else "Температура поки невідома")
    if cmd == "outages":
        bl = [b for b in outage_blocks() if b[1] > now_ts()]
        if not bl:
            return f"Для черги {CFG['queue']} відключень не заплановано"
        return f"Черга {CFG['queue']}:\n" + "\n".join(f"{fmt_dt(s)}-{fmt_t(e)}" for s, e in bl)
    return HELP


HELP = ("/status стан\n/temp температура\n/outages графік черги\n/on увімкнути автоматику\n"
        "/off вимкнути автоматику\n/charge 90 зарядити до 90%\n/auto скинути ручну ціль")


def status_text():
    t = RT["tele"]
    if not RT["online"]:
        return "Станція не відповідає"
    d = RT["decision"]
    act = {"charge": "заряд", "hold": "утримання", "off": "вимкнено", "outage": "відключення"}.get(d["action"], d["action"])
    lines = [f"Заряд: {t.get('soc', 0):.0f}%"]
    if t.get("temp") is not None:
        lines.append(f"Температура: {t['temp']:.1f} °C")
    if t.get("bat_w") is not None:
        lines.append(f"Батарея: {t['bat_w']:+.0f} Вт")
    if t.get("load_w") is not None:
        lines.append(f"Навантаження: {t['load_w']:.0f} Вт")
    tgt = f" до {d['target']}%" if d.get("target") is not None else ""
    lines.append(f"Режим: {act}{tgt}")
    lines.append(f"Причина: {d['reason']}")
    lines.append(f"Автоматика: {'увімкнена' if STATE['enabled'] else 'вимкнена'}")
    return "\n".join(lines)


def tg_poll(wait):
    if not CFG.get("tg_token"):
        time.sleep(wait)
        return
    try:
        r = tg_api("getUpdates", {"offset": STATE["tg_offset"], "timeout": wait}, timeout=wait + 10)
    except Exception as e:
        log(f"Telegram poll: {e}")
        time.sleep(min(wait, 5))
        return
    for upd in (r or {}).get("result", []):
        STATE["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CFG.get("tg_chat_id")):
            continue
        parts = (msg.get("text") or "").strip().split()
        if not parts:
            continue
        cmd = parts[0].split("@")[0]
        try:
            reply = handle_command(cmd, parts[1] if len(parts) > 1 else None)
        except Exception as e:
            reply = f"Помилка: {e}"
        RT["last_cmd"] = {"id": f"tg{upd['update_id']}", "text": reply, "ts": int(now_ts())}
        RT["push_now"] = True
        save_json(STATE_PATH, STATE)
        tg_send(reply)
        control_step()


# ---------------- GitHub Gist ----------------
class Gist:
    def __init__(self):
        self.etag = None
        self.disabled_until = 0

    def enabled(self):
        return bool(CFG.get("github_token")) and now_ts() >= self.disabled_until

    def req(self, method, path, body=None, etag=None):
        url = CFG["github_api"].rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {CFG['github_token']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "marstek-agent",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        rq = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=25) as r:
                return r.status, json.loads(r.read().decode() or "{}"), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, etag
            body = e.read().decode(errors="ignore")[:300]
            if e.code in (401, 403):
                self.disabled_until = now_ts() + 600
            raise RuntimeError(f"GitHub {e.code}: {body}")

    def ensure(self):
        if CFG.get("gist_id"):
            return CFG["gist_id"]
        _, gists, _ = self.req("GET", "/gists?per_page=100")
        for g in gists or []:
            if g.get("description") == GIST_DESC:
                gid = g["id"]
                break
        else:
            body = {"description": GIST_DESC, "public": False, "files": {
                "state.json": {"content": "{}"},
                "commands.json": {"content": json.dumps({"ping": 0, "cmds": []})},
            }}
            _, g, _ = self.req("POST", "/gists", body)
            gid = g["id"]
            log(f"Створено приватний gist {gid}")
        USER_CFG["gist_id"] = gid
        save_json(CFG_PATH, USER_CFG)
        rebuild_cfg()
        return gid

    def poll(self):
        gid = self.ensure()
        code, g, etag = self.req("GET", f"/gists/{gid}", etag=self.etag)
        if code == 304 or not g:
            return
        self.etag = etag
        f = (g.get("files") or {}).get("commands.json")
        if not f:
            return
        try:
            data = json.loads(f.get("content") or "{}")
        except ValueError:
            return
        ping = float(data.get("ping") or 0)
        if ping > RT["viewer_ts"] and now_ts() - ping < 600:
            if now_ts() - RT["viewer_ts"] > CFG["push_live_sec"]:
                RT["push_now"] = True
            RT["viewer_ts"] = ping
        for c in data.get("cmds") or []:
            cid = str(c.get("id"))
            if not cid or cid in STATE["acked"]:
                continue
            if now_ts() - float(c.get("ts") or 0) > 900:   # старі команди не виконуємо
                STATE["acked"] = (STATE["acked"] + [cid])[-60:]
                continue
            try:
                res = handle_command(c.get("cmd"), c.get("value"))
                ok = True
            except Exception as e:
                res, ok = f"Помилка: {e}", False
            STATE["acked"] = (STATE["acked"] + [cid])[-60:]
            RT["last_cmd"] = {"id": cid, "text": res, "ok": ok, "ts": int(now_ts())}
            RT["push_now"] = True
            log(f"Команда з дашборда: {c.get('cmd')} {c.get('value') or ''} -> {res}")
            if c.get("cmd") not in ("ping", "refresh"):
                tg_send(f"📱 З дашборда: {res}")
            control_step()
        save_json(STATE_PATH, STATE)

    def push(self):
        gid = self.ensure()
        body = {"files": {"state.json": {"content": json.dumps(build_public_state(), ensure_ascii=False,
                                                                separators=(",", ":"))}}}
        self.req("PATCH", f"/gists/{gid}", body)
        self.etag = None


def build_public_state():
    t = RT["tele"]
    o = RT["outage"]
    n = now_ts()
    blocks = [[int(s), int(e)] for s, e in outage_blocks() if e > n - 6 * 3600]
    settings = {k: CFG[k] for k in EDITABLE}
    return {
        "v": VERSION, "updated": int(n),
        "online": RT["online"], "last_seen": int(RT["last_seen"]),
        "soc": t.get("soc"), "temp": t.get("temp"), "temp_level": STATE["temp_level"],
        "bat_w": t.get("bat_w"), "load_w": t.get("load_w"), "grid_w": t.get("grid_w"),
        "mode": t.get("mode"), "rssi": t.get("rssi"), "device": t.get("device"), "fw": t.get("fw"),
        "enabled": STATE["enabled"], "force_target": STATE.get("force_target"),
        "decision": RT["decision"],
        "outage": ({"start": o["start"], "wh": round(o["wh"]), "soc_start": o["soc_start"]} if o else None),
        "outages": STATE["outages"][-20:],
        "blocks": blocks, "queue": CFG["queue"], "schedule_updated": schedule_updated(),
        "schedule_error": _sched.get("err"),
        "settings": settings,
        "capacity_wh": CFG["capacity_wh"], "min_soc": CFG["min_soc"],
        "eff_discharge": CFG["eff_discharge"],
        "profile": [round(x) for x in STATE["profile"]],
        "history": STATE["history"],
        "alerts": STATE["alerts"][-15:],
        "acked": STATE["acked"][-40:], "last_cmd": RT["last_cmd"],
    }


# ---------------- Цикл ----------------
def main():
    if not CFG.get("marstek_ip"):
        print("Спершу запусти: python agent.py setup")
        sys.exit(1)
    log(f"Агент v{VERSION} запущено, станція {CFG['marstek_ip']}, черга {CFG['queue']}, час {tz_line()}")
    tg_send(f"Агент Marstek v{VERSION} запущено\n{HELP}")
    gist = Gist()
    while True:
        try:
            n = now_ts()
            if n - RT["sample_ts"] >= CFG["sample_sec"]:
                RT["sample_ts"] = n
                sample_step()
            if n - RT["control_ts"] >= CFG["control_sec"]:
                RT["control_ts"] = n
                control_step()
            live = now_ts() - RT["viewer_ts"] < 240
            every = CFG["push_live_sec"] if live else CFG["push_idle_sec"]
            if gist.enabled():
                try:
                    if n - RT["poll_ts"] >= CFG["poll_sec"]:
                        RT["poll_ts"] = n
                        gist.poll()
                    if RT["push_now"] or now_ts() - RT["push_ts"] >= every:
                        gist.push()
                        RT["push_ts"] = now_ts()
                        RT["push_now"] = False
                except Exception as e:
                    log(f"GitHub: {e}")
                    RT["poll_ts"] = now_ts() + 30
                    RT["push_ts"] = now_ts() - every + 60
                    RT["push_now"] = False
            if now_ts() - RT["save_ts"] > 300:
                RT["save_ts"] = now_ts()
                save_json(STATE_PATH, STATE)
            tg_poll(8 if CFG.get("tg_token") else 3)
        except KeyboardInterrupt:
            save_json(STATE_PATH, STATE)
            break
        except Exception:
            log(traceback.format_exc())
            time.sleep(10)


# ---------------- Майстер налаштування і тест ----------------
def ask(prompt, default=""):
    s = input(f"{prompt}" + (f" [{default}]" if default != "" else "") + ": ").strip()
    return s or str(default)


def discover(port=None):
    """Шукає станції Marstek у мережі широкомовним запитом."""
    port = int(port or CFG.get("marstek_port") or 30000)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(3)
    found = []
    try:
        s.bind(("", port))
    except OSError:
        s.bind(("", 0))
    try:
        msg = json.dumps({"id": 1, "method": "Marstek.GetDevice", "params": {"ble_mac": "0"}}).encode()
        s.sendto(msg, ("255.255.255.255", port))
        end = time.time() + 3
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            try:
                r = json.loads(data.decode()).get("result", {})
            except ValueError:
                continue
            if r.get("device") and addr[0] not in [f["ip"] for f in found]:
                found.append({"ip": addr[0], "device": r.get("device"), "ver": r.get("ver"),
                              "mac": r.get("wifi_mac") or r.get("ble_mac")})
    except Exception as e:
        log(f"Пошук станції не вдався: {e}")
    finally:
        s.close()
    return found


def setup():
    cfg = dict(USER_CFG)
    print("\nНалаштування агента Marstek. Enter залишає значення в дужках.\n")
    print("1. Станція")
    found = discover()
    if found:
        for f in found:
            print(f"   знайдено {f['device']} (прошивка {f['ver']}) на {f['ip']}")
    else:
        print("   автоматично не знайдено, введи IP з додатку Marstek або роутера")
    cfg["marstek_ip"] = ask("   IP станції", found[0]["ip"] if found else cfg.get("marstek_ip", ""))
    if found and found[0].get("mac"):
        cfg["marstek_mac"] = found[0]["mac"]
    cfg["marstek_port"] = int(ask("   UDP порт Open API", cfg.get("marstek_port", 30000)))
    print("\n2. Графік відключень")
    cfg["queue"] = ask("   Твоя черга (наприклад 3.1)", cfg.get("queue", "3.1"))
    print("\n3. GitHub (для дашборда). Токен класичний, лише з правом gist.")
    cfg["github_token"] = ask("   Токен (порожньо, щоб пропустити)", cfg.get("github_token", ""))
    print("\n4. Telegram (сповіщення). Створи бота в @BotFather.")
    cfg["tg_token"] = ask("   Токен бота (порожньо, щоб пропустити)", cfg.get("tg_token", ""))
    if cfg["tg_token"]:
        input("   Надішли своєму боту будь-яке повідомлення в Telegram і натисни Enter...")
        try:
            url = f"https://api.telegram.org/bot{cfg['tg_token']}/getUpdates"
            with urllib.request.urlopen(url, timeout=20) as r:
                res = json.loads(r.read().decode()).get("result", [])
            ids = [u["message"]["chat"]["id"] for u in res if "message" in u]
            if ids:
                cfg["tg_chat_id"] = ids[-1]
                print(f"   chat_id = {ids[-1]}")
            else:
                print("   Повідомлення не знайдено, chat_id введи вручну.")
                cfg["tg_chat_id"] = int(ask("   chat_id", cfg.get("tg_chat_id", 0)))
        except Exception as e:
            print(f"   Не вдалося отримати chat_id: {e}")
    save_json(CFG_PATH, cfg)
    print(f"\nЗбережено в {CFG_PATH}. Перевірка: python agent.py --test\n")


def tz_line():
    off = -time.timezone if not time.localtime().tm_isdst else -time.altzone
    return f"{dt.datetime.now():%d.%m %H:%M} (UTC{off // 3600:+d})"


def selftest():
    ok = True
    print(f"Час на планшеті: {tz_line()}")
    if time.localtime().tm_gmtoff == 0:
        print("  УВАГА: часовий пояс UTC. Вистав у налаштуваннях Android пояс Київ, інакше графік зсунеться.")
    print(f"Станція {CFG.get('marstek_ip')}:{CFG.get('marstek_port')}")
    try:
        t = read_station()
        print(f"  OK: заряд {t.get('soc')}%, температура {t.get('temp')} °C, батарея {t.get('bat_w')} Вт, "
              f"навантаження {t.get('load_w')} Вт, режим {t.get('mode')}, {t.get('device')} fw {t.get('fw')}")
    except Exception as e:
        ok = False
        print(f"  ПОМИЛКА: {e}\n  Перевір IP, порт і що Open API увімкнено в додатку Marstek.")
    print(f"Графік черги {CFG['queue']}")
    bl = [b for b in outage_blocks() if b[1] > now_ts()]
    if _sched["err"]:
        ok = False
        print(f"  ПОМИЛКА: {_sched['err']}")
    else:
        print("  OK: " + (", ".join(f"{fmt_dt(s)}-{fmt_t(e)}" for s, e in bl) or "відключень не заплановано"))
    if CFG.get("github_token"):
        try:
            gid = Gist().ensure()
            print(f"GitHub\n  OK: gist {gid}")
        except Exception as e:
            ok = False
            print(f"GitHub\n  ПОМИЛКА: {e}")
    if CFG.get("tg_token"):
        try:
            tg_api("sendMessage", {"chat_id": CFG["tg_chat_id"], "text": "Тест агента Marstek: зв'язок є"})
            print("Telegram\n  OK: тестове повідомлення надіслано")
        except Exception as e:
            ok = False
            print(f"Telegram\n  ПОМИЛКА: {e}")
    print("\nУсе готово до запуску." if ok else "\nВиправ помилки вище.")


def probe(ip, port, local_port, method, params, timeout=8):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    try:
        try:
            s.bind(("", local_port))
            bound = s.getsockname()[1]
        except OSError as e:
            return f"не вдалося зайняти локальний порт {local_port}: {e}"
        s.sendto(json.dumps({"id": 7, "method": method, "params": params}).encode(), (ip, port))
        t0 = time.time()
        while True:
            data, addr = s.recvfrom(16384)
            if b'"method"' not in data:   # пропускаємо власне відлуння
                break
        return f"OK за {time.time() - t0:.1f} с від {addr[0]}:{addr[1]} (локальний порт {bound}): {data.decode(errors='ignore')[:2000]}"
    except socket.timeout:
        return "немає відповіді"
    except Exception as e:
        return f"помилка: {e}"
    finally:
        s.close()


def diag():
    ip, port = CFG.get("marstek_ip"), int(CFG.get("marstek_port", 30000))
    print(f"Станція в налаштуваннях: {ip}:{port}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((ip or "192.168.31.1", 9))
        print(f"IP планшета: {s.getsockname()[0]}")
        s.close()
    except Exception as e:
        print(f"IP планшета: невідомо ({e})")
    print("\n1. Пошук станцій широкомовним запитом...")
    found = discover(port)
    for f in found:
        print(f"   знайдено {f['device']} fw {f['ver']} на {f['ip']} mac {f['mac']}")
    if not found:
        print("   нічого не знайдено")
    n = 2
    for p in sorted({port, 30000}):
        for lp in (30000, 0):
            print(f"\n{n}. Marstek.GetDevice на {ip}:{p}, локальний порт {lp or 'випадковий'}")
            print("   " + probe(ip, p, lp, "Marstek.GetDevice", {"ble_mac": "0"}))
            n += 1
    print(f"\n{n}. ES.GetStatus на {ip}:{port}")
    print("   " + probe(ip, port, 30000, "ES.GetStatus", {"id": 0}))
    print(f"\n{n + 1}. Bat.GetStatus на {ip}:{port}")
    print("   " + probe(ip, port, 30000, "Bat.GetStatus", {"id": 0}))
    print("\nСкинь скрін цього результату.")


def dump():
    """Сирі дані станції: всі поля, які вона віддає. Для налагодження."""
    ip, port = CFG.get("marstek_ip"), int(CFG.get("marstek_port", 30000))
    print(f"{dt.datetime.now():%d.%m %H:%M:%S}  станція {ip}:{port}")
    calls = [("ES.GetStatus", {"id": 0}), ("Bat.GetStatus", {"id": 0}), ("ES.GetMode", {"id": 0}),
             ("EM.GetStatus", {"id": 0})]
    for method, params in calls:
        res = None
        for lp in (0, 30000):
            raw = probe(ip, port, lp, method, params, timeout=6)
            if raw.startswith("OK"):
                try:
                    res = json.loads(raw[raw.index("{"):]).get("result")
                except ValueError:
                    res = raw
                break
            time.sleep(0.5)
        print(f"\n{method}:")
        if isinstance(res, dict):
            print("  " + "  ".join(f"{k}={v}" for k, v in res.items() if k != "id"))
        else:
            print(f"  {res or 'немає відповіді'}")
        time.sleep(0.5)


if __name__ == "__main__":
    # Клавіатура Android часто міняє "--" на тире, тому приймаємо setup, -setup, --setup, —setup
    ARGS = [a.lstrip("-\u2014\u2013\u2012").lower() for a in sys.argv[1:]]
    if "setup" in ARGS:
        setup()
    elif "test" in ARGS:
        selftest()
    elif "diag" in ARGS:
        diag()
    elif "dump" in ARGS:
        dump()
    else:
        main()
