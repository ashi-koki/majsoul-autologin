#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
majsoul_auto.py —— 雀魂(https://game.maj-soul.com/1/) 自动登录脚本（独立版）。

不依赖「小苏菲」exe：下载本包后直接运行，首次运行会在包内生成 data/（保存登录状态）
与 settings.json（配置），登录一次后即可反复免登录。

   - 纯标准库（依赖同目录 common.py 与 majsoul_cdp.py）；
   - 通过 Chrome 的 --remote-debugging-port 用 CDP 控制浏览器、监听游戏网络；
   - 游戏是 Unity WebGL，界面全在 canvas 里、没有 DOM 按钮，因此登录状态靠监听
     游戏与服务端的 WebSocket 协议消息判定：
        `lq.Lobby.loginSuccess`   → 登录完成（客户端发出）
        `lq.Lobby.loginBeat`      → 大厅心跳
     注意：loginSuccess/loginBeat 只代表「登录握手完成」，此时大厅画面往往还在加载
     资源（实测还要再等 4~8 秒）。因此本脚本在握手完成后，再用 CDP 截屏 + 纯标准库
     PNG 解码判断画面「近黑占比」——加载页接近全黑(>90%)，大厅五颜六色(<35%)，
     确认画面真正渲染后才算「进入游戏主界面」。
   - 首次登录 / 无 token 时停在登录页，`--login` 保持浏览器等你手动登录；
   - 连接不稳定（网关路由失败）时自动刷新重选线路 = 「更换信号更好的连接」；
   - 进入主界面后 POST 一个 JSON webhook 通知，再按 `--delay` 停留后关闭。

用法（详见同目录 README.md）:
    python majsoul_auto.py                 # 进主界面 → 发 webhook → 停留 30s → 关闭
    python majsoul_auto.py --delay 0       # 进主界面后立即关闭
    python majsoul_auto.py --delay 120     # 进主界面后停留 120 秒再关闭
    python majsoul_auto.py --keep-open     # 进主界面后保持浏览器打开
    python majsoul_auto.py --login         # 首次登录：停在登录页等你手动登录
    python majsoul_auto.py --timeout 180   # 最多等 180 秒
    python majsoul_auto.py --no-webhook    # 不发 webhook
    python majsoul_auto.py --json          # 输出机器可读 JSON
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

# Windows 控制台 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 让调度器从任意位置调用时都能找到同目录的 common.py / majsoul_cdp.py
try:
    _HERE = str(Path(__file__).resolve().parent)
except NameError:  # 某些内嵌执行环境可能没有 __file__
    _HERE = str(Path.cwd())
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common  # noqa: E402
import majsoul_cdp as cdp  # noqa: E402

DEFAULT_PORT = 9229

# 协议消息里的 ASCII 关键词（雀魂 WebSocket 是二进制 protobuf，CDP 返回 base64，
# 解码后能找到这些可读字符串）
SIG_LOGIN_SUCCESS = b"loginSuccess"       # 登录完成（客户端发出）
SIG_LOGIN_BEAT = b"loginBeat"             # 大厅心跳
SIG_LOGIN_TRY = b"oauth2Login"            # 正在发起登录
SIG_CONNECT = b"requestConnection"        # 正在连接网关路由
SIG_LOBBY_FETCH = (
    b"fetchInfo", b"fetchAnnouncement", b"fetchBannerActivityData",
    b"fetchChallenge", b"fetchDailyTask", b"fetchConnectionInfo",
    b"fetchActivityFlipInfo", b"fetchRollingNotice", b"fetchAchievementRate",
)

# 画面「近黑占比」阈值：加载页实测 >90%，大厅实测 <35%。低于该值即认为大厅已渲染。
LOBBY_RENDER_MAX_DARK = 0.55


def _print(*args, **kwargs):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(*args, **kwargs)


def _frame_has_needle(payload_data, needle):
    """payload_data 为 CDP 给的 base64(二进制帧) 或原文(文本帧)。返回是否含关键词。"""
    if not payload_data:
        return False
    try:
        data = base64.b64decode(payload_data, validate=False)
    except Exception:
        data = payload_data.encode("utf-8", "replace")
    return needle in data


class LoginMonitor:
    """消费 CDP 网络事件，跟踪登录进度。"""

    def __init__(self):
        self.login_success = False
        self.login_beats = 0
        self.login_try = False
        self.connect = False
        self.ws_opens = 0
        self.ws_errors = []
        self.gateway_urls = []
        self.clientgate_ok = False
        self.lobby_fetches = set()   # 大厅初始化期间出现的 fetch* 消息类型

    @property
    def login_done(self):
        """登录握手完成 = 客户端已发出 loginSuccess 或首个 loginBeat。

        这仅是「登录完成」，大厅画面此刻可能仍在加载资源（实测要晚 4~8 秒）。
        是否「进入主界面」须再靠截屏近黑占比确认（见 run_until_main 里的判定）。
        """
        return self.login_success or self.login_beats >= 1

    def feed(self, events):
        for e in events:
            m = e.get("method", "")
            p = e.get("params", {})
            if m == "Network.webSocketCreated":
                self.ws_opens += 1
                u = p.get("url", "")
                if "gateway" in u:
                    self.gateway_urls.append(u)
            elif m == "Network.webSocketFrameError":
                self.ws_errors.append(p.get("errorMessage", ""))
            elif m in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
                pl = p.get("response", {}).get("payloadData", "")
                if _frame_has_needle(pl, SIG_LOGIN_SUCCESS):
                    self.login_success = True
                if _frame_has_needle(pl, SIG_LOGIN_BEAT):
                    self.login_beats += 1
                if _frame_has_needle(pl, SIG_LOGIN_TRY):
                    self.login_try = True
                if _frame_has_needle(pl, SIG_CONNECT):
                    self.connect = True
                for sig in SIG_LOBBY_FETCH:
                    if _frame_has_needle(pl, sig):
                        self.lobby_fetches.add(sig.decode("ascii", "replace"))
                        break
            elif m == "Network.responseReceived":
                url = p.get("response", {}).get("url", "")
                if "clientgate" in url and "routes" in url:
                    self.clientgate_ok = True

    def summary(self):
        return {
            "login_success": self.login_success,
            "login_beats": self.login_beats,
            "login_try": self.login_try,
            "ws_opens": self.ws_opens,
            "ws_errors": self.ws_errors[-3:],
            "gateway_urls": self.gateway_urls[-3:],
            "clientgate_ok": self.clientgate_ok,
            "lobby_fetches": sorted(self.lobby_fetches),
        }


def _route_name(gateway_urls):
    for u in reversed(gateway_urls):
        m = re.search(r"(route-\d+)", u or "")
        if m:
            return m.group(1)
    return None


# ---- 主流程：等待进入游戏主界面 -------------------------------------------

def run_until_main(port, timeout, retries, on_progress=None):
    """启动并等待进入主界面。返回 (result_dict, exit_code)。"""
    # 0) 首次运行：生成配置模板、确保数据目录可用
    common.ensure_settings()
    # 1) 关闭旧的、同 user-data-dir 的 Chrome（避免调试端口不被监听）
    common.close_chrome()
    # 2) 启动
    common.launch_chrome(port)

    start = time.time()

    # 3) 等 CDP 就绪并拿到 page target
    try:
        target = cdp.find_page_target(port, url_substr="maj-soul",
                                      retries=40, delay=0.5)
    except TimeoutError:
        return {"ok": False, "reason": "页面未就绪(无法连接调试端口)", "code": 1}, 1
    client = cdp.CDPClient(target["webSocketDebuggerUrl"], timeout=30)
    try:
        client.call("Network.enable")
        client.call("Page.enable")
    except Exception as e:
        client.close()
        return {"ok": False, "reason": f"CDP 初始化失败: {e}", "code": 1}, 1

    monitor = LoginMonitor()
    last_report = [0.0]
    done_since = None       # loginSuccess/loginBeat 首次出现的时间
    last_dark = [None]      # 最近一次截屏的近黑占比（供结果摘要）

    def lobby_rendered():
        """截屏并算「近黑占比」；返回 None 表示截屏不可用(解码失败/命令失败)。"""
        try:
            r = client.call("Page.captureScreenshot", {"format": "png"})
            ratio = common.png_dark_ratio(r.get("result", {}).get("data", ""))
        except Exception:
            return None
        if ratio is not None:
            last_dark[0] = round(ratio, 3)
        return ratio

    def _wait(deadline):
        """等至 deadline；「登录握手完成 + 大厅画面真正渲染」后返回 True。"""
        nonlocal done_since
        next_shot = 0.0
        while time.time() < deadline:
            events = client.drain_events()
            if events:
                monitor.feed(events)
            now = time.time()
            if on_progress and now - last_report[0] >= 5:
                on_progress(monitor)
                last_report[0] = now
            if monitor.login_done:
                if done_since is None:
                    done_since = now
                    next_shot = now           # 立即截第一张
                if now >= next_shot:
                    ratio = lobby_rendered()
                    if ratio is None:
                        # 截屏不可用 → 回退：握手完成后再等 8 秒即视为进入大厅
                        if now - done_since >= 8:
                            return True
                        next_shot = now + 1.0
                    elif ratio < LOBBY_RENDER_MAX_DARK:
                        # 画面已从「加载页(全黑)」变成「大厅(多彩)」，稳定 2 秒再返回
                        stable_until = now + 2
                        while time.time() < stable_until:
                            monitor.feed(client.drain_events())
                            time.sleep(0.3)
                        return True
                    else:
                        next_shot = now + 1.5   # 还没渲染完，稍后再截
            time.sleep(0.3)
        return False

    def _success(reason):
        account = client.js("localStorage.getItem('account')") or None
        client.close()
        return {
            "ok": True, "reason": reason,
            "account": account,
            "route": _route_name(monitor.gateway_urls),
            "duration": round(time.time() - start, 1),
            "dark_ratio": last_dark[0],
            "monitor": monitor.summary(), "code": 0,
        }, 0

    # 4) 首轮等待
    if _wait(time.time() + timeout):
        return _success("已进入游戏主界面(已登录)")

    # 5) 未成功则刷新页面重试（网络抖动/线路差/登录被拒时多试几次）
    for attempt in range(1, retries + 1):
        token = client.js("localStorage.getItem('access_token')") or None
        if not token and not monitor.login_try:
            # 没有登录 token、也从未发起登录 → 刷新无意义（需先 --login 手动登录）
            _print("[信息] 无登录 token 且未发起登录，跳过重试(请先 --login 手动登录)。")
            break
        _print(f"[信息] 未进入主界面，刷新页面重试(第 {attempt}/{retries} 次)...")
        try:
            client.call("Page.reload", {"ignoreCache": False})
        except Exception as e:
            _print(f"[警告] 刷新失败: {e}")
            break
        done_since = None   # 重置：新一轮握手完成的时间从零计时
        last_dark[0] = None
        if _wait(time.time() + timeout):
            return _success(f"重试第 {attempt} 次后已进入游戏主界面")

    monitor.feed(client.drain_events())
    client.close()
    if not monitor.login_try and not monitor.connect:
        reason = "未登录(无 token 或页面未加载)"
    elif monitor.login_try and not monitor.login_done:
        reason = "登录被拒绝/可能 token 已过期(重试仍失败)"
    else:
        reason = "登录未完成或连接失败"
    return {"ok": False, "reason": reason,
            "duration": round(time.time() - start, 1),
            "monitor": monitor.summary(), "code": 1}, 1


# ---- webhook --------------------------------------------------------------

def notify_webhook(url, result):
    """登录成功后 POST 一个 JSON webhook，返回 True/False。"""
    title = "雀魂自动登录成功"
    parts = [result.get("reason") or "已进入游戏主界面"]
    if result.get("account"):
        parts.append(f"账号 {result['account']}")
    if result.get("route"):
        parts.append(f"线路 {result['route']}")
    if result.get("duration") is not None:
        parts.append(f"耗时 {result['duration']}s")
    content = " | ".join(parts)

    status, body = common.send_webhook(url, title, content)
    if status is not None and 200 <= status < 300:
        _print(f"[信息] webhook 已发送(HTTP {status}): {content}")
        return True
    _print(f"[警告] webhook 发送失败: {body}")
    return False


def notify_failure_webhook(url, result):
    """登录失败时 POST 一个 JSON webhook 提醒，返回 True/False。"""
    title = "雀魂自动登录失败"
    m = result.get("monitor") or {}
    parts = [result.get("reason") or "未登录"]
    if m.get("login_try"):
        parts.append("已发起登录但未成功(可能 token 过期)")
    if m.get("ws_errors"):
        parts.append(f"连接错误 {len(m['ws_errors'])} 次")
    if m.get("gateway_urls"):
        parts.append(f"线路 {_route_name(m['gateway_urls']) or '未知'}")
    if result.get("duration") is not None:
        parts.append(f"耗时 {result['duration']}s")
    content = " | ".join(parts)

    status, body = common.send_webhook(url, title, content)
    if status is not None and 200 <= status < 300:
        _print(f"[信息] 失败提醒 webhook 已发送(HTTP {status}): {content}")
        return True
    _print(f"[警告] 失败提醒 webhook 发送失败: {body}")
    return False


def _webhook_url(args_webhook, settings):
    """解析 webhook 地址：--webhook > settings.json > 默认；未配置返回空串。"""
    return (args_webhook or settings.get("webhook_url")
            or common.DEFAULT_WEBHOOK or "").strip()


# ---- 命令行入口 -----------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="雀魂自动登录(独立版)：监听游戏协议，确认进入主界面后发 webhook 并关闭。")
    ap.add_argument("--timeout", type=int, default=150, metavar="秒",
                    help="等待进入主界面的最大秒数(默认 150)")
    ap.add_argument("--retries", type=int, default=3, metavar="次",
                    help="未进入主界面时刷新页面重试的次数(默认 3)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="Chrome 远程调试端口(默认 9229)")
    ap.add_argument("--delay", type=float, default=30.0, metavar="秒",
                    help="进入主界面后停留多少秒再关闭(默认 30；0=立即关闭)")
    ap.add_argument("--keep-open", action="store_true",
                    help="进入主界面后保持浏览器打开(忽略 --delay)")
    ap.add_argument("--login", action="store_true",
                    help="未登录时停在登录页，等待手动登录(交互式)")
    ap.add_argument("--webhook", type=str, default=None, metavar="URL",
                    help="覆盖 settings.json 里的 webhook_url")
    ap.add_argument("--no-webhook", action="store_true",
                    help="不发 webhook")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON 结果(供调度器解析)")
    args = ap.parse_args(argv)

    def progress(monitor):
        s = monitor.summary()
        if not args.json:
            _print(f"[信息] 进度: 连接={s['ws_opens']} "
                   f"登录成功={s['login_success']} 心跳={s['login_beats']} "
                   f"错误={len(s['ws_errors'])}")

    result, code = run_until_main(args.port, args.timeout, args.retries,
                                  on_progress=progress)

    if result["ok"]:
        _print("[完成] " + result["reason"])
        # 发 webhook（默认开启；未配置 webhook_url 则跳过）
        if not args.no_webhook:
            settings = common.load_settings()
            url = _webhook_url(args.webhook, settings)
            if url:
                result["webhook"] = notify_webhook(url, result)
            else:
                _print("[信息] 未配置 webhook_url，跳过通知。")

        # 停留（让用户能看清是否真的登录了），然后关闭
        if args.keep_open:
            _print("[信息] 浏览器保持打开。")
        elif args.delay > 0:
            _print(f"[信息] 停留 {args.delay:g} 秒后关闭浏览器...")
            try:
                time.sleep(args.delay)
            except KeyboardInterrupt:
                pass
            _print("[信息] 关闭浏览器并保存会话...")
            common.close_chrome()
        else:
            _print("[信息] 关闭浏览器并保存会话...")
            common.close_chrome()
    else:
        _print("[未完成] " + result["reason"])
        # 登录失败也发 webhook 提醒（--login 手动登录模式是预期等待，不发）
        if not args.no_webhook and not args.login:
            settings = common.load_settings()
            url = _webhook_url(args.webhook, settings)
            if url:
                result["webhook"] = notify_failure_webhook(url, result)
            else:
                _print("[信息] 未配置 webhook_url，跳过失败提醒。")
        if args.login:
            _print("[信息] 浏览器保持打开，请手动登录；登录成功后会自动记录。")
            _print("[信息] 之后再用本脚本即为免登录。")
        elif not args.keep_open:
            common.close_chrome()

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    main()
