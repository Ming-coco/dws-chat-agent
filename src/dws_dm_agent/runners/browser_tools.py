#!/usr/bin/env python3
"""只读/可交互的浏览器后端：按脚本打开页面、点击、输入、断言、截图。

设计约束（与 jira_tools.py / gitlab_tools.py 一致）：

- 只用标准库。不依赖 Selenium/Playwright，换一台机器拉下代码就能用；
  需要的只是机器上已有的 Edge/Chrome 及其自带的 CDP 调试协议。
- 一次调用跑完一整段脚本，不跨调用保留浏览器状态。这样没有隐藏状态，
  失败可复现，也不会有孤儿进程占着端口。
- 断言失败要如实报失败并给出实际拿到的内容，不能把「没找到元素」
  说成「页面没问题」。

协议：stdin 读一行 JSON {"tool": "browser_run", "args": {...}}
      stdout 写一行 JSON {"ok": true, ...} 或 {"ok": false, "error": "..."}
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_MS = 15_000
MAX_TIMEOUT_MS = 60_000
NAVIGATION_TIMEOUT_MS = 30_000
MAX_STEPS = 40
MAX_TEXT_CHARS = 4000
MAX_CONSOLE_ENTRIES = 20
BROWSER_START_TIMEOUT_S = 25.0

_ENV_KEYS = ("BROWSER_PATH", "BROWSER_HEADLESS", "BROWSER_TIMEOUT_SECONDS")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_FILE_VARS = ("DWS_BROWSER_ENV_FILE", "DWS_AGENT_ENV_FILE")


class BrowserError(Exception):
    """可直接展示给模型的失败原因。"""


# --------------------------------------------------------------------------
# 配置：环境变量优先，其次部署目录的 .env
# --------------------------------------------------------------------------


def _read_env_file(path: str) -> dict[str, str]:
    if not path:
        return {}
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY_RE.fullmatch(key) or key not in _ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


_ENV_CACHE: dict[str, dict[str, str]] = {}


def _setting(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    for variable in _ENV_FILE_VARS:
        env_path = (os.environ.get(variable) or "").strip()
        if not env_path:
            continue
        if env_path not in _ENV_CACHE:
            _ENV_CACHE[env_path] = _read_env_file(env_path)
        value = (_ENV_CACHE[env_path].get(name) or "").strip()
        if value:
            return value
    return ""


def _headless() -> bool:
    configured = _setting("BROWSER_HEADLESS").lower()
    if configured in ("0", "false", "no", "off"):
        return False
    return True


_CANDIDATES = {
    "Windows": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "Linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ],
}


def find_browser() -> str:
    """找可用的 Chromium 系浏览器。配置优先，其次各平台常见路径，最后看 PATH。"""
    configured = _setting("BROWSER_PATH")
    if configured:
        if Path(configured).is_file():
            return configured
        raise BrowserError(f"BROWSER_PATH 指向的文件不存在：{configured}")

    system = platform.system()
    for candidate in _CANDIDATES.get(system, []):
        if Path(candidate).is_file():
            return candidate

    for name in ("chrome", "chromium", "msedge", "google-chrome"):
        for directory in (os.environ.get("PATH") or "").split(os.pathsep):
            if not directory:
                continue
            for suffix in ("", ".exe"):
                candidate = Path(directory) / (name + suffix)
                if candidate.is_file():
                    return str(candidate)

    raise BrowserError(
        "找不到可用的浏览器（Edge / Chrome / Chromium）。"
        "可以装一个，或在部署目录 .env 里写 BROWSER_PATH=浏览器可执行文件的绝对路径。"
    )


# --------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC 6455 子集：文本帧 + 分片 + ping/pong）
# --------------------------------------------------------------------------


class WebSocket:
    """够用的 WebSocket 客户端：只处理 CDP 需要的文本帧。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        match = re.match(r"^ws://([^/:]+):(\d+)(/.*)$", url)
        if not match:
            raise BrowserError(f"不支持的调试地址：{url}")
        host, port, path = match.group(1), int(match.group(2)), match.group(3)

        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        self._buffer = b""
        while b"\r\n\r\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise BrowserError("浏览器调试端口拒绝连接")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise BrowserError("WebSocket 握手失败：" + head.split(b"\r\n", 1)[0].decode("latin-1"))

    # -- 发送 ------------------------------------------------------------
    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + opcode=1(text)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    # -- 接收 ------------------------------------------------------------
    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise BrowserError("浏览器连接已断开")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def receive(self) -> str:
        collected = b""
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(
                    byte ^ mask[index % 4] for index, byte in enumerate(payload)
                )

            if opcode == 0x8:  # close
                raise BrowserError("浏览器关闭了调试连接")
            if opcode == 0x9:  # ping
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            collected += payload
            if fin:
                return collected.decode("utf-8", "replace")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        else:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 浏览器会话
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Session:
    def __init__(self) -> None:
        self.executable = find_browser()
        self.port = _free_port()
        self.profile_dir = tempfile.mkdtemp(prefix="dws-browser-")
        self.process: subprocess.Popen[bytes] | None = None
        self.ws: WebSocket | None = None
        self._next_id = 0
        self.console: list[str] = []
        self.failed_requests: list[str] = []

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        arguments = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--disable-gpu",
            "--hide-scrollbars",
            "--mute-audio",
            "about:blank",
        ]
        if _headless():
            arguments.insert(1, "--headless=new")
        try:
            self.process = subprocess.Popen(
                arguments,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            raise BrowserError(f"浏览器启动失败：{error}") from error

        deadline = time.monotonic() + BROWSER_START_TIMEOUT_S
        target = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=1.5
                ) as response:
                    targets = json.loads(response.read().decode("utf-8", "replace"))
                for item in targets or []:
                    url = str(item.get("webSocketDebuggerUrl") or "")
                    if item.get("type") == "page" and url:
                        target = url
                        break
                if target:
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.15)
        if not target:
            raise BrowserError(
                f"浏览器在 {BROWSER_START_TIMEOUT_S:.0f} 秒内没有开放调试端口（端口 {self.port}）"
            )
        self.ws = WebSocket(target)
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")

    def stop(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=8)
            except Exception:  # noqa: BLE001 - 清理失败不影响结果
                try:
                    self.process.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.process = None
        _remove_tree(self.profile_dir)

    # -- CDP ------------------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.ws is None:
            raise BrowserError("浏览器会话没有建立")
        self._next_id += 1
        message_id = self._next_id
        self.ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        while True:
            raw = self.ws.receive()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    detail = message["error"]
                    raise BrowserError(
                        f"CDP {method} 失败：{detail.get('message', detail)}"
                    )
                return message.get("result") or {}
            self._collect_event(message)

    def _collect_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            kind = str(params.get("type") or "log")
            parts = []
            for arg in params.get("args") or []:
                if isinstance(arg, dict):
                    value = arg.get("value")
                    if value is None:
                        value = arg.get("description") or arg.get("type") or ""
                    parts.append(str(value))
            text = " ".join(part for part in parts if part).strip()
            if text:
                self.console.append(f"[{kind}] {text[:300]}")
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            text = str(
                detail.get("exception", {}).get("description")
                or detail.get("text")
                or ""
            )
            if text:
                self.console.append("[exception] " + text[:300])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if str(entry.get("level")) in ("error", "warning"):
                text = str(entry.get("text") or "")
                if text:
                    self.console.append(f"[{entry.get('level')}] {text[:300]}")
        elif method == "Network.loadingFailed":
            reason = str(params.get("errorText") or "失败")
            self.failed_requests.append(reason[:200])

    # -- 便捷操作 --------------------------------------------------------
    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = str(
                detail.get("exception", {}).get("description") or detail.get("text") or ""
            )
            raise BrowserError("页面脚本出错：" + text[:300])
        return (result.get("result") or {}).get("value")

    def wait_ready(self, timeout_ms: int = NAVIGATION_TIMEOUT_MS) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except BrowserError:
                pass
            time.sleep(0.1)
        raise BrowserError(f"页面在 {timeout_ms} 毫秒内没有加载完成")

    def navigate(self, url: str) -> str:
        result = self.call("Page.navigate", {"url": url})
        error = result.get("errorText")
        if error:
            raise BrowserError(f"打不开 {url}：{error}")
        self.wait_ready()
        return str(self.evaluate("document.title") or "")

    def element_center(self, selector: str, index: int = 0) -> dict[str, float]:
        expression = (
            "(() => {"
            f"  const nodes = document.querySelectorAll({json.dumps(selector)});"
            f"  const el = nodes[{index}];"
            "  if (!el) return null;"
            "  el.scrollIntoView({block: 'center'});"
            "  const r = el.getBoundingClientRect();"
            "  if (r.width === 0 && r.height === 0) return null;"
            "  return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
            "})()"
        )
        point = self.evaluate(expression)
        if not isinstance(point, dict):
            raise BrowserError(f"页面上找不到可点击的元素：{selector}")
        return {"x": float(point["x"]), "y": float(point["y"])}

    def click_point(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": kind,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1 if kind != "mouseMoved" else 0,
                },
            )
        time.sleep(0.05)


def _remove_tree(path: str) -> None:
    """删掉临时 profile 目录；Windows 上浏览器文件句柄可能还没释放，失败就跳过。"""

    def _on_error(function, target, info):  # noqa: ANN001 - shutil 回调签名
        try:
            os.chmod(target, 0o700)
            function(target)
        except OSError:
            pass

    import shutil

    shutil.rmtree(path, onerror=_on_error)


# --------------------------------------------------------------------------
# 步骤执行
# --------------------------------------------------------------------------


def _text_of(selector: str, index: int = 0) -> str:
    return (
        "(() => {"
        f"  const el = document.querySelectorAll({json.dumps(selector)})[{index}];"
        "  if (!el) return null;"
        "  return (el.innerText || el.textContent || '').trim();"
        "})()"
    )


def _find_by_text(pattern: str) -> str:
    """在可见元素里按文字找最内层匹配项，返回选择器可用的表达式。"""
    return (
        "(() => {"
        f"  const needle = {json.dumps(pattern)};"
        "  const all = Array.from(document.querySelectorAll('a,button,span,div,li,td,label,p'));"
        "  const hits = all.filter(el => {"
        "    const t = (el.innerText || el.textContent || '').trim();"
        "    if (!t.includes(needle)) return false;"
        "    const r = el.getBoundingClientRect();"
        "    if (r.width === 0 && r.height === 0) return false;"
        "    return true;"
        "  });"
        "  if (!hits.length) return null;"
        "  hits.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);"
        "  const el = hits[0];"
        "  el.scrollIntoView({block: 'center'});"
        "  const r = el.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()"
    )


def _step_timeout(step: dict[str, Any]) -> int:
    raw = step.get("timeout_ms")
    try:
        value = int(raw) if raw is not None else DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_MS
    return max(200, min(value, MAX_TIMEOUT_MS))


def run_step(session: Session, step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise BrowserError(f"第 {index} 步格式不对，应为对象")

    def label(name: str) -> dict[str, Any]:
        return {"step": index, "action": name}

    if "goto" in step:
        url = str(step["goto"])
        title = session.navigate(url)
        return {**label("goto"), "url": url, "title": title}

    if "click" in step:
        selector = str(step["click"])
        point = session.element_center(selector, int(step.get("index") or 0))
        session.click_point(point["x"], point["y"])
        session.wait_ready(_step_timeout(step))
        return {**label("click"), "selector": selector, "ok": True}

    if "click_text" in step:
        pattern = str(step["click_text"])
        point = session.evaluate(_find_by_text(pattern))
        if not isinstance(point, dict):
            raise BrowserError(f"页面上找不到文字包含「{pattern}」的可点击元素")
        session.click_point(float(point["x"]), float(point["y"]))
        session.wait_ready(_step_timeout(step))
        return {**label("click_text"), "text": pattern, "ok": True}

    if "type" in step:
        spec = step["type"]
        if not isinstance(spec, dict) or "selector" not in spec:
            raise BrowserError("type 需要 {selector, text}")
        selector = str(spec["selector"])
        text = str(spec.get("text") or "")
        session.evaluate(
            "(() => {"
            f"  const el = document.querySelector({json.dumps(selector)});"
            "  if (!el) return false;"
            "  el.focus();"
            "  return true;"
            "})()"
        )
        session.call("Input.insertText", {"text": text})
        if spec.get("enter"):
            for kind in ("keyDown", "keyUp"):
                session.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": kind,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                )
            session.wait_ready(_step_timeout(step))
        return {**label("type"), "selector": selector, "typed": len(text), "ok": True}

    if "wait" in step:
        spec = step["wait"]
        timeout = _step_timeout(step)
        if isinstance(spec, (int, float)):
            time.sleep(float(spec) / 1000)
            return {**label("wait"), "ms": spec, "ok": True}
        if isinstance(spec, dict) and "selector" in spec:
            selector = str(spec["selector"])
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                found = session.evaluate(
                    "!!document.querySelector(" + json.dumps(selector) + ")"
                )
                if found:
                    return {**label("wait"), "selector": selector, "ok": True}
                time.sleep(0.1)
            raise BrowserError(
                f"等了 {timeout} 毫秒，页面上仍然没有出现 {selector}"
            )
        if isinstance(spec, dict) and "text" in spec:
            needle = str(spec["text"])
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                body = str(session.evaluate("document.body ? document.body.innerText : ''") or "")
                if needle in body:
                    return {**label("wait"), "text": needle, "ok": True}
                time.sleep(0.15)
            raise BrowserError(f"等了 {timeout} 毫秒，页面文字里仍然没有「{needle}」")
        raise BrowserError("wait 需要 {selector} / {text} / 毫秒数")

    if "text" in step:
        selector = str(step["text"])
        session.evaluate("0")
        value = session.evaluate(_text_of(selector, int(step.get("index") or 0)))
        if value is None:
            raise BrowserError(f"页面上找不到元素：{selector}")
        text = str(value)[:MAX_TEXT_CHARS]
        return {**label("text"), "selector": selector, "text": text}

    if "expect_text" in step:
        spec = step["expect_text"]
        if not isinstance(spec, dict) or "contains" not in spec:
            raise BrowserError("expect_text 需要 {contains, selector?}")
        needle = str(spec["contains"])
        selector = spec.get("selector")
        if selector:
            raw = session.evaluate(_text_of(str(selector), int(spec.get("index") or 0)))
            if raw is None:
                raise BrowserError(f"断言失败：页面上找不到 {selector}")
            haystack = str(raw)
            scope = f"元素 {selector}"
        else:
            haystack = str(
                session.evaluate("document.body ? document.body.innerText : ''") or ""
            )
            scope = "整页"
        if needle not in haystack:
            preview = haystack.strip().replace("\n", " ")[:400]
            raise BrowserError(
                f"断言失败：{scope} 里没有「{needle}」。实际内容开头：{preview or '（空）'}"
            )
        return {**label("expect_text"), "contains": needle, "ok": True}

    if "eval" in step:
        value = session.evaluate(str(step["eval"]))
        return {**label("eval"), "value": value}

    if "screenshot" in step:
        target = str(step["screenshot"])
        result = session.call("Page.captureScreenshot", {"format": "png"})
        data = result.get("data")
        if not data:
            raise BrowserError("截图失败：浏览器没有返回图像数据")
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        size = path.stat().st_size
        return {
            **label("screenshot"),
            "path": str(path.resolve()),
            "bytes": size,
            "ok": True,
        }

    raise BrowserError(
        f"第 {index} 步不认识：{sorted(step)}。"
        "支持 goto / click / click_text / type / wait / text / expect_text / eval / screenshot"
    )


def run_script(steps: Any, url: Any = None) -> dict[str, Any]:
    if isinstance(steps, str):
        steps = [{"goto": steps}]
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        raise BrowserError("steps 应该是数组")
    if url and not steps:
        steps = [{"goto": str(url)}]
    elif url:
        steps = [{"goto": str(url)}, *steps]
    if len(steps) > MAX_STEPS:
        raise BrowserError(f"一次最多 {MAX_STEPS} 步，收到 {len(steps)} 步")
    if not steps:
        raise BrowserError("没有给任何步骤")

    session = Session()
    results: list[dict[str, Any]] = []
    try:
        session.start()
        for index, step in enumerate(steps):
            results.append(run_step(session, step, index))
        title = str(session.evaluate("document.title") or "")
        current = str(session.evaluate("location.href") or "")
        return {
            "ok": True,
            "title": title,
            "url": current,
            "steps": results,
            "console": session.console[-MAX_CONSOLE_ENTRIES:],
            "failed_requests": session.failed_requests[-MAX_CONSOLE_ENTRIES:],
        }
    except BrowserError as error:
        return {
            "ok": False,
            "error": str(error),
            "steps": results,
            "console": session.console[-MAX_CONSOLE_ENTRIES:],
            "failed_requests": session.failed_requests[-MAX_CONSOLE_ENTRIES:],
        }
    finally:
        session.stop()


def handle(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {"ok": False, "error": "请求格式不对，需要 JSON 对象"}
    tool = str(request.get("tool") or "").strip()
    args = request.get("args")
    if not isinstance(args, dict):
        args = {}
    if tool != "browser_run":
        return {"ok": False, "error": f"未知工具 {tool!r}"}
    try:
        return run_script(args.get("steps"), args.get("url"))
    except BrowserError as error:
        return {"ok": False, "error": str(error)}


def main() -> int:
    raw = sys.stdin.buffer.readline().decode("utf-8", "replace")
    try:
        request = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as error:
        payload = {"ok": False, "error": f"请求不是合法 JSON：{error}"}
    else:
        payload = handle(request)
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
