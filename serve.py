#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛牛酱投喂面板 · 本地小服务。

双击启动器（Windows 快捷方式 / Kubuntu 桌面 .desktop）→ 起服务并打开浏览器面板（已在运行则直接开面板）。
定时巡检：Windows 走计划任务；Kubuntu 走 systemd 用户定时器 niuniu-watch.timer（install.sh 安装）。
端点：
  GET /              面板页面
  GET /api/quick     实时快检（配置模型，chat+responses 双线，30s 超时），缓存 5s
  GET /api/sweep     跑一遍全站巡检（watch.py），返回最新结果
  GET /api/open?f=   用系统默认程序打开 status.md / log.jsonl
  GET /api/quit      退出服务
闲置 30 分钟自动退出。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    ROOT = Path(sys.executable).resolve().parent           # 数据目录＝exe 所在目录（可写）
    ASSETS = Path(getattr(sys, "_MEIPASS", str(ROOT)))     # 打包资源目录
    sys.path.insert(0, str(ASSETS))
else:
    ASSETS = ROOT
    sys.path.insert(0, str(ROOT))
import watch  # noqa: E402

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
CF = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW：Windows 子进程不弹窗口；其它平台取 0


def asset(name):
    """优先取 exe 旁的同名文件（可热改），否则用打包内嵌资源。"""
    local = ROOT / name
    return local if local.exists() else ASSETS / name


def open_path(p):
    """用系统默认程序打开文件（Windows / macOS / Linux）。"""
    p = str(p)
    if IS_WIN:
        os.startfile(p)  # Windows 专用
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])


def run_watch():
    if FROZEN:
        subprocess.run([sys.executable, "--watch"], cwd=str(ROOT), timeout=600,
                       creationflags=CF)
    else:
        subprocess.run([sys.executable, str(ROOT / "watch.py")], cwd=str(ROOT), timeout=600)

PORT = 8791
URL = f"http://127.0.0.1:{PORT}/"
IDLE_EXIT_SEC = 30 * 60

_cache = {"ts": 0.0, "data": None}
_hit = {"t": time.time()}
_lock = threading.Lock()
_roster = {"t": 0.0, "ids": []}
_last = {}  # id -> (ts, rec)  上一轮探测结果，供慢模型快速窗口回退
_confirmed = {}  # id -> 已确认的稳定状态（连续 2 次同向才切换，滤掉单次抖动）
_pending = {}    # id -> [候选状态, 连续次数]


def confirmed_state(mid, raw):
    """2 次确认制：单次探测结果与已确认状态不同时先挂起，连续 2 次一致才切换。"""
    conf = _confirmed.get(mid)
    if raw == "checking":
        return conf if conf else "checking"
    if conf is None or raw == conf:
        _confirmed[mid] = raw
        _pending.pop(mid, None)
        return raw
    pend = _pending.get(mid)
    if pend and pend[0] == raw:
        pend[1] += 1
    else:
        _pending[mid] = [raw, 1]
    if _pending[mid][1] >= 2:
        _confirmed[mid] = raw
        _pending.pop(mid, None)
        return raw
    return conf


def _finish_probes(ex, pending):
    for fut, m in pending:
        try:
            _m, rec = fut.result()
            _last[m] = (time.time(), rec)
        except Exception:  # noqa: BLE001
            pass
    ex.shutdown(wait=False)


def probe_all(ids, fast_sec=6.0, timeout=30):
    """并发探测；快速窗口内返回的用新结果，其余回退到 ≤90s 旧结果（stale），后台补完。"""
    done, pending = {}, []
    ex = ThreadPoolExecutor(max_workers=max(4, min(24, len(ids))))
    for m in ids:
        pending.append((ex.submit(watch.probe, m, timeout), m))
    deadline = time.time() + fast_sec
    while pending:
        remain = deadline - time.time()
        if remain <= 0:
            break
        time.sleep(min(0.06, remain))
        still = []
        for fut, m in pending:
            if fut.done():
                try:
                    _m, rec = fut.result()
                    done[m] = rec
                except Exception:  # noqa: BLE001
                    pass
            else:
                still.append((fut, m))
        pending = still
    if pending:
        threading.Thread(target=_finish_probes, args=(ex, pending), daemon=True).start()
    out, now = {}, time.time()
    for m in ids:
        if m in done:
            rec = dict(done[m])
            rec.pop("stale", None)
            _last[m] = (now, rec)
        else:
            prev = _last.get(m)
            if prev and now - prev[0] < 90:
                rec = dict(prev[1])
                rec["stale"] = True
            else:
                rec = {"state": "checking", "chat": None, "resp": None, "ms": 0, "err": "", "stale": True}
                _last[m] = (now, rec)
        out[m] = rec
    return out


def site_models():
    """站点 /v1/models 全量名单（缓存 5 分钟）。"""
    if _roster["ids"] and time.time() - _roster["t"] < 300:
        return _roster["ids"]
    try:
        s, b, _ = watch.http("/models", None, timeout=30)
        if s == 200:
            ids = sorted(m["id"] for m in json.loads(b)["data"])
            if ids:
                _roster["ids"] = ids
                _roster["t"] = time.time()
    except Exception:  # noqa: BLE001
        pass
    return _roster["ids"]


def err_kind(err: str) -> str:
    e = err.lower()
    if "429" in e:
        return "限流中"
    if "forbidden" in e:
        return "上游拒绝"
    if "502" in e:
        return "上游故障"
    if "503" in e:
        return "维护中"
    if "timed out" in e or "timeout" in e:
        return "超时"
    if "connect" in e or "refused" in e or "resolve" in e:
        return "连不上"
    return "不可用"


def today_digest():
    today = datetime.now().astimezone().date()
    runs = feeds = 0
    ever, last = set(), ""
    try:
        for ln in (ROOT / "log.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(ln)
                if "models" not in rec:
                    continue
                ts = datetime.fromisoformat(rec["ts"]).astimezone()
            except Exception:  # noqa: BLE001
                continue
            if ts.date() != today:
                continue
            runs += 1
            ups = [m for m, r in rec["models"].items() if r.get("state") == "up"]
            if ups:
                feeds += 1
                ever.update(ups)
                last = ts.strftime("%H:%M")
    except FileNotFoundError:
        pass
    return {"runs": runs, "feeds": feeds, "ever": sorted(ever), "last": last}


def sweep_state():
    try:
        s = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
        return {
            "updated": s.get("updated", ""),
            "summary": s.get("summary", ""),
            "models": s.get("models", {}),
            "ids": s.get("ids", []),
        }
    except Exception:  # noqa: BLE001
        return {"updated": "", "summary": "", "models": {}, "ids": []}


def changes_tail(n=6):
    try:
        return (ROOT / "changes.log").read_text(encoding="utf-8").splitlines()[-n:]
    except Exception:  # noqa: BLE001
        return []


def history_24h():
    """最近 24 小时、每小时一个格的可用性历史（从 log.jsonl 聚合）。"""
    now = datetime.now().astimezone()
    base = now.replace(minute=0, second=0, microsecond=0)
    slots = [base - timedelta(hours=i) for i in range(23, -1, -1)]
    idx = {s.strftime("%m%d%H"): i for i, s in enumerate(slots)}
    per, runs = {}, [0] * 24
    acc = {}
    up_runs = total = 0
    try:
        lines = (ROOT / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for ln in lines:
        try:
            rec = json.loads(ln)
            if "models" not in rec:
                continue
            ts = datetime.fromisoformat(rec["ts"]).astimezone()
        except Exception:  # noqa: BLE001
            continue
        i = idx.get(ts.strftime("%m%d%H"))
        if i is None:
            continue
        runs[i] += 1
        total += 1
        anyup = False
        for m, r in rec["models"].items():
            arr = per.setdefault(m, [None] * 24)
            a = acc.setdefault(m, {"up": 0, "chat": 0, "runs": 0, "ms": [], "last": None})
            a["runs"] += 1
            st = r.get("state")
            a["last"] = st
            if st == "up":
                arr[i] = 1
                anyup = True
                a["up"] += 1
            else:
                if arr[i] is None:
                    arr[i] = 0
                if st == "chat-only":
                    a["chat"] += 1
            if r.get("ms"):
                a["ms"].append(r["ms"])
        if anyup:
            up_runs += 1
    stats = {}
    for m, a in acc.items():
        ms = a["ms"]
        stats[m] = {
            "up": a["up"], "chat": a["chat"], "runs": a["runs"],
            "avg_ms": round(sum(ms) / len(ms)) if ms else None,
            "last": a["last"],
        }
    return {
        "slots": [s.strftime("%H:00") for s in slots],
        "models": per,
        "runs": runs,
        "up_runs": up_runs,
        "total_runs": total,
        "stats": stats,
    }


def model_history(model, limit=60):
    rows = []
    try:
        lines = (ROOT / "log.jsonl").read_text(encoding="utf-8").splitlines()[-500:]
    except FileNotFoundError:
        lines = []
    for ln in lines:
        try:
            rec = json.loads(ln)
            r = (rec.get("models") or {}).get(model)
            if r is None:
                continue
            rows.append({
                "ts": rec["ts"][11:16] if len(rec.get("ts", "")) >= 16 else rec.get("ts", ""),
                "full": rec.get("ts", ""),
                "state": r.get("state"), "ms": r.get("ms") or 0, "err": (r.get("err") or "")[:90],
            })
        except Exception:  # noqa: BLE001
            continue
    return rows[-limit:]


def detail(model):
    import re
    if not re.match(r"^[\w.\-]+$", model or ""):
        return {"error": "bad model id"}
    _m, live = watch.probe(model, timeout=30)
    live["errKind"] = "" if live["state"] == "up" else err_kind(live["err"])
    tools = {"ok": None, "err": ""}
    if live["state"] != "down":
        s, b, _ = watch.http("/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "Call the get_weather tool for Paris now."}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "Get weather of a city",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}}],
            "tool_choice": "auto", "max_completion_tokens": 256, "stream": False}, timeout=30)
        if s == 200:
            try:
                msg = json.loads(b)["choices"][0]["message"]
                tc = msg.get("tool_calls") or []
                tools = {"ok": bool(tc),
                         "err": "" if tc else "未发起工具调用（仅文本回复）"}
            except Exception as e:  # noqa: BLE001
                tools = {"ok": False, "err": f"解析失败: {e}"}
        else:
            tools = {"ok": False, "err": watch.short_err(s, b)}
    return {"id": model, "live": live, "tools": tools, "history": model_history(model)}


TRY_OVERLAY = ROOT / "_try-retry.yml"
TRY_DIR = ROOT / "_try"


def try_model(model):
    """真实拉起一次 omp -p 会话（niuniu/<model>），返回完整结果。"""
    import re
    if not re.match(r"^[\w.\-]+$", model or ""):
        return {"ok": False, "error": "bad model id"}
    exe = shutil.which("omp")
    if not exe:
        return {"ok": False, "error": "PATH 里找不到 omp"}
    TRY_DIR.mkdir(exist_ok=True)
    if not TRY_OVERLAY.exists():
        TRY_OVERLAY.write_text("retry:\n  maxRetries: 2\n  baseDelayMs: 400\n", encoding="utf-8")
    cmd = [exe, "-p", "--cwd", str(TRY_DIR), "--no-session", "--no-title",
           "--no-skills", "--no-extensions", "--no-lsp", "--auto-approve",
           "--config", str(TRY_OVERLAY), "--thinking", "low",
           "--model", f"niuniu/{model}", "回答这一个词即可：PONG"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, creationflags=CF)
        out = (p.stdout or "").strip()
        return {
            "ok": p.returncode == 0 and bool(out),
            "sec": round(time.time() - t0, 1), "rc": p.returncode,
            "pong": "PONG" in out.upper(),
            "out": out[-900:], "err": (p.stderr or "").strip()[-400:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "sec": round(time.time() - t0, 1), "error": "实测超时（180s）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def watcher_cfg():
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_watcher_cfg(cfg):
    (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def task_status():
    """定时巡检状态：Kubuntu 读 systemd 用户定时器；Windows 读计划任务。"""
    if IS_LINUX:
        return _linux_task_status()
    if not IS_WIN:
        return {"error": "不支持的平台: " + sys.platform}
    return _win_task_status()


def _win_task_status():
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$t=Get-ScheduledTask -TaskName 'niuniu-watch' -ErrorAction Stop;"
        "$i=Get-ScheduledTaskInfo -TaskName 'niuniu-watch';"
        "[pscustomobject]@{State=[string]$t.State;Next=[string]$i.NextRunTime;"
        "Last=[string]$i.LastRunTime;Result=$i.LastTaskResult} | ConvertTo-Json -Compress"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30,
                           creationflags=CF)
        if p.returncode != 0:
            return {"error": ((p.stderr or p.stdout or "").strip()[:160] or "读取计划任务失败")}
        return json.loads(p.stdout.strip() or "{}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _ps(cmd, timeout=60):
    return subprocess.run(["powershell", "-NoProfile", "-Command",
                           "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + cmd],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=CF)


def _pyw():
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


# ---------- Kubuntu：systemd 用户定时器 ----------

UNIT_SERVICE = "niuniu-watch.service"
UNIT_TIMER = "niuniu-watch.timer"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
CAL = {15: "*:0/15", 30: "*:0/30", 60: "hourly", 120: "*-*-* 00/2:00:00",
       180: "*-*-* 00/3:00:00", 360: "*-*-* 00/6:00:00", 720: "*-*-* 00/12:00"}


def _systemctl(*params, timeout=60):
    """systemctl --user；systemctl 缺失/超时也返回 CompletedProcess，不抛异常。"""
    args = ["systemctl", "--user", "--no-pager", *params]
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"})  # LC_ALL=C：时间戳格式稳定可解析
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "systemctl 不存在")
    except Exception as e:  # noqa: BLE001
        return subprocess.CompletedProcess(args, 1, "", f"{type(e).__name__}: {e}")


def _sd_quote(s):
    """systemd 单元文件参数转义（'%' 需写成 '%%'；含空格路径加引号）。"""
    return '"' + str(s).replace("%", "%%").replace('"', '\\"') + '"'


def _write_units(minutes):
    """写入 niuniu-watch.{service,timer}：整点对齐、错过的巡检开机后补跑。"""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / UNIT_SERVICE).write_text(
        "[Unit]\n"
        "Description=niuniu-watch sweep (watch + publish)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={_sd_quote(ROOT)}\n"
        f"ExecStart={_sd_quote(sys.executable)} {_sd_quote(ROOT / 'serve.py')} --hourly\n",
        encoding="utf-8")
    (UNIT_DIR / UNIT_TIMER).write_text(
        "[Unit]\n"
        "Description=niuniu-watch schedule\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={CAL[minutes]}\n"
        "Persistent=true\n"
        f"Unit={UNIT_SERVICE}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n",
        encoding="utf-8")


def _sd_ts(v):
    """'Wed 2026-10-14 15:00:00 CST' → '2026/10/14 15:00:00'（无值 → ''）。"""
    p = (v or "").split()
    return f"{p[1]} {p[2]}".replace("-", "/") if len(p) >= 3 else ""


def _linux_task_status():
    if not (UNIT_DIR / UNIT_TIMER).exists():
        return {"State": "Disabled", "Next": "", "Last": "", "Result": None}
    props = {}
    for unit in (UNIT_TIMER, UNIT_SERVICE):
        out = _systemctl("show", unit, "-p", "ActiveState",
                         "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
                         "-p", "ExecMainStatus").stdout or ""
        for ln in out.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                props[f"{unit}.{k}"] = v
    state = "Disabled"
    if props.get(f"{UNIT_TIMER}.ActiveState") == "active":
        state = "Ready"
    if props.get(f"{UNIT_SERVICE}.ActiveState") in ("activating", "active"):
        state = "Running"
    res = (props.get(f"{UNIT_SERVICE}.ExecMainStatus") or "").strip()
    return {"State": state,
            "Next": _sd_ts(props.get(f"{UNIT_TIMER}.NextElapseUSecRealtime")),
            "Last": _sd_ts(props.get(f"{UNIT_TIMER}.LastTriggerUSec")),
            "Result": int(res) if res.lstrip("-").isdigit() else None}


def _linux_task_set(action, arg=""):
    if action == "run":
        r = _systemctl("start", UNIT_SERVICE)
    elif action == "disable":
        r = _systemctl("disable", "--now", UNIT_TIMER)
    elif action == "enable":
        if not (UNIT_DIR / UNIT_TIMER).exists():  # 首次：没装过就按配置间隔装好再启用
            try:
                iv = int(watcher_cfg().get("interval") or 60)
                _write_units(iv if iv in CAL else 60)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        _systemctl("daemon-reload")
        r = _systemctl("enable", "--now", UNIT_TIMER)
    elif action == "interval":
        try:
            minutes = int(arg or 60)
        except ValueError:
            return {"ok": False, "error": "bad interval"}
        if minutes not in CAL:
            return {"ok": False, "error": "支持 15/30/60/120/180/360/720 分钟"}
        try:
            _write_units(minutes)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        _systemctl("daemon-reload")
        _systemctl("enable", UNIT_TIMER)
        r = _systemctl("restart", UNIT_TIMER)  # 让新 OnCalendar 立即生效
        if r.returncode == 0:
            cfg = watcher_cfg()
            cfg["interval"] = minutes
            save_watcher_cfg(cfg)
    else:
        return {"ok": False, "error": "unknown action"}
    return {"ok": r.returncode == 0, "code": r.returncode,
            "out": (r.stdout or "").strip()[-160:], "err": (r.stderr or "").strip()[-160:]}


def task_set(action, arg=""):
    """管理定时巡检：Kubuntu 走 systemd 用户定时器；Windows 走计划任务。"""
    if IS_LINUX:
        return _linux_task_set(action, arg)
    if not IS_WIN:
        return {"ok": False, "error": "不支持的平台: " + sys.platform}
    return _win_task_set(action, arg)


def _win_task_set(action, arg=""):
    cfg = watcher_cfg()
    if action == "disable":
        r = _ps("Disable-ScheduledTask -TaskName 'niuniu-watch' | Out-Null; 'ok'")
    elif action == "enable":
        r = _ps("Enable-ScheduledTask -TaskName 'niuniu-watch' | Out-Null; 'ok'")
    elif action == "run":
        r = _ps("Start-ScheduledTask -TaskName 'niuniu-watch'; 'ok'")
    elif action == "interval":
        try:
            minutes = int(arg or 60)
        except ValueError:
            return {"ok": False, "error": "bad interval"}
        if minutes not in (15, 30, 60, 120, 180, 360, 720):
            return {"ok": False, "error": "支持 15/30/60/120/180/360/720 分钟"}
        step = timedelta(minutes=minutes)
        base = datetime.now().replace(second=0, microsecond=0)
        day_start = base.replace(hour=0, minute=0)
        at = day_start + ((base - day_start) // step + 1) * step
        if FROZEN:
            exe, arg = str(Path(sys.executable).resolve()), "--hourly"
        else:
            # 与 Linux / exe 一致：巡检 + 发布快照（仅 watch.py 会导致 Pages 静默停更）
            exe, arg = _pyw(), f'"{ROOT / "serve.py"}" --hourly'
        script = (
            "$ErrorActionPreference='Stop';"
            f"$a=New-ScheduledTaskAction -Execute '{exe}' -Argument '{arg}'"
            f" -WorkingDirectory '{ROOT}';"
            f"$t=New-ScheduledTaskTrigger -Once -At ([datetime]'{at:%Y-%m-%d %H:%M:00}')"
            f" -RepetitionInterval (New-TimeSpan -Minutes {minutes});"
            "Register-ScheduledTask -TaskName 'niuniu-watch' -Action $a -Trigger $t -Force | Out-Null;'ok'"
        )
        r = _ps(script)
        if r.returncode == 0:
            cfg["interval"] = minutes
            save_watcher_cfg(cfg)
    else:
        return {"ok": False, "error": "unknown action"}
    return {"ok": r.returncode == 0, "code": r.returncode,
            "out": (r.stdout or "").strip()[-160:], "err": (r.stderr or "").strip()[-160:]}



def apply_snapshot_sweep(data):
    """快照发布：把仍为 checking 的 live 行用 state.json（sweep）补全。

    --hourly 先跑 watch 再 publish；publish 进程内 probe_all 可能未跑完，
    若不合并，Pages 会出现 up:0 / 大量 checking，而 sweep 里其实已有可用模型。
    只填补 checking，不覆盖已完成的 live 探测结果。
    """
    if not data or "error" in data:
        return data
    sw = data.get("sweep") or {}
    sw_models = sw.get("models") or {}
    if not sw_models:
        sw_models = sweep_state().get("models") or {}
    if not sw_models:
        return data
    models = data.get("models") or []
    changed = False
    for m in models:
        if m.get("state") != "checking":
            continue
        sst = sw_models.get(m["id"])
        if sst not in ("up", "chat-only", "down"):
            continue
        m["state"] = sst
        m["stale"] = False
        if sst == "up":
            m["err"] = ""
            m["errKind"] = ""
        elif sst == "chat-only":
            m["err"] = m.get("err") or "responses failed"
            m["errKind"] = m.get("errKind") or ""
        changed = True
    if not changed:
        return data
    up = [m["id"] for m in models if m.get("state") == "up"]
    data["up"] = up
    tot = (data.get("counts") or {}).get("total", len(models))
    data["counts"] = {"up": len(up), "total": tot}
    rank = {"up": 0, "chat-only": 1, "checking": 2, "down": 3}
    models.sort(key=lambda x: (rank.get(x["state"], 4), 0 if x.get("configured") else 1, x["id"]))
    data["models"] = models
    return data


def payload(force=False, deep=False, wait=None):
    if not watch.KEY:
        return {"error": "缺少 api_key：请在 config.json 里填入牛牛酱的密钥（参考 config.example.json）"}
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < 5:
        return _cache["data"]
    with _lock:  # single-flight：并发请求共享同一批探测
        now = time.time()
        if not force and _cache["data"] and now - _cache["ts"] < 5:
            return _cache["data"]
        cfg = set(watch.configured_ids())
        ids = site_models() or sorted(cfg) or watch.FALLBACK_IDS
        live = probe_all(ids, fast_sec=wait if wait else (15.0 if deep else 6.0))
        models = []
        up = []
        for m in ids:
            r = live[m]
            st = confirmed_state(m, r["state"])
            if st == "up":
                up.append(m)
            models.append({
                "id": m, "state": st, "ms": r["ms"] if st == r["state"] else 0,
                "err": r["err"], "errKind": "" if st in ("up", "checking") else err_kind(r["err"]),
                "configured": m in cfg, "stale": bool(r.get("stale")) or st != r["state"],
            })
        rank = {"up": 0, "chat-only": 1, "checking": 2, "down": 3}
        models.sort(key=lambda x: (rank.get(x["state"], 4), 0 if x["configured"] else 1, x["id"]))
        data = {
            "ts": datetime.now().astimezone().strftime("%H:%M:%S"),
            "counts": {"up": len(up), "total": len(ids)},
            "up": up,
            "models": models,
            "today": today_digest(),
            "sweep": sweep_state(),
            "changes": changes_tail(),
            "history": history_24h(),
            "port": PORT,
        }
        _cache.update(ts=now, data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "niuniu-dash/1.0"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        _hit["t"] = time.time()
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._send(200, asset("dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/quick":
                deep = parse_qs(u.query).get("deep", ["0"])[0] == "1"
                return self._json(payload(deep=deep))
            if u.path == "/api/detail":
                m = parse_qs(u.query).get("m", [""])[0]
                return self._json(detail(m))
            if u.path == "/api/try":
                m = parse_qs(u.query).get("m", [""])[0]
                return self._json(try_model(m))
            if u.path == "/api/task":
                cfg = watcher_cfg()
                return self._json({"status": task_status(), "interval": cfg.get("interval", 60),
                                   "notify": bool(cfg.get("notify", True))})
            if u.path == "/api/task/set":
                q = parse_qs(u.query)
                return self._json(task_set(q.get("action", [""])[0], q.get("arg", [""])[0]))
            if u.path == "/api/watcher/set":
                q = parse_qs(u.query)
                cfg = watcher_cfg()
                cfg["notify"] = q.get("notify", ["1"])[0] == "1"
                save_watcher_cfg(cfg)
                return self._json({"ok": True, "notify": cfg["notify"]})
            if u.path == "/api/alert":
                q = parse_qs(u.query)
                rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                       "m": q.get("m", [""])[0], "from": q.get("from", [""])[0], "to": q.get("to", [""])[0]}
                try:
                    with (ROOT / "alerts.log").open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except Exception:  # noqa: BLE001
                    pass
                return self._json({"ok": True})
            if u.path == "/api/sweep":
                run_watch()
                return self._json(payload(force=True))
            if u.path == "/api/open":
                f = parse_qs(u.query).get("f", ["status"])[0]
                target = {"status": ROOT / "status.md", "log": ROOT / "log.jsonl",
                          "changes": ROOT / "changes.log"}.get(f)
                if target and target.exists():
                    open_path(target)
                return self._json({"ok": True})
            if u.path == "/api/quit":
                self._json({"ok": True})
                threading.Timer(0.4, self.server.shutdown).start()
                return
            return self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def already_running():
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def idle_watch(server):
    while True:
        time.sleep(30)
        if time.time() - _hit["t"] > IDLE_EXIT_SEC:
            server.shutdown()
            return


def main():
    if "--publish" in sys.argv:  # 生成静态快照并推送 GitHub Pages
        try:
            import publish
            return publish.main()
        except Exception:  # noqa: BLE001
            return 1
    if "--hourly" in sys.argv:  # 定时任务（Windows 计划任务 / systemd）：巡检 + 发布快照
        try:
            watch.main()
        except Exception:  # noqa: BLE001
            pass
        try:
            import publish
            publish.main()
        except Exception:  # noqa: BLE001
            pass
        return 0
    if "--watch" in sys.argv:  # 单次巡检模式（Windows 计划任务 / 面板「立即巡检」）
        try:
            return watch.main()
        except Exception:  # noqa: BLE001
            return 1
    quiet = "--no-open" in sys.argv
    if already_running():
        if not quiet:
            webbrowser.open(URL)
        return 0
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=idle_watch, args=(httpd,), daemon=True).start()
    if not quiet:
        threading.Timer(0.6, lambda: webbrowser.open(URL)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
