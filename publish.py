#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成静态快照页（docs/index.html + docs/data.json）并推送到 GitHub Pages。

别人打开 https://<user>.github.io/niuniu-watch/ 即可查看；
页面每分钟自动拉取 data.json 刷新，本脚本由每小时定时任务调用（Windows 计划任务 / systemd 用户定时器）。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
DOCS = ROOT / "docs"


def build():
    import serve
    data = serve.payload(force=True, wait=25.0)
    if "error" in data:
        return data
    data = serve.apply_snapshot_sweep(data)
    html = serve.asset("dashboard.html").read_text(encoding="utf-8")
    snap = "<script>window.__SNAPSHOT__ = " + json.dumps(data, ensure_ascii=False) + ";</script>"
    if "<body>" not in html:
        raise RuntimeError("dashboard.html 缺少 <body> 注入点")
    html = html.replace("<body>", "<body>\n" + snap, 1)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    return data


def git_push():
    def run(args):
        return subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)
    run(["add", "docs"])
    st = run(["status", "--porcelain", "docs"])
    if not (st.stdout or "").strip():
        return "无变化，跳过推送"
    run(["commit", "-m", f"snapshot {time.strftime('%Y-%m-%d %H:%M')}"])
    r = run(["push"])
    return "已推送" if r.returncode == 0 else f"推送失败: {(r.stderr or '')[:200]}"


def main():
    try:
        data = build()
    except Exception as e:  # noqa: BLE001
        print("publish error:", e)
        return 1
    if "error" in data:
        print("publish skipped:", data["error"])
        return 1
    print("snapshot:", data.get("ts"), "| up:", data["counts"]["up"], "/", data["counts"]["total"])
    print("git:", git_push())
    return 0


if __name__ == "__main__":
    sys.exit(main())
