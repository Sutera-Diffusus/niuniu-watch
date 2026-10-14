#!/usr/bin/env bash
# 今天牛牛酱投喂了没 · Kubuntu / KDE 安装脚本
#
#   ./install.sh                 安装：桌面启动器（应用菜单 + 桌面）+ systemd 用户定时器
#   ./install.sh --interval 30   巡检间隔（15/30/60/120/180/360/720 分钟，默认 60）
#   ./install.sh --uninstall     卸载定时器与启动器（log.jsonl 等数据文件保留）
#
# 定时器执行 `serve.py --hourly`：探测牛牛站全部模型 + 发布静态快照。
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

usage() { sed -n '2,7s/^# \{0,1\}//p' "$0"; }

INTERVAL=60
DO_UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) DO_UNINSTALL=1 ;;
    --interval) INTERVAL="${2:?--interval 需要一个数字}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
  shift
done

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "错误：未找到 python3（sudo apt install python3）"; exit 1; }

if [ "$DO_UNINSTALL" = 1 ]; then
  systemctl --user disable --now niuniu-watch.timer 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/niuniu-watch.service" \
        "$HOME/.config/systemd/user/niuniu-watch.timer"
  systemctl --user daemon-reload 2>/dev/null || true
  rm -f "$HOME/.local/share/applications/niuniu-watch.desktop"
  rm -f "$HOME/Desktop/今天牛牛酱投喂了没.desktop"
  echo "已卸载：定时器与桌面启动器（log.jsonl 等数据文件保留）"
  exit 0
fi

command -v git >/dev/null || echo "提示：未找到 git，快照发布（publish.py）会失败 → sudo apt install git"
command -v notify-send >/dev/null || echo "提示：未找到 notify-send，通知将退回 kdialog → sudo apt install libnotify-bin"
[ -f config.json ] || echo "提示：还没有 config.json —— 复制 config.example.json 并填入 api_key"

# ---- 1. 桌面启动器 ----
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
ICON="$DIR/_icon.ico"
[ -f "$ICON" ] || ICON="applications-internet"
cat > "$APPS/niuniu-watch.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=今天牛牛酱投喂了没
Comment=NiuNiu feeding dashboard
Exec="$PY" "$DIR/serve.py"
Path=$DIR
Icon=$ICON
Terminal=false
Categories=Utility;Network;
StartupNotify=false
EOF
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [ -z "$DESK" ] || [ ! -d "$DESK" ]; then DESK="$HOME/Desktop"; fi
if [ -d "$DESK" ]; then
  cp "$APPS/niuniu-watch.desktop" "$DESK/今天牛牛酱投喂了没.desktop"
  chmod +x "$DESK/今天牛牛酱投喂了没.desktop"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

# ---- 2. systemd 用户定时器（单元文件由 serve.py 生成，与面板里的设置逻辑一致） ----
NN_DIR="$DIR" NN_INTERVAL="$INTERVAL" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["NN_DIR"])
import serve
r = serve.task_set("interval", os.environ["NN_INTERVAL"])
if not r.get("ok"):
    sys.exit("定时器安装失败: %s" % r)
print("定时器已就绪：niuniu-watch.timer")
PYEOF

systemctl --user list-timers niuniu-watch.timer --no-pager || true
cat <<'EOF'

完成。常用命令：
  systemctl --user status niuniu-watch.timer    # 定时器状态
  systemctl --user start niuniu-watch.service   # 立即巡检一次
  journalctl --user -u niuniu-watch -n 50       # 最近日志
  ./install.sh --uninstall                      # 卸载
面板：应用菜单或桌面双击「今天牛牛酱投喂了没」。
（可选）想让巡检在未登录时也跑：sudo loginctl enable-linger $USER
EOF
