#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛牛酱投喂快检（控制台版）。

有投喂退出码 0，全挂退出码 1，可直接被脚本调用。
图形面板见 serve.py（桌面快捷方式「牛牛酱投喂」）。
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import watch  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ids = watch.configured_ids() or watch.FALLBACK_IDS
    with ThreadPoolExecutor(max_workers=8) as ex:
        live = dict(ex.map(lambda m: watch.probe(m, timeout=30), ids))
    up = [m for m in ids if live[m]["state"] == "up"]
    print(f"牛牛酱投喂检查 · {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
    for m in ids:
        r = live[m]
        icon = {"up": "✔ 双线", "chat-only": "△ 仅chat", "down": "✘"}[r["state"]]
        detail = f"{r['ms']}ms" if r["state"] != "down" else r.get("err", "")[:60]
        print(f"  {icon:8} {m:<24}{detail}")
    print("—")
    print("有投喂：" + "、".join(up) if up else "没投喂：配置的模型全挂")
    return 0 if up else 1


if __name__ == "__main__":
    sys.exit(main())
