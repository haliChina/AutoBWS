"""控制台 GUI(Textual)—— 把 CLI 当 GUI 操作。

全屏、列表勾选、详情面板、模态确认、实时自动刷新;开抢/新建各开独立窗口,
Hub 不阻塞;profiles 目录变化自动刷新。
"""
from __future__ import annotations

import os
import subprocess
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, Center, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, DataTable, Static, Button, Label
from rich.text import Text
from rich.table import Table as RTable
from rich.panel import Panel
from rich import box

from core import profiles
from net.proxy import proxy_label, resolve_pool
from net import proxycheck
from utils.fmt import fmt_ts, fmt_duration
from paths import ROOT
from ui.toast import ToastHost

PINK = "#FB7299"


def _spawn(extra_args: list[str]) -> bool:
    cmd = [sys.executable, str(ROOT / "main.py"), *extra_args]
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(cmd, cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE)
            return True
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["xterm", "-e"]):
            try:
                subprocess.Popen(term + cmd, cwd=str(ROOT))
                return True
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return False


def _proxy_cell(p) -> str:
    if not p.proxies:
        return "直连"
    return f"{len(p.proxies)} 个"


class ConfirmScreen(ModalScreen[bool]):
    """模态确认框。"""
    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 56; height: auto; border: round #FB7299; background: $panel; padding: 1 2; }
    #msg { width: 100%; content-align: center middle; padding: 1 0; }
    #btns { width: 100%; height: auto; align: center middle; }
    Button { margin: 0 2; }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.message, id="msg")
            with Horizontal(id="btns"):
                yield Button("确认", variant="error", id="yes")
                yield Button("取消", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class HubApp(App):
    TITLE = "AUTOBWS"
    SUB_TITLE = "bw乐园抢票助手"
    CSS = """
    Screen { layers: base overlay; background: $surface; }
    #body { height: 1fr; }
    #ptable { width: 3fr; border: round #FB7299; }
    #details { width: 2fr; border: round #888888; padding: 0 1; }
    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: #FB7299; color: $text; }
    DataTable > .datatable--header { color: #FB7299; text-style: bold; }
    """
    BINDINGS = [
        Binding("space", "toggle", "勾选"),
        Binding("enter,g", "grab", "开抢(勾选)", priority=True),
        Binding("n", "new", "新建"),
        Binding("e", "edit", "编辑"),
        Binding("p", "proxy_check", "代理检测"),
        Binding("d", "delete", "删除"),
        Binding("r", "refresh", "刷新"),
        Binding("q", "quit", "退出"),
        Binding("up,k", "cursor_up", "上", show=False),
        Binding("down,j", "cursor_down", "下", show=False),
    ]

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.checked: set[str] = set()
        self.names: list[str] = []
        self._profs: dict = {}
        self._sig = None
        self._checking: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield DataTable(id="ptable", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="details"):
                yield Static(id="details-inner")
        yield ToastHost()
        yield Footer()

    def _toast(self, msg: str, kind: str = "info") -> None:
        try:
            self.query_one(ToastHost).show(msg, kind=kind)
        except Exception:
            pass

    def on_mount(self) -> None:
        self.theme = getattr(self.args, "theme", None) or "ansi-dark"
        t = self.query_one("#ptable", DataTable)
        t.add_columns("✓", "配置名", "账号", "代理", "场次", "延迟")
        self.reload()
        self.set_interval(1.0, self._auto_reload)

    def _dir_signature(self):
        try:
            return tuple(sorted((n, os.path.getmtime(profiles.PROFILES_DIR / f"{n}.json"))
                                for n in profiles.list_profiles()))
        except Exception:
            return tuple(profiles.list_profiles())

    def _auto_reload(self) -> None:
        if self._checking:
            return
        sig = self._dir_signature()
        if sig != self._sig:
            self.reload()

    def reload(self) -> None:
        self._sig = self._dir_signature()
        t = self.query_one("#ptable", DataTable)
        prev = t.cursor_row
        t.clear()
        self.names = profiles.list_profiles()
        self._profs = {n: profiles.load(n) for n in self.names}
        self.checked &= set(self.names)
        for n in self.names:
            p = self._profs[n]
            mark = Text("✓", style=f"bold {PINK}") if n in self.checked else Text(" ")
            t.add_row(mark, Text(n, style="bold"),
                      f"{p.uname or '?'} {p.uid}", _proxy_cell(p),
                      str(len(p.sessions)), f"{p.base_interval}ms/+{p.offset}ms", key=n)
        if self.names:
            t.move_cursor(row=min(prev, len(self.names) - 1))
        self._update_details()

    def _cursor_name(self) -> str | None:
        t = self.query_one("#ptable", DataTable)
        if not self.names:
            return None
        i = t.cursor_row
        return self.names[i] if 0 <= i < len(self.names) else None

    def _update_details(self) -> None:
        d = self.query_one("#details-inner", Static)
        name = self._cursor_name()
        if not name:
            d.update(Panel("[dim]还没有配置。按 [b]n[/] 新建。[/]", title="详情", border_style="grey50", box=box.ROUNDED))
            return
        p = self._profs[name]
        g = RTable.grid(padding=(0, 1))
        g.add_column(justify="right", style="grey62")
        g.add_column()
        g.add_row("账号", f"[green]{p.uname}[/] [grey62]{p.uid}[/]")
        g.add_row("指纹", p.impersonate)
        g.add_row("延迟", f"基础 {p.base_interval}ms · 提前 {p.offset}ms")
        if p.proxies:
            try:
                n = len(resolve_pool(p.proxies))
            except Exception:
                n = len(p.proxies)
            g.add_row("代理池", f"{n} 个 · [grey62]失效转直连: {'是' if p.fallback_direct else '否'}[/]")
        else:
            g.add_row("代理", "直连")
        sess = RTable(box=box.SIMPLE, expand=True)
        sess.add_column("日期", style="grey62")
        sess.add_column("场次")
        sess.add_column("开抢", style="grey62")
        for s in p.sessions[:12]:
            sess.add_row(s.get("date", "")[4:], (s.get("title", "") or "")[:18], fmt_ts(s.get("begin", 0))[5:16])
        if len(p.sessions) > 12:
            sess.add_row("…", f"其余 {len(p.sessions) - 12} 个", "")
        from rich.console import Group
        chk = " [b #FB7299]✓ 已勾选(将开抢)[/]" if name in self.checked else ""
        d.update(Panel(Group(g, Text(""), sess), title=f"[b #FB7299]{name}[/]{chk}",
                       border_style=PINK, box=box.ROUNDED))

    def on_data_table_row_highlighted(self, event) -> None:
        self._update_details()

    def action_cursor_up(self) -> None:
        self.query_one("#ptable", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#ptable", DataTable).action_cursor_down()

    def action_toggle(self) -> None:
        name = self._cursor_name()
        if not name:
            return
        t = self.query_one("#ptable", DataTable)
        row = t.cursor_row
        if name in self.checked:
            self.checked.discard(name)
            t.update_cell_at(Coordinate(row, 0), Text(" "))
        else:
            self.checked.add(name)
            t.update_cell_at(Coordinate(row, 0), Text("✓", style=f"bold {PINK}"))
        self._update_details()

    def action_grab(self) -> None:
        targets = [n for n in self.names if n in self.checked] or ([self._cursor_name()] if self._cursor_name() else [])
        if not targets:
            self._toast("没有可开抢的配置", "warn")
            return
        if _spawn(["--grab-worker"] + sum([["--profile", n] for n in targets], [])
                  + (["--probe"] if self.args.probe else []) + (["--dry-run"] if self.args.dry_run else [])):
            self._toast(f"已在新窗口开抢:{' · '.join(targets)}", "ok")
        else:
            self._toast("无法开新窗口", "err")

    def action_new(self) -> None:
        if _spawn(["--new-profile"]):
            self._toast("已在新窗口新建配置,完成后自动出现在列表", "ok")
        else:
            self._toast("无法开新窗口", "err")

    def action_edit(self) -> None:
        name = self._cursor_name()
        if not name:
            return
        if _spawn(["--edit-profile", name]):
            self._toast(f"已在新窗口编辑「{name}」,保存后自动刷新", "ok")
        else:
            self._toast("无法开新窗口", "err")

    def action_delete(self) -> None:
        name = self._cursor_name()
        if not name:
            return

        def done(ok: bool | None) -> None:
            if ok:
                profiles.delete(name)
                self.checked.discard(name)
                self.reload()
                self._toast(f"已删除「{name}」", "ok")
        self.push_screen(ConfirmScreen(f"删除配置「{name}」?"), done)

    def action_refresh(self) -> None:
        self.reload()
        self._toast("已刷新", "info")

    def action_proxy_check(self) -> None:
        name = self._cursor_name()
        if not name:
            return
        p = self._profs.get(name)
        if not p or not p.proxies:
            self._toast(f"「{name}」没有配置代理", "warn")
            return
        if self._checking:
            self._toast("已有代理检测在进行,请稍候", "warn")
            return
        self.run_worker(self._proxy_check(name), exclusive=True, group="pxcheck")

    async def _proxy_check(self, name: str) -> None:
        p = self._profs.get(name)
        if not p:
            return
        raws = resolve_pool(p.proxies)
        if not raws:
            self._toast("解析不到有效代理", "warn")
            return
        self._checking = name
        total = len(raws)
        d = self.query_one("#details-inner", Static)

        def render(done, last=""):
            bar = "█" * int(20 * done / total) + "░" * (20 - int(20 * done / total))
            body = Text.assemble(
                (f"检测「{name}」的代理\n", f"bold {PINK}"),
                (f"{bar}  {done}/{total}\n\n", "grey78"),
                ("超时 3s · 多并发\n", "grey50"),
                (last, "grey62"))
            d.update(Panel(body, title="代理检测", border_style=PINK, box=box.ROUNDED))

        render(0)
        self._toast(f"开始检测「{name}」{total} 个代理", "info")

        def on_prog(done, tot, res):
            lbl = proxy_label(res.get("norm")) if res.get("norm") else res.get("raw", "")
            tag = f"✓ {res['latency_ms']}ms" if res["ok"] else f"✗ {res['error']}"
            render(done, f"最近:{lbl}  {tag}")

        res = await proxycheck.evaluate(raws, p.impersonate, timeout=3.0, on_progress=on_prog)
        ranked = res["ranked"]
        self._checking = None
        if ranked:
            p.proxies = list(ranked)
            profiles.save(p)
            self.reload()
            self._toast(f"「{name}」可用 {len(ranked)}/{total},已过滤保存", "ok")
        else:
            self.reload()
            self._toast(f"「{name}」{total} 个代理都不可用(保留原配置)", "warn")


def run_hub(args) -> None:
    HubApp(args).run()
