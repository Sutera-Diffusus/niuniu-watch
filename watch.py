#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛牛酱公益站模型可用性监控（每小时由定时任务调用：Windows 计划任务 / systemd 用户定时器）。

- 探测 free.quanthatch.com 上的全部模型：chat/completions 全量，通过者再验 responses 线。
- 写 log.jsonl（每次运行追加）、state.json（供 omp hook 读取）、status.md（人读）、
  changes.log（变化流水）；状态有变化时弹一条桌面通知（Windows 气泡 / Linux notify-send）。
  若配置了 webhook_url，仅在「没投喂 → 开饭」这一沿上 POST 一次。
- 只用标准库，异常绝不外抛（定时任务里静默运行）。
"""
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

BASE = "https://free.quanthatch.com/v1"
ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):  # 打包后：数据写到 exe 同目录，而不是临时解包目录
    ROOT = Path(sys.executable).resolve().parent


def _load_key():
    """密钥优先级：环境变量 NIUNIU_API_KEY → config.json（不进代码仓库）。"""
    k = os.environ.get("NIUNIU_API_KEY", "").strip()
    if k:
        return k
    try:
        c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return (c.get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


KEY = _load_key()
LOG = ROOT / "log.jsonl"
STATE = ROOT / "state.json"
STATUS = ROOT / "status.md"
REPORT = ROOT / "report.md"
CHANGES = ROOT / "changes.log"
MODELS_YML = Path.home() / ".omp" / "agent" / "models.yml"
NOTIFY = True  # 状态有变化时弹桌面通知

UA = "niuniu-watch/1.0"
CTX = ssl.create_default_context()

FALLBACK_IDS = [
    "codex-auto-review", "deepseek-v4-flash", "glm-5.3", "glm-5.3-flash", "gpt-5.5",
    "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6", "gpt-6-astra",
    "gpt-6-sol", "hy4-preview", "k3", "step-3.5-flash", "step-3.5-flash-2603",
    "step-3.7-flash", "step-5-preview", "step-image-edit-2", "step-router-v1",
    "stepaudio-2.5-asr", "stepaudio-2.5-chat", "stepaudio-2.5-realtime", "stepaudio-2.5-tts",
]


def http(path, payload=None, timeout=90):
    """→ (status|None, body, seconds)"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": UA},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", time.time() - t0


def short_err(status, body):
    if status is None:
        return body[:60]
    try:
        msg = json.loads(body).get("error", {}).get("message", body)
    except Exception:  # noqa: BLE001
        msg = body
    return f"{status} {str(msg)[:60]}"


def probe(model, timeout=90):
    status, body, dt = http("/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_completion_tokens": 32, "stream": False,
    }, timeout=timeout)
    chat_ok = status == 200
    resp_ok = None
    if chat_ok:
        s2, _b2, _d2 = http("/responses", {
            "model": model, "input": "Say OK", "max_output_tokens": 64, "store": False,
        }, timeout=timeout)
        resp_ok = s2 == 200
    if chat_ok and resp_ok:
        state = "up"
    elif chat_ok:
        state = "chat-only"
    else:
        state = "down"
    return model, {
        "state": state, "chat": status, "resp": (200 if resp_ok else (s2 if chat_ok else None)),
        "ms": round(dt * 1000),
        "err": "" if state == "up" else (short_err(status, body) if not chat_ok else "responses failed"),
    }


def configured_ids():
    """从 models.yml 的 niuniu 段解析 models 列表里的 id。"""
    try:
        lines = MODELS_YML.read_text(encoding="utf-8").splitlines()
        ids, inside = [], False
        for ln in lines:
            if re.match(r"^  niuniu:\s*$", ln):
                inside = True
                continue
            if inside:
                if re.match(r"^  \S", ln):  # 下一个 provider
                    break
                m = re.match(r"^\s*- id:\s*(\S+)", ln)
                if m:
                    ids.append(m.group(1))
        return ids
    except Exception:  # noqa: BLE001
        return []


def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def toast(title, text):
    """桌面通知：Windows 托盘气泡；Linux 用 notify-send（KDE），退回 kdialog。"""
    if not NOTIFY:
        return
    try:
        if sys.platform == "win32":
            ps = (
                "$ErrorActionPreference='Stop';"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(15000,'{title}','{text}',[System.Windows.Forms.ToolTipIcon]::Info);"
                "Start-Sleep -Seconds 16;$n.Dispose()"
            )
            tmp = ROOT / "_toast.ps1"
            tmp.write_text("\ufeff" + ps, encoding="utf-8")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(tmp)],
                creationflags=0x08000000,
            )
            return
        ns = shutil.which("notify-send")
        if ns:
            subprocess.Popen([ns, "-a", "niuniu-watch", "-t", "15000", title, text])
            return
        kd = shutil.which("kdialog")
        if kd:
            subprocess.Popen([kd, "--title", title, "--passivepopup", text, "15"])
    except Exception:  # noqa: BLE001
        pass


def load_cfg():
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def feed_opened(old, up_ids):
    """开饭沿：上一轮有基线且没有任何 up，这一轮至少有一个 up。首次建基线不算。"""
    if not old:
        return False
    if any(v == "up" for v in old.values()):
        return False
    return bool(up_ids)


def webhook_body(up_ids, now):
    """开饭通知正文。menu 为当前可用模型；text 是短中文。"""
    menu = [str(m) for m in (up_ids or [])]
    text = "开饭啦" + (("：" + "、".join(menu)) if menu else "")
    return {
        "status": "up",
        "menu": menu,
        "ts": now.isoformat(timespec="seconds"),
        "text": text,
    }


def post_webhook(url, payload):
    """POST JSON 到 webhook_url。失败只在 stderr 记一行，绝不抛出（不打断巡检 / 面板）。"""
    url = str(url or "").strip()
    if not url:
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        try:
            print("webhook 跳过: 地址需以 http:// 或 https:// 开头", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
            r.read(512)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"webhook 失败: {type(e).__name__}: {e}"[:180], file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def main():
    global NOTIFY
    cfg = load_cfg()
    NOTIFY = bool(cfg.get("notify", NOTIFY))
    webhook_url = str(cfg.get("webhook_url") or "").strip()
    now = datetime.now().astimezone()
    prev = load_json(STATE) or {}

    status, body, _ = http("/models", None, timeout=30)
    ids = []
    if status == 200:
        try:
            ids = sorted(m["id"] for m in json.loads(body)["data"])
        except Exception:  # noqa: BLE001
            ids = []
    if not ids:
        ids = prev.get("ids") or FALLBACK_IDS

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = dict(ex.map(probe, ids))

    cur = {m: r["state"] for m, r in results.items()}
    old = prev.get("models") or {}
    changed = bool(old) and cur != old
    first = not old

    if first:
        revision = 1
        report = "首次运行，基线已建立。"
        toast_title, toast_text = "牛牛站监控", "监控已启动，基线已建立"
    elif changed:
        revision = (prev.get("revision") or 1) + 1
        up_t = [m for m in ids if old.get(m) != "up" and cur[m] == "up"]
        down_t = [m for m in ids if old.get(m) in ("up", "chat-only") and cur[m] == "down"]
        deg_t = [m for m in ids if old.get(m) == "up" and cur[m] == "chat-only"]
        parts = []
        if up_t: parts.append("上线: " + ", ".join(up_t))
        if down_t: parts.append("下线: " + ", ".join(down_t))
        if deg_t: parts.append("降级(仅chat): " + ", ".join(deg_t))
        report = "；".join(parts) if parts else "状态集合变化"
        with CHANGES.open("a", encoding="utf-8") as f:
            f.write(f"{now.isoformat(timespec='seconds')}  rev{revision}  {report}\n")
        toast_title = "牛牛站监控"
        names = up_t + down_t + deg_t
        toast_text = report if len(names) <= 4 else "；".join(parts)[:180]
    else:
        revision = prev.get("revision") or 1
        report = "无变化"

    up = [m for m in ids if cur.get(m) == "up"]
    summary = f"可用 {len(up)}/{len(ids)}" + (": " + ", ".join(up) if up else "（全挂）")
    if changed:
        summary += f" · 变化: {report}"

    # ---- log ----
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "models": results, "ids": ids},
                           ensure_ascii=False) + "\n")
    # ---- state ----
    STATE.write_text(json.dumps({
        "revision": revision, "updated": now.isoformat(timespec="seconds"),
        "summary": summary, "ids": ids, "models": cur,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    # ---- report ----
    REPORT.write_text(f"{now.strftime('%Y-%m-%d %H:%M')} {report}\n", encoding="utf-8")
    # ---- status.md ----
    cfg = configured_ids()
    hist = []
    try:
        for ln in LOG.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                rec = json.loads(ln)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= now - timedelta(hours=24):
                    hist.append(rec)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass

    def uptime(m):
        n = sum(1 for h in hist if (h.get("models") or {}).get(m, {}).get("state") == "up")
        c = sum(1 for h in hist if (h.get("models") or {}).get(m, {}).get("state") == "chat-only")
        return n, c, len(hist)

    rows = []
    order = list(cfg) + [m for m in ids if m not in cfg]
    for m in order:
        r = results.get(m, {})
        st = r.get("state", "?")
        icon = {"up": "✅ 双线", "chat-only": "⚠️ 仅chat", "down": "❌"}.get(st, st)
        n, c, tot = uptime(m)
        rate = f"{n}/{tot}" + (f" (+{c} 仅chat)" if c else "") if tot else "-"
        tag = "●" if m in cfg else ""
        rows.append(f"| {tag}{m} | {icon} | {rate} | {r.get('err','') or '—'} |")

    changes_tail = []
    try:
        changes_tail = CHANGES.read_text(encoding="utf-8").splitlines()[-8:]
    except Exception:  # noqa: BLE001
        pass

    md = [
        "# 牛牛站模型监控",
        f"更新: {now.strftime('%Y-%m-%d %H:%M:%S')} · revision {revision} · 24h 样本 {len(hist)} 次",
        "",
        f"**{summary}**",
        "",
        "| 模型 (●=配置在 models.yml) | 现况 | 24h 可用 | 本次错误 |",
        "|---|---|---|---|",
        *rows,
        "",
        "## 最近变化",
        *(changes_tail or ["（暂无）"]),
        "",
    ]
    STATUS.write_text("\n".join(md) + "\n", encoding="utf-8")

    if feed_opened(old, up):
        post_webhook(webhook_url, webhook_body(up, now))
    if changed or first:
        toast(toast_title, toast_text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                    "fatal": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
