"""右下角通知:滑入动画、最多 2 条、新的挤掉最旧、底部一条细进度条倒计时到点自动消失。

用法:宿主 App 的 Screen CSS 里声明 `layers: base overlay;`,compose 末尾 `yield ToastHost()`,
然后 `self.query_one(ToastHost).show("消息", kind="ok")`。
"""
from __future__ import annotations

import time

from textual.containers import Container, Vertical
from textual.widgets import Static
from rich.text import Text

ACCENT = "#FB7299"
KIND_STYLE = {
    "ok": "bold #51CF66", "info": ACCENT, "warn": "bold #FFD43B", "err": "bold #FF6B6B",
}
KIND_BORDER = {
    "ok": "#51CF66", "info": ACCENT, "warn": "#FFD43B", "err": "#FF6B6B",
}


class Toast(Vertical):
    DEFAULT_CSS = """
    Toast {
        width: 46; height: auto; padding: 0 1; margin: 1 2 1 0;
        background: $panel; border: round #FB7299;
    }
    Toast .toast-msg { width: 100%; }
    Toast .toast-bar { width: 100%; height: 1; }
    """

    def __init__(self, message: str, *, kind: str = "info", timeout: float = 4.0):
        super().__init__()
        self._msg = message
        self._kind = kind
        self._timeout = max(0.5, timeout)
        self._end = 0.0
        self._timer = None
        self.styles.border = ("round", KIND_BORDER.get(kind, ACCENT))

    def compose(self):
        yield Static(Text(self._msg, style=KIND_STYLE.get(self._kind, ACCENT)), classes="toast-msg")
        yield Static("", classes="toast-bar")

    def on_mount(self) -> None:
        self.styles.opacity = 0.0
        self.styles.animate("opacity", value=1.0, duration=0.22)
        self._end = time.monotonic() + self._timeout
        self._timer = self.set_interval(0.1, self._tick)
        self._tick()

    def _tick(self) -> None:
        rem = self._end - time.monotonic()
        if rem <= 0:
            self.dismiss()
            return
        frac = rem / self._timeout
        bar = self.query_one(".toast-bar", Static)
        w = max(1, bar.size.width or 44)
        filled = max(0, int(round(w * frac)))
        color = KIND_BORDER.get(self._kind, ACCENT)
        bar.update(Text("─" * filled, style=color) + Text("─" * (w - filled), style="grey30"))

    def dismiss(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.styles.animate("opacity", value=0.0, duration=0.2,
                            on_complete=lambda: self.remove() if self.is_mounted else None)


class ToastHost(Container):
    """右下角浮层(overlay 层,dock 底部、空时高度为 0 不挡 Footer);最多承载 MAX 条 Toast。"""

    DEFAULT_CSS = """
    ToastHost {
        layer: overlay; dock: bottom; height: auto; width: 100%;
        align: right bottom; background: transparent;
    }
    ToastHost > #toast-stack { width: auto; height: auto; background: transparent; }
    """
    MAX = 2

    def compose(self):
        yield Vertical(id="toast-stack")

    def show(self, message: str, *, kind: str = "info", timeout: float = 4.0) -> None:
        stack = self.query_one("#toast-stack", Vertical)
        current = list(stack.query(Toast))
        for old in current[:max(0, len(current) - (self.MAX - 1))]:
            old.dismiss()
        stack.mount(Toast(message, kind=kind, timeout=timeout))
