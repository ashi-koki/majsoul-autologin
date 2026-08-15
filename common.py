#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common.py —— 独立包共享的工具：路径、浏览器启动/关闭、配置、webhook。

「雀魂自动登录」独立包（不依赖小苏菲 exe）的公共部分，零第三方依赖。
所有路径都相对本文件所在目录，首次运行会在包内生成 data/（Chrome 用户目录，
保存登录状态）与 settings.json（配置模板）。
"""

import base64
import json
import shutil
import ssl
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

# Windows 控制台 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 本文件所在目录 = 独立包根目录（不依赖 _internal）
try:
    ROOT = Path(__file__).resolve().parent
except NameError:
    ROOT = Path.cwd()

DATA_DIR = ROOT / "data"                 # Chrome 用户目录（登录状态持久化处）
SETTINGS_PATH = ROOT / "settings.json"

DEFAULT_URL = "https://game.maj-soul.com/1/"
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# webhook 通知地址：默认留空（公开发布不含私人地址），用户在 settings.json 里自行填写。
DEFAULT_WEBHOOK = ""

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_DEFAULT_SETTINGS = {
    "ms_url": DEFAULT_URL,
    "browser_width": 960,
    "browser_height": 540,
    "custom_browser_path": "",
    "webhook_url": DEFAULT_WEBHOOK,
}


def load_settings():
    """读取 settings.json，缺失字段用默认值补齐；文件不存在则返回默认。"""
    s = dict(_DEFAULT_SETTINGS)
    try:
        if SETTINGS_PATH.exists():
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def ensure_settings():
    """首次运行时生成 settings.json 模板，方便用户编辑。"""
    if not SETTINGS_PATH.exists():
        try:
            SETTINGS_PATH.write_text(
                json.dumps(_DEFAULT_SETTINGS, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass


def find_chrome(settings):
    custom = (settings.get("custom_browser_path") or "").strip()
    if custom and Path(custom).exists():
        return custom
    if Path(DEFAULT_CHROME).exists():
        return DEFAULT_CHROME
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def launch_chrome(port):
    """用系统 Chrome 打开游戏（带远程调试端口），返回 Popen。"""
    settings = load_settings()
    chrome = find_chrome(settings)
    if not chrome:
        print("[错误] 找不到系统 Chrome。请安装 Chrome，或在 settings.json 里设置"
              " custom_browser_path。")
        sys.exit(2)
    url = settings.get("ms_url") or DEFAULT_URL
    w = int(settings.get("browser_width", 960))
    h = int(settings.get("browser_height", 540))
    cmd = [
        chrome,
        f"--user-data-dir={DATA_DIR}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--window-size={w},{h}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        url,
    ]
    print(f"[信息] 启动浏览器并监听 CDP 端口 {port}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_pwsh(script):
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            capture_output=True, timeout=45, text=True, creationflags=_NO_WINDOW)
    except Exception:
        return None


def chrome_main_pids():
    """返回所有使用本包 user-data-dir 的 chrome 主进程 PID。"""
    marker = str(DATA_DIR)
    script = (
        "$m = '" + marker.replace("'", "''") + "';"
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" |"
        " Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $m + '*'))"
        " -and ($_.CommandLine -notlike '*--type=*') } |"
        " Select-Object -ExpandProperty ProcessId"
    )
    r = _run_pwsh(script)
    if not r or not r.stdout:
        return []
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def close_chrome():
    pids = chrome_main_pids()
    if not pids:
        return
    print(f"[信息] 关闭浏览器(进程 {pids})...")
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    time.sleep(1)


def send_webhook(url, title, content, timeout=15):
    """POST 一个 JSON webhook，格式 {"title":..., "content":...}。

    返回 (status:int|None, body_or_error:str)。先正常校验证书；若因证书过期/无效
    失败（自建 webhook 常见），回退到不校验证书再试一次。
    """
    payload = json.dumps({"title": title, "content": content},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"},
        method="POST")

    def _read(context=None):
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    try:
        return _read()
    except Exception as e:
        msg = str(e)
        if "CERTIFICATE" not in msg.upper() and "SSL" not in msg.upper():
            return None, msg
        # 证书过期/无效 → 不校验再试（自建 webhook 常见）
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            return _read(context=ctx)
        except Exception as e2:
            return None, str(e2)


# --------------------------------------------------------------------------- #
# 纯标准库 PNG 解码（用于判断游戏画面是否「已渲染」）
# --------------------------------------------------------------------------- #

def png_dark_ratio(data, threshold=60):
    """返回 PNG 图像中「近黑像素」的占比(0.0~1.0)；失败返回 None。

    用途：雀魂是 Unity WebGL，加载页(登录/资源加载中)几乎全黑(实测近黑占比>90%)，
    而大厅主界面五彩缤纷(实测<35%)。据此可精确判断「是否真正进入游戏主界面」。

    `data` 接受 PNG 原始字节或 base64 文本(CDP 截图返回值)。纯标准库实现(zlib+struct)，
    支持 Chrome 截屏产生的非隔行、8bit、灰度/RGB/RGBA PNG。索引色(PNG color type 3)
    需要 PLTE 表，Chrome 截屏不会产生，故不支持时返回 None。
    """
    if isinstance(data, str):
        try:
            data = base64.b64decode(data, validate=False)
        except Exception:
            return None
    try:
        if not data or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        idat = b""
        width = height = bitdepth = colortype = interlace = None
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            ctype = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            pos += 12 + length
            if ctype == b"IHDR":
                width, height, bitdepth, colortype, _comp, _filt, interlace = \
                    struct.unpack(">IIBBBBB", chunk)
            elif ctype == b"IDAT":
                idat += chunk
            elif ctype == b"IEND":
                break
        if not (width and height and idat) or bitdepth != 8 or interlace != 0:
            return None
        bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colortype)
        if bpp is None or colortype == 3:
            return None
        raw = zlib.decompress(idat)
        stride = width * bpp
        prev = bytearray(stride)
        dark = 0
        total = 0
        idx = 0
        for _y in range(height):
            if idx >= len(raw):
                return None
            ftype = raw[idx]
            idx += 1
            if idx + stride > len(raw):
                return None
            line = raw[idx:idx + stride]
            idx += stride
            recon = bytearray(stride)
            for i in range(stride):
                a = recon[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                if ftype == 0:
                    val = line[i]
                elif ftype == 1:                       # Sub
                    val = (line[i] + a) & 0xFF
                elif ftype == 2:                       # Up
                    val = (line[i] + b) & 0xFF
                elif ftype == 3:                       # Average
                    val = (line[i] + ((a + b) >> 1)) & 0xFF
                elif ftype == 4:                       # Paeth
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    val = (line[i] + pr) & 0xFF
                else:
                    return None
                recon[i] = val
            prev = recon
            # 逐像素统计近黑(取 RGB，忽略 alpha)
            for i in range(0, stride, bpp):
                r = recon[i]
                g = recon[i + 1] if bpp >= 3 else r
                b = recon[i + 2] if bpp >= 3 else r
                total += 1
                if r + g + b < threshold:
                    dark += 1
        return dark / total if total else None
    except Exception:
        return None
