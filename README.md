# 雀魂自动登录（独立版）

一个**不依赖「小苏菲」exe** 的雀魂（https://game.maj-soul.com/1/ ）自动登录脚本。
下载本包后直接运行即可：首次运行会在包内生成 `data/`（保存登录状态的 Chrome 用户目录）
与 `settings.json`（配置），登录一次后反复调用免登录。

**纯标准库、零第三方依赖**（只需系统装有 Python 3.7+ 与 Chrome），可被任务调度器
（OneDragon ScriptChainer 等）直接调用。

## 它做什么

雀魂网页端是 **Unity WebGL** 游戏，界面全部画在 canvas 里、没有 DOM 按钮可点，所以
不能靠「找按钮 → 点击」来登录。本脚本用 Chrome 的 `--remote-debugging-port`（CDP）控制
浏览器，分**两步**判定「真正进入主界面」：

1. **登录握手**：监听游戏与服务端的 WebSocket 协议，收到客户端发出的
   `lq.Lobby.loginSuccess`（或首个 `lq.Lobby.loginBeat`）即登录完成；
2. **画面渲染**：握手完成时大厅往往还在加载资源（实测要再等 4~8 秒）。脚本用 CDP
   截屏 + 纯标准库 PNG 解码算「近黑占比」——加载页接近全黑(>90%)，大厅五颜六色(<35%)，
   确认画面真正渲染后才判定进入主界面。

脚本启动后自动完成：**打开游戏 → 自动登录 → 确认进入主界面 → POST webhook → 停留
`--delay` 秒 → 关闭**（退出码 `0` = 成功 / `1` = 失败 / `2` = 启动出错）。

## 快速开始

1. 把整个 `majsoul-autologin/` 文件夹下载到本地（Windows）。
2. 首次登录（会让你在弹出的浏览器里手动登录一次）：

   ```powershell
   cd majsoul-autologin
   python majsoul_auto.py --login
   ```

3. 之后即可反复免登录：

   ```powershell
   python majsoul_auto.py                 # 进主界面 → 发 webhook → 停留 30s → 关闭
   python majsoul_auto.py --delay 0       # 进主界面后立即关闭
   python majsoul_auto.py --keep-open     # 进主界面后保持浏览器打开
   ```

## 命令一览

| 命令 | 说明 |
|---|---|
| `python majsoul_auto.py` | 默认：进主界面 → 发 webhook → 停留 30 秒 → 关闭 |
| `--delay 秒` | 进入主界面后停留多久再关闭（默认 30；`0` = 立即关闭） |
| `--keep-open` | 进入主界面后保持浏览器打开（忽略 `--delay`） |
| `--login` | 首次登录：未登录时停在登录页等你手动登录 |
| `--timeout 秒` | 等待进入主界面的最大秒数（默认 150） |
| `--retries 次` | 未进入主界面时刷新页面重试的次数（默认 3） |
| `--port N` | Chrome 远程调试端口（默认 9229） |
| `--webhook URL` | 覆盖 settings.json 里的 webhook_url |
| `--no-webhook` | 不发 webhook |
| `--json` | 额外输出机器可读 JSON |

退出码：`0` = 已进入主界面，`1` = 超时未登录 / 连接失败，`2` = 找不到 Chrome 等启动错误。

## 界面上的几种情况如何处理

- **首次登录**：没有 `access_token` 时游戏停在登录页，`--login` 会保持浏览器打开等你
  手动登录；登录成功后 token 自动持久化到 `data/`，之后免登录。
- **记录登录状态**：登录状态存在包内 `data/`（Chrome 用户目录），登录一次即可反复调用。
- **「更换信号更好的连接」**：某条网关线路连接失败（`net::ERR_CONNECTION_CLOSED` 等）时，
  脚本自动刷新页面让游戏重选线路，等效于手动切换更优连接。
- **直到进入主界面**：以「`loginSuccess`/`loginBeat` 握手完成 + 截屏确认画面渲染」为
  双重信号，画面真正显示大厅后才返回（避免页面还在加载就误判成功）。
- **登录失败**：超时仍未进入主界面会刷新页面重试（默认 3 次），并 POST 一条
  「登录失败」webhook 提醒。
- **登录过期弹窗**：token 过期时游戏会弹窗要求重新登录。该弹窗的具体样子尚未抓取，
  暂不自动点击，仅重试并告警（后续可补充自动重登）。

## webhook 通知

进入主界面后，脚本会向 `settings.json` 里的 `webhook_url` POST 一个 JSON：

```json
{"title": "雀魂自动登录成功",
 "content": "已进入游戏主界面(已登录) | 账号 xxx@qq.com | 线路 route-3 | 耗时 36.5s"}
```

**登录失败**（超时仍未进入主界面，`--login` 手动登录模式除外）时，会 POST 一条失败提醒：

```json
{"title": "雀魂自动登录失败",
 "content": "登录被拒绝/可能 token 已过期(重试仍失败) | 已发起登录但未成功 | 线路 route-3 | 耗时 45.2s"}
```

- 通知地址在 `settings.json` 的 `webhook_url` 里填写（**默认留空 = 不发送**），或用
  `--webhook URL` 临时覆盖、`--no-webhook` 关闭。
- 内容不包含密码。

## 配置文件 settings.json（首次运行自动生成）

```json
{
  "ms_url": "https://game.maj-soul.com/1/",
  "browser_width": 960,
  "browser_height": 540,
  "custom_browser_path": "",
  "webhook_url": ""
}
```

- `custom_browser_path` 留空则自动查找系统 Chrome（也可填 Edge 等浏览器完整路径）。
- `webhook_url` 填你的通知地址（n8n / 钉钉 / 飞书机器人等），留空则不发送通知。

## 配合调度器

以 OneDragon ScriptChainer 为例：

```yaml
- display_name: majsoul_autologin
  script_type: python
  script_path: C:\...\majsoul-autologin\majsoul_auto.py
  script_arguments: '--delay 30 --json'
  ...
```

- `script_path` 指向包内的 `majsoul_auto.py` 即可（脚本会自己找到同目录的
  `common.py` / `majsoul_cdp.py`）。
- 建议加 `--json` 让调度器解析 `{"ok": true, ...}` 判断结果；`--delay` 按需调整。
- 若调度器内嵌执行导致找不到兄弟模块，就把 `script_arguments` 留空、让其在包目录下
  以文件方式运行即可。

## 目录结构

```
majsoul-autologin/
├── majsoul_auto.py   # 主脚本：自动登录 + webhook + 停留关闭
├── common.py         # 共享工具：路径/浏览器启动关闭/webhook/PNG 解码
├── majsoul_cdp.py    # 纯标准库 CDP(Chrome DevTools Protocol) 客户端
├── settings.json     # 配置（首次运行自动生成）
└── data/             # Chrome 用户目录，保存登录状态（首次运行自动生成）
```

## 安全提示

- `data/` 里存着**雀魂登录 token（明文）**，`settings.json` 里可配 webhook 地址；
  **不要把这些提交到 GitHub 或分享给别人。**
- 本仓库的 `.gitignore` 已忽略 `data/` 与 `settings.json`。

## 作者 / 致谢

- 作者：[@ashi-koki](https://github.com/ashi-koki)
- 开发与调试协助：Claude（Anthropic）
