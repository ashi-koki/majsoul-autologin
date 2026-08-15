#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
majsoul_cdp.py —— 纯标准库的 Chrome DevTools Protocol (CDP) 客户端。

用于「雀魂小苏菲」的自动登录：通过 Chrome 的 --remote-debugging-port 控制浏览器，
读取/操作页面 DOM（点击按钮、读取 localStorage、判断界面状态等）。

零第三方依赖：只用 socket / http.client / base64 / hashlib / struct / json / threading。

核心两部分：
  - CDPClient : 与某个 page target 的 WebSocket 通信（发命令、收响应/事件）。
  - 底层 _WebSocket : 手写 RFC6455 客户端（握手 + 帧编解码）。
"""

import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import threading
import time


# --------------------------------------------------------------------------- #
# WebSocket (RFC 6455) 客户端 —— 仅实现 CDP 所需的最小集合
# --------------------------------------------------------------------------- #

class WebSocketError(Exception):
    pass


class _WebSocket:
    """极简 WebSocket 客户端。客户端帧必须掩码，服务端帧不掩码。"""

    def __init__(self, url, timeout=15.0):
        self._sock = None
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._closed = False
        host, port, path = self._parse(url)
        self._connect(host, port)
        self._handshake(host, path, timeout)

    @staticmethod
    def _parse(url):
        # ws://host:port/path
        rest = url[len("ws://"):]
        if "/" in rest:
            hostport, path = rest.split("/", 1)
            path = "/" + path
        else:
            hostport, path = rest, "/"
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            port = int(port)
        else:
            host, port = hostport, 80
        return host, port, path

    def _connect(self, host, port):
        try:
            self._sock = socket.create_connection((host, port), timeout=15.0)
        except OSError as e:
            raise WebSocketError(f"连接 {host}:{port} 失败: {e}")

    def _handshake(self, host, path, timeout):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        self._sock.settimeout(timeout)
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手时连接被关闭")
            resp += chunk
            if len(resp) > 65536:
                raise WebSocketError("握手响应过大")
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise WebSocketError(f"握手失败: {status.decode('utf-8', 'replace')}")
        # 服务端可能把首帧与握手响应一并发来，把多余字节暂存
        self._buf = bytearray(rest)

    # -- 帧读写 --------------------------------------------------------------

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WebSocketError("连接中断")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text):
        payload = text.encode("utf-8")
        with self._send_lock:
            self._send_frame(payload, opcode=0x1)

    def _send_frame(self, payload, opcode):
        header = bytearray([0x80 | opcode])  # FIN=1
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_frame(self):
        """读取一帧，返回 (opcode, payload: bytes)。自动应答 ping。"""
        with self._recv_lock:
            while True:
                b1, b2 = self._recv_exact(2)
                opcode = b1 & 0x0F
                masked = bool(b2 & 0x80)
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(length)
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x9:  # ping -> pong
                    self._send_frame(payload, opcode=0xA)
                    continue
                if opcode == 0x8:  # close
                    self._closed = True
                    raise WebSocketError("连接已关闭")
                # 0x0 分片继续帧：CDP 消息通常单帧，这里简单拼接后续帧
                if opcode == 0x0:
                    continue
                return opcode, payload

    def close(self):
        if self._sock is not None:
            try:
                self._send_frame(b"", opcode=0x8)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# --------------------------------------------------------------------------- #
# CDP 客户端
# --------------------------------------------------------------------------- #

class CDPClient:
    """连接到一个 page target，提供 request/response 风格的 call()。"""

    def __init__(self, ws_url, timeout=20.0):
        self.ws = _WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}   # id -> [event, result]
        self._events = []    # 收到的无 id 事件（供调试）
        self._events_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def drain_events(self):
        """取出并清空当前累积的 CDP 事件（无 id 的消息），返回列表。"""
        with self._events_lock:
            out = self._events
            self._events = []
            return out

    def _read_loop(self):
        while True:
            try:
                opcode, payload = self.ws.recv_frame()
            except WebSocketError:
                break
            except Exception:
                break
            if opcode != 0x1:  # 只关心文本帧
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                ev = self._pending.pop(mid, None)
                if ev is not None:
                    ev[0] = msg
                    ev[1].set()
            else:
                with self._events_lock:
                    self._events.append(msg)

    def call(self, method, params=None, timeout=None):
        """发送命令并等待响应，返回响应 dict（已含 id/result/error）。"""
        timeout = timeout or self.timeout
        with self._lock:
            self._id += 1
            mid = self._id
            msg = {"id": mid, "method": method}
            if params is not None:
                msg["params"] = params
            ev = [None, threading.Event()]
            self._pending[mid] = ev
            try:
                self.ws.send_text(json.dumps(msg))
            except Exception as e:
                self._pending.pop(mid, None)
                raise WebSocketError(f"发送 {method} 失败: {e}")
        if not ev[1].wait(timeout):
            self._pending.pop(mid, None)
            raise TimeoutError(f"CDP 命令超时: {method}")
        resp = ev[0]
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"CDP 错误 {method}: {err.get('message')}")
        return resp

    # -- 常用封装 ------------------------------------------------------------

    def evaluate(self, expression, timeout=None, await_promise=False):
        """在页面里执行 JS，返回 JSON 可序列化的结果（或 None）。"""
        r = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
        )
        result = r.get("result", {}).get("result", {})
        if result.get("type") == "undefined":
            return None
        return result.get("value")

    def js(self, expression, timeout=None):
        """evaluate 的别名，忽略异常详情，直接返回 value（异常时返回 None）。"""
        try:
            return self.evaluate(expression, timeout=timeout)
        except Exception:
            return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 连接辅助：通过 HTTP 端点发现 page target
# --------------------------------------------------------------------------- #

def http_get_json(port, path, host="127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {path}")
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def list_targets(port, host="127.0.0.1"):
    """GET /json 返回目标列表。"""
    return http_get_json(port, "/json", host)


def find_page_target(port, url_substr=None, host="127.0.0.1", retries=30, delay=0.5):
    """等待并返回匹配的 page target 的 webSocketDebuggerUrl。

    url_substr: 页面 URL 需包含的子串（如 "maj-soul"），None 表示任意 page。
    """
    last = None
    for _ in range(retries):
        try:
            targets = list_targets(port, host)
            for t in targets:
                if t.get("type") != "page":
                    continue
                u = t.get("url", "")
                if url_substr and url_substr not in u:
                    continue
                if t.get("webSocketDebuggerUrl"):
                    return t
            last = targets
        except Exception as e:
            last = e
        time.sleep(delay)
    raise TimeoutError(f"未找到 page target (substr={url_substr!r}); 最后状态: {last!r}")


if __name__ == "__main__":
    # 自检：列出一个调试端口的 targets（用法: python majsoul_cdp.py 9229）
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9229
    print(json.dumps(list_targets(port), ensure_ascii=False, indent=2))
