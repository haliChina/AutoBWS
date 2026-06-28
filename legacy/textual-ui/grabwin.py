"""抢票窗口的 Textual GUI(独立窗口里跑)。

布局(自上而下整列):大倒计时/阶段横幅 → 全宽进度表(原地差量更新)→ 统计条
(发包/抢中/拥挤/风控/退避/网络异常 + 时间源 + 每账号代理)→ 事件日志(连续同条折叠 ×N);
重要事件右下角 toast 提示。
"""
from __future__ import annotations

from collections import deque

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, DataTable, Static, RichLog
from rich.text import Text
from rich.table import Table as RTable

from core import profiles
from net.proxy import resolve_pool
from core.api import BwsClient, ServerClock
from core.grabber import jobs_from_profile, ThreadedGrab
from core.lock import acquire_accounts, release_all
from utils.fmt import fmt_duration
from ui.toast import ToastHost

PINK = "#FB7299"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PHASE_STYLE = {
    "等待": "grey50", "蹲点": "cyan", "开抢": "yellow", "抢票中": "yellow",
    "退避": "magenta", "抢中": "bold green", "停止": "grey62", "截止": "grey62",
    "异常": "red", "已中止": "grey50",
}
ACTIVE = {"蹲点", "开抢", "退避", "抢票中"}
_COLS = [("账号/代理", "acct"), ("场次", "sess"), ("状态", "status"),
         ("尝试", "tries"), ("间隔", "intv"), ("最近返回", "ret")]


class GrabApp(App):
    TITLE = "AUTOBWS"
    SUB_TITLE = "抢票"
    CSS = """
    Screen { layers: base overlay; background: $surface; }
    #banner { height: 3; border: round #FB7299; content-align: center middle; }
    #prog { height: 1fr; border: round #FB7299; }
    #statbar { height: auto; border: round #888888; padding: 0 1; }
    #log { height: 6; border: round #888888; }
    DataTable > .datatable--header { color: #FB7299; text-style: bold; }
    """
    BINDINGS = [Binding("q", "quit", "关闭窗口"), Binding("ctrl+c", "quit", "关闭", show=False)]

    def __init__(self, args, profs):
        super().__init__()
        self.args = args
        self.profs = profs
        self.tg: ThreadedGrab | None = None
        self.clock_desc = ""
        self.proxy_status: dict[str, str] = {}
        self.phase_text = "正在初始化…"
        self._logq: deque = deque(maxlen=400)
        self._frame = 0
        self._order: list[str] = []
        self._prev: dict[str, tuple] = {}
        self._final: set = set()
        self._rows_built = False
        self._done_notified = False
        self._locks: list = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="banner")
        yield DataTable(id="prog", cursor_type="row", zebra_stripes=True)
        yield Static(id="statbar")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield ToastHost()
        yield Footer()

    def on_mount(self) -> None:
        self.theme = getattr(self.args, "theme", None) or "ansi-dark"
        self._tbl = self.query_one("#prog", DataTable)
        self._log = self.query_one("#log", RichLog)
        self._banner = self.query_one("#banner", Static)
        self._statbar = self.query_one("#statbar", Static)
        for label, key in _COLS:
            self._tbl.add_column(label, key=key)
        self._render_banner()
        self._render_statbar()
        self.run_worker(self._setup_and_run(), exclusive=True)
        self.set_interval(0.1, self._tick)

    def _render_banner(self) -> None:
        self._banner.update(Text(self.phase_text, style=f"bold {PINK}", justify="center"))

    def _render_statbar(self) -> None:
        meta = [f"账号 {len(self.profs)} · 场次 {sum(len(p.sessions) for p in self.profs)}"]
        if self.clock_desc:
            meta.append(self.clock_desc)
        g = RTable.grid(padding=(0, 1))
        g.add_column()
        if self.tg and self.tg.stat_totals().get("sent"):
            s = self.tg.stat_totals()
            line = Text.assemble(
                ("发包 ", "grey62"), (f"{s.get('sent', 0)}", "grey78"),
                ("  抢中 ", "grey62"), (f"{s.get('win', 0)}", "bold #51CF66"),
                ("  拥挤 ", "grey62"), (f"{s.get('relief', 0)}", "grey78"),
                ("  风控 ", "grey62"), (f"{s.get('risk', 0)}", "grey78"),
                ("  退避 ", "grey62"), (f"{s.get('throttle', 0)}", "grey78"),
                ("  网络异常 ", "grey62"), (f"{s.get('net', 0)}", "grey78"),
                ("    ·    " + "   ·   ".join(meta), "grey50"))
            g.add_row(line)
        else:
            g.add_row(Text("   ·   ".join(meta), style="grey70"))
        accts = [f"{p.name[:12]} → {self.proxy_status.get(p.name)}"
                 for p in self.profs if self.proxy_status.get(p.name)]
        if accts:
            g.add_row(Text("代理  " + "    ".join(accts), style="grey62"))
        self._statbar.update(g)

    def _set_phase(self, txt: str) -> None:
        self.phase_text = txt
        self._render_banner()

    def _notify(self, msg: str) -> None:
        self._logq.append(msg)

    def _toast(self, msg: str, kind: str = "info") -> None:
        try:
            self.query_one(ToastHost).show(msg, kind=kind)
        except Exception:
            pass

    def _maybe_toast(self, msg: str) -> None:
        if "抢中" in msg or "全部完成" in msg:
            self._toast(msg, "ok")
        elif "切换代理" in msg:
            self._toast(msg, "warn")
        elif "异常" in msg:
            self._toast(msg, "err")
        elif "失败" in msg or "跳过" in msg:
            self._toast(msg, "warn")

    async def _setup_and_run(self) -> None:
        ok, skipped, self._locks = acquire_accounts(self.profs)
        for name in skipped:
            self._notify(f"账号「{name}」已在其它窗口抢票,跳过")
        self.profs = ok
        if not self.profs:
            self._set_phase("所选账号都已在其它窗口抢票(按 q 关闭)")
            return

        # 抢票前不检测代理(避免卡顿);直接用配置里的代理,运行中由引擎按失败/测活切换。
        account_opts: dict[str, dict] = {}
        for p in self.profs:
            raws = resolve_pool(p.proxies)
            if raws:
                account_opts[p.name] = {"proxies": raws, "fallback_direct": getattr(p, "fallback_direct", True)}
                self.proxy_status[p.name] = f"{len(raws)} 个代理(运行中测活)"
            else:
                account_opts[p.name] = {"proxies": [None], "fallback_direct": True}
                self.proxy_status[p.name] = "直连"
        self._render_statbar()

        self._set_phase("校时中…")
        p0 = self.profs[0]
        sc = BwsClient(p0.cookies, p0.impersonate)
        clock = ServerClock(sc)
        await clock.sync()
        await sc.aclose()
        self.clock_desc = clock.describe()
        self._render_statbar()

        jobs = []
        for p in self.profs:
            jobs += jobs_from_profile(p)
        if not jobs:
            self._set_phase("所选配置没有可抢的场次(按 q 关闭)")
            return

        if self.args.dry_run:
            for j in jobs:
                self._notify(f"[dry-run] {j.account} reserve_id={j.sess['reserve_id']} ticket={j.sess['ticket_no']}")
            self._set_phase("dry-run 完成(按 q 关闭)")
            return
        if self.args.probe:
            self._set_phase("probe:各发一次…")
            for j in jobs:
                proxy = (account_opts.get(j.account, {}).get("proxies") or [None])[0]
                c = BwsClient(j.cookies, j.impersonate, proxy)
                try:
                    r = await c.reserve_do(j.sess["reserve_id"], j.sess["ticket_no"])
                    self._notify(f"[probe] {j.account} {j.sess['title'][:12]}: code={r.get('code')} {r.get('message')}")
                finally:
                    await c.aclose()
            self._set_phase("probe 完成(按 q 关闭)")
            return

        self.tg = ThreadedGrab(jobs, clock, account_opts=account_opts, notify=self._notify, refresh=True)
        self.tg.start()

    def _icon(self, p: dict, spin: str) -> str:
        if p["phase"] in ACTIVE:
            return spin
        if p["ok"]:
            return "✓"
        if p["phase"] in ("停止", "截止", "异常"):
            return "×"
        return "·"

    def _sig(self, p: dict, spin: str) -> tuple:
        plabel = p.get("proxy", "直连")
        acct = p["account"][:12] + ("" if plabel == "直连" else f" 🌐{plabel[:14]}")
        sess = f"{p['date'][4:]} {p['title'][:16]}"
        status = f"{self._icon(p, spin)} {p['phase']}"
        tries = str(p["attempts"] or "—")
        intv = f"{p['interval']}ms" if p["interval"] else "—"
        ret = p["result"] or (f"{p['code']} {str(p['msg'])[:20]}" if p["code"] is not None else "—")
        return (acct, sess, status, tries, intv, ret)

    def _cells(self, p: dict, sig: tuple) -> list:
        st = PHASE_STYLE.get(p["phase"], "white")
        return [Text(sig[0]), Text(sig[1]), Text(sig[2], style=st),
                Text(sig[3], justify="right"), Text(sig[4], justify="right"),
                Text(sig[5], style=st if p["result"] else "grey62")]

    def _drain_log(self) -> None:
        pending, n = None, 0
        while self._logq:
            m = self._logq.popleft()
            if m == pending:
                n += 1
            else:
                if pending is not None:
                    self._log.write(pending + (f"  ×{n}" if n > 1 else ""))
                    self._maybe_toast(pending)
                pending, n = m, 1
        if pending is not None:
            self._log.write(pending + (f"  ×{n}" if n > 1 else ""))
            self._maybe_toast(pending)

    def _tick(self) -> None:
        self._frame += 1
        spin = SPIN[self._frame % len(SPIN)]
        self._drain_log()

        if not self.tg:
            return
        prog = self.tg.progress
        if not self._rows_built:
            self._order = sorted(prog.keys(), key=lambda k: (prog[k]["account"], prog[k]["date"], prog[k]["reserve_id"] or 0))
            for k in self._order:
                sig = self._sig(prog[k], spin)
                self._tbl.add_row(*self._cells(prog[k], sig), key=k)
                self._prev[k] = sig
            self._rows_built = True
        else:
            for k in self._order:
                if k in self._final:
                    continue
                p = prog.get(k)
                if p is None:
                    continue
                sig = self._sig(p, spin)
                prev = self._prev.get(k)
                if sig != prev:
                    cells = self._cells(p, sig)
                    for i, (label, ckey) in enumerate(_COLS):
                        if not prev or prev[i] != sig[i]:
                            try:
                                self._tbl.update_cell(k, ckey, cells[i])
                            except Exception:
                                pass
                    self._prev[k] = sig
                if p["done"]:
                    self._final.add(k)

        rem = self.tg.earliest_target - self.tg.clock.now_ms()
        won = sum(1 for p in prog.values() if p.get("ok"))
        n = len(prog)
        if self.tg.all_done:
            if not self._done_notified:
                self._notify(f"全部完成,抢中 {won}/{n}(按 q 关闭)")
                self._done_notified = True
            self._set_phase(f"完成 · 抢中 {won}/{n}")
        elif rem > 0:
            self._set_phase(f"距最早开抢  {fmt_duration(rem)}")
        else:
            self._set_phase(f"开抢进行中 · 抢中 {won}/{n}")
        self._render_statbar()

    def _cleanup(self) -> None:
        try:
            if self.tg:
                self.tg.join(timeout=2)
                self.tg.close()
        except Exception:
            pass
        try:
            release_all(self._locks)
        except Exception:
            pass

    def action_quit(self) -> None:
        if self.tg:
            self.tg.stop()
        self._cleanup()
        self.exit()


async def run_grab_app(args) -> None:
    profs = profiles.load_all(args.profiles or [])
    if not profs:
        print(f"找不到配置:{args.profiles}")
        try:
            input("按回车关闭…")
        except Exception:
            pass
        return
    await GrabApp(args, profs).run_async()
