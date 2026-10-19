# 今天牛牛酱投喂了没

牛牛酱公益站（free.quanthatch.com）模型可用性监控面板。

**在线版（打开即看）：<https://sutera-diffusus.github.io/niuniu-watch/>**
静态快照页，可由 GitHub Actions 或本地程序每小时发布；页面每分钟自动拉取新快照。
实时探测、一键实测、控制台等完整功能请使用本地版。

本地版：Windows 用单文件 exe 双击即用；Kubuntu 源码运行，`install.sh` 一键装好桌面启动器与 systemd 定时器。
两种方式都会自动起本地服务（`127.0.0.1:8791`）并打开浏览器。本地定时器仍然可选。

## GitHub Actions

[`.github/workflows/hourly-snapshot.yml`](.github/workflows/hourly-snapshot.yml) 在 GitHub 上跑 `python3 serve.py --hourly`：巡检，把 `docs/` 推回默认分支，再用 Actions 把这个目录发布到 Pages。Windows 计划任务和 Kubuntu 的 `niuniu-watch.timer` 不用改；两处不要同时推 `docs/`。

合并后：

1. **Settings → Secrets and variables → Actions** 添加 `NIUNIU_API_KEY`（必填，与本地 `config.json` 的 `api_key` 相同，不会写入仓库）。可选 `NIUNIU_WEBHOOK_URL`：只写在 runner 的临时 `config.json` 里（已在 `.gitignore`），用来发「没投喂 → 开饭」。
2. **Settings → Pages → Build and deployment**，Source 选 **GitHub Actions**（一次即可）。用 `GITHUB_TOKEN` 推上去的提交不会触发原来的「从分支 / docs 部署」。
3. 打开 Actions，手动运行一次 **Hourly snapshot**（`workflow_dispatch`，选默认分支）。

定时规则是每小时第 5 分钟。**cron 是 UTC**；免费 / 共享 runner 上经常会晚几分钟甚至更久。任务里 `TZ=Asia/Shanghai`，页面上的钟点仍是 +08。

`state.json`、`log.jsonl`、`changes.log` 放在 Actions cache 里跨次保留，不进 git。日志只留最近 **120 天**（够热力图和连胜，也避免缓存一直变大）。缓存被清掉或第一次跑时，这一轮只建基线，**不会**发开饭 webhook；热力图从新的巡检重新积。

## 功能

- 全站模型全量实测（chat + responses 双线），**能用的置顶**，默认只显示前 5 行、可展开
- 行情头带：巨型可用计数 / 24h 供餐带 / 投喂率 / 巡检统计
- 推荐榜（24h 可用率 → 延迟）、变化记录、系统通知、深浅双主题
- 点模型行打开抽屉：实时探测（含工具调用验证）、延迟趋势、巡检历史、复制 selector
- 「一键实测」走真实 omp 会话；每小时自动巡检（GitHub Actions / Windows 计划任务 / Kubuntu systemd 用户定时器）
- 开饭热力图：按星期 × 整点看历史上什么时候在投喂（样本少会标明，静态快照里也带得走）
- 可选 Webhook：刚从「没投喂」变成「开饭」时 POST 一次（见下文）；桌面通知仍由 `notify` 控制

## 使用（Kubuntu / KDE）

1. 克隆仓库，在目录里放一个 `config.json`（参考 `config.example.json`），填入你在站点申请的密钥：

   ```json
   { "api_key": "sk-你的密钥", "notify": true }
   ```

2. 运行 `./install.sh`：装好桌面启动器（应用菜单 + 桌面）与 systemd 用户定时器
   （每小时巡检 + 发布快照）。换间隔用 `./install.sh --interval 30`。
3. 从应用菜单或桌面双击「今天牛牛酱投喂了没」打开面板。

依赖：python3（只用标准库）；`git`（快照发布）；`libnotify-bin`（桌面通知，缺失时退回 kdialog）。

```bash
systemctl --user status niuniu-watch.timer    # 定时器状态
systemctl --user start niuniu-watch.service   # 立即巡检一次
journalctl --user -u niuniu-watch -n 50       # 最近日志
./install.sh --uninstall                      # 卸载（数据文件保留）
```

面板底部控制台的频率切换 / 暂停 / 立即巡检，同样直接操作这个 systemd 定时器。

## 使用（Windows exe）

1. 下载 Release 中的 `niuniu-feed-v1.0.exe`
   （GitHub 的 Release 资产名不支持中文，下载后可自行重命名为 `今天牛牛酱投喂了没.exe`，不影响使用）
2. 同目录放一个 `config.json`（同上）
3. 双击 exe → 浏览器自动打开面板；再次双击只会把面板拉起来
4. 面板底部控制台可切换巡检频率 / 暂停 / 立即巡检（写入计划任务 `niuniu-watch`）

> 密钥只存在本地 `config.json`，不会打进 exe，也不会出现在本仓库。

## 从源码构建（Windows exe）

需要 Python 3.10+ 与 `pyinstaller`，直接运行 `build.cmd`（产物为单文件 exe）。
Kubuntu 不需要打包，`install.sh` 直接以源码运行。

## Webhook 开饭通知

在 `config.json` 增加 `webhook_url`（参考 `config.example.json`）。留空或不写则关闭。
`notify` 仍只控制桌面气泡，和 Webhook 互不影响。地址不要提交进仓库。

```json
{
  "api_key": "sk-你的密钥",
  "notify": true,
  "webhook_url": "https://example.com/hooks/niuniu"
}
```

- **地址**：`http://` 或 `https://`，接收 `POST`，`Content-Type: application/json`。
- **何时发**：只在开饭沿上发一次——上一轮一个 `up` 都没有，这一轮至少有一个模型变成可用。已经在开饭、重复巡检、模型只是多上线一个、或者第一次建立基线，都不会发。
- **失败**：超时、连不上、非 2xx 都只在 stderr 记一行（`webhook 失败: …`），巡检和面板照常跑。

正文示例：

```json
{
  "status": "up",
  "menu": ["glm-5.3-flash"],
  "ts": "2026-10-16T12:00:01+08:00",
  "text": "开饭啦：glm-5.3-flash"
}
```

| 字段 | 含义 |
|---|---|
| `status` | 固定 `"up"`，表示刚变为开饭 |
| `menu` | 当前可用模型 id；一个都没有时为 `[]` |
| `ts` | 本轮巡检时间，ISO 8601，带时区 |
| `text` | 短中文，多个模型用顿号接在「开饭啦：」后面 |

## 文件说明

| 文件 | 作用 |
|---|---|
| `serve.py` | 面板服务与 API（quick/detail/try/sweep/task…）；`--hourly`＝巡检 + 发布快照 |
| `.github/workflows/hourly-snapshot.yml` | GitHub 上的每小时巡检 + Pages 发布 |
| `watch.py` | 探测核心；单次巡检（由定时任务调用），开饭沿上可发 webhook |
| `publish.py` | 生成 `docs/` 静态快照并推送到 GitHub Pages |
| `dashboard.html` | 全部界面；exe 优先读取同目录此文件，改样式无需重打包 |
| `check.py` | 控制台快检（有投喂退出码 0） |
| `install.sh` | Kubuntu 安装/卸载（.desktop 启动器 + systemd 用户定时器） |
| `build.cmd` / `create-shortcut.ps1` | Windows 一键打包 / 创建桌面快捷方式 |
