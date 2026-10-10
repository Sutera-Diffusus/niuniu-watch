# 今天牛牛酱投喂了没

牛牛酱公益站（free.quanthatch.com）模型可用性监控面板。
单文件 exe，双击即用：自动起本地服务（`127.0.0.1:8791`）并打开浏览器。

## 功能

- 全站模型全量实测（chat + responses 双线），**能用的置顶**，默认只显示前 5 行、可展开
- 行情头带：巨型可用计数 / 24h 供餐带 / 投喂率 / 巡检统计
- 推荐榜（24h 可用率 → 延迟）、变化记录、系统通知、深浅双主题
- 点模型行打开抽屉：实时探测（含工具调用验证）、延迟趋势、巡检历史、复制 selector
- 「一键实测」走真实 omp 会话；每小时自动巡检（Windows 计划任务）

## 使用（exe）

1. 下载 Release 中的 `今天牛牛酱投喂了没.exe`，放到任意文件夹
2. 同目录放一个 `config.json`（参考 `config.example.json`），填入你在站点申请的密钥：

   ```json
   { "api_key": "sk-你的密钥", "notify": true }
   ```

3. 双击 exe → 浏览器自动打开面板；再次双击只会把面板拉起来

> 密钥只存在本地 `config.json`，不会打进 exe，也不会出现在本仓库。

## 从源码构建

需要 Python 3.10+ 与 `pyinstaller`，直接运行 `build.cmd`（产物为单文件 exe）。

## 文件说明

| 文件 | 作用 |
|---|---|
| `serve.py` | 面板服务与 API（quick/detail/try/sweep/task…） |
| `watch.py` | 探测核心；`--watch` 单次巡检（计划任务调用） |
| `dashboard.html` | 全部界面；exe 优先读取同目录此文件，改样式无需重打包 |
| `check.py` | 控制台快检（有投喂退出码 0） |
| `build.cmd` / `create-shortcut.ps1` | 一键打包 / 创建桌面快捷方式 |
