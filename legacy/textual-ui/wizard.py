"""新建/编辑配置的多步向导(Textual):模拟设备 → 代理 → 登录 → 票号绑定 → 选择场次 → 抢票设置。

左侧步骤进度可点击;←/→ 或 Ctrl+←/→ 或按钮切换。新建按步骤逐步完成;
编辑可在 1~6 间任意跳转(仍受前置条件约束:未登录不能到绑定/选场次)。
登录步:已登录则显示当前账号并提供「重新登录/切换账号」。登录成功即落盘。右下角 toast 提示。
"""
from __future__ import annotations

import re

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Header, Footer, Static, Input, RadioSet, RadioButton,
                             Checkbox, DataTable, Button, OptionList)
from textual.widgets.option_list import Option
from textual.coordinate import Coordinate
from rich.text import Text
from rich.table import Table as RTable

from core import profiles, login
from core.profiles import Profile, session_snapshot
from core.api import BwsClient, ID_TYPES
from core.grabber import collect_sessions, selectable
from net.http import IMPERSONATE_CHOICES, new_async_session
from net.proxy import resolve_pool
from utils.fmt import fmt_ts
from ui.toast import ToastHost

PINK = "#FB7299"
STEPS = ["模拟设备", "代理", "登录", "票号绑定", "选择场次", "抢票设置"]
LOGIN_STATE = {"waiting": "等待扫码…", "scanned": "已扫描,请在手机点击确认…",
               "expired": "二维码已失效,按 r 重新生成", "error": "登录失败,按 r 重试",
               "timeout": "二维码超时,按 r 重新生成"}


def _pinyin_key(s: str) -> str:
    try:
        from pypinyin import lazy_pinyin, Style
        full = "".join(lazy_pinyin(s))
        first = "".join(lazy_pinyin(s, style=Style.FIRST_LETTER))
        return f"{s}\n{full}\n{first}".lower()
    except Exception:
        return s.lower()


class WizardApp(App):
    TITLE = "AUTOBWS"
    SUB_TITLE = "配置向导"
    CSS = """
    Screen { layers: base overlay; background: $surface; }
    #navbar { height: 1; padding: 0 2; }
    #navbar Button { height: 1; min-height: 1; border: none; margin-right: 2; }
    #navtitle { width: 1fr; height: 1; content-align: left middle; color: $text-muted; }
    #frame { height: 1fr; }
    #steps { width: 20; height: 1fr; border-right: solid #FB7299; }
    #stepbody { width: 1fr; padding: 1 1; }
    #hint { height: 1; color: $text-muted; padding: 0 2; }
    .step Static { margin-bottom: 1; }
    #login-row { height: auto; }
    #qr-area { width: auto; color: white; margin-right: 3; }
    #login-side { width: 1fr; }
    DataTable { height: 1fr; }
    Input, RadioSet, Checkbox { margin-bottom: 1; }
    #bind-form Input { margin-bottom: 0; }
    #bind-form RadioSet { margin-bottom: 0; }
    #bind-form Button { margin-top: 1; }
    """
    BINDINGS = [
        Binding("ctrl+right", "next", "下一步", priority=True),
        Binding("ctrl+left", "back", "上一步", priority=True),
        Binding("right", "next", "下一步", show=False),
        Binding("left", "back", "上一步", show=False),
        Binding("r", "retry", "重试", show=False),
        Binding("space", "toggle_session", "勾选", show=False),
        Binding("ctrl+a", "select_all", "全选", show=False, priority=True),
        Binding("q", "quit", "关闭"),
        Binding("ctrl+c", "quit", "关闭", show=False),
    ]

    def __init__(self, args, existing: Profile | None = None):
        super().__init__()
        self.args = args
        self.editing = existing is not None
        self.draft = existing or Profile(name="新配置")
        self._orig_name = existing.name if existing else None
        self._saved_name = None
        self.step = 0
        self._max_step = 0
        self._logged_in = False
        self._bound_ok = False
        self._force_relogin = False
        self._login_url = None
        self._info = None
        self.sessions_opts: list[dict] = []
        self.selected: set = set()
        self._sessions_loaded = False
        self._done = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="navbar"):
            yield Static(id="navtitle")
            yield Button("← 上一步", id="btn-back", variant="default")
            yield Button("下一步 →", id="btn-next", variant="primary")
        with Horizontal(id="frame"):
            yield OptionList(id="steps")
            yield VerticalScroll(id="stepbody")
        yield Static(id="hint")
        yield ToastHost()
        yield Footer()

    def on_mount(self) -> None:
        self.theme = getattr(self.args, "theme", None) or "ansi-dark"
        self._last_size = tuple(self.size)
        self.set_interval(0.4, self._watch_resize)
        self._show_step(0)
        if self.editing:
            self.run_worker(self._preflight_edit(), group="net")

    def _watch_resize(self) -> None:
        sz = tuple(self.size)
        if sz != self._last_size:
            self._last_size = sz
            self._rerender_qr()

    # ---------- helpers ----------
    def _safe_update(self, selector: str, renderable) -> None:
        try:
            self.query_one(selector, Static).update(renderable)
        except Exception:
            pass

    def _toast(self, msg: str, kind: str = "info") -> None:
        try:
            self.query_one(ToastHost).show(msg, kind=kind)
        except Exception:
            pass

    def _qr_fit(self) -> tuple[int, int]:
        """登录步可用空间(扣掉右侧信息列和精简后的上下框架),供二维码按窗口动态适配。"""
        try:
            body = self.query_one("#stepbody", VerticalScroll)
            cols = body.size.width - 32          # 给右侧信息列留位
            rows = body.size.height - 2          # 标题行
        except Exception:
            cols, rows = self.size.width - 34, self.size.height - 6
        return (max(21, cols), max(11, rows))

    def _rerender_qr(self) -> None:
        if self.step == 2 and self._login_url:
            self._safe_update("#qr-area",
                              Text(login.render_qr(self._login_url, compact=True, fit=self._qr_fit()), no_wrap=True))

    def on_resize(self, event) -> None:
        self._rerender_qr()

    # ---------- sidebar / navigation ----------
    def _reached(self, i: int) -> bool:
        if i <= 1:
            return True
        if i == 2:
            return self._logged_in
        if i == 3:
            return self._bound_ok
        return self._bound_ok and i <= self._max_step

    def _render_sidebar(self) -> None:
        ol = self.query_one("#steps", OptionList)
        ol.clear_options()
        for i, label in enumerate(STEPS):
            if i == self.step:
                mark, st = "▶", f"bold {PINK}"
            elif self._reached(i):
                mark, st = "✓", "#51CF66"
            else:
                mark, st = "○", "grey50"
            ol.add_option(Option(Text(f" {mark} {i + 1}. {label}", style=st), id=f"step-{i}"))
        try:
            ol.highlighted = self.step
        except Exception:
            pass

    def _show_step(self, idx: int) -> None:
        self.step = idx
        self._max_step = max(self._max_step, idx)
        self.query_one("#steps").display = (idx != 2)   # 登录步隐藏侧栏,给二维码让出整行宽度
        self._render_sidebar()
        self.query_one("#btn-back").disabled = (idx == 0)
        self.query_one("#btn-next").label = "完成 ✓" if idx == len(STEPS) - 1 else "下一步 →"
        self._safe_update("#navtitle", Text(f"步骤 {idx + 1}/{len(STEPS)} · {STEPS[idx]}", style="grey70"))
        self._set_hint(idx)
        self.run_worker(self._mount_step(idx), exclusive=True, group="mount")

    async def _mount_step(self, idx: int) -> None:
        body = self.query_one("#stepbody", VerticalScroll)
        await body.remove_children()
        w = self._build(idx)
        await body.mount(w)                              # 等挂载完成,_enter 里 query 才找得到控件
        w.styles.opacity = 0.0
        w.styles.animate("opacity", value=1.0, duration=0.25)
        if self.step == idx:                            # 期间没再切步才执行
            self._enter(idx)

    def _set_hint(self, idx: int) -> None:
        tips = {
            2: "用 App 扫码;扫不出可复制下方链接。已登录可点「重新登录」换号。",
            4: "↑↓ 移动 · 空格 勾选 · Ctrl+A 全选 · 顶部可搜索(拼音)。",
        }
        nav = ("点左侧步骤任意跳转 · " if self.editing else "") + "←/→ 切换 · q 关闭"
        base = f"步骤 {idx + 1}/{len(STEPS)} · {nav}"
        extra = tips.get(idx)
        self.query_one("#hint", Static).update(base + (f"   |   {extra}" if extra else ""))

    def _can_enter(self, idx: int) -> str | None:
        if idx >= 3 and not self._logged_in:
            return "需先在「登录」步骤完成登录"
        if idx >= 4 and not self._bound_ok:
            return "需先完成「票号绑定」"
        return None

    def _jump_to(self, idx: int) -> None:
        if self._done or idx == self.step:
            return
        block = self._can_enter(idx)
        if not block and not self.editing and idx > self._max_step:
            block = "请按步骤逐步完成"
        if block:
            self._toast(block, "warn")
            self._render_sidebar()
            return
        self._capture(self.step)
        self._show_step(idx)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "steps":
            self._jump_to(event.option_index)

    def action_next(self) -> None:
        if self._done:
            return
        self._capture(self.step)
        if self.step >= len(STEPS) - 1:
            self._finish()
            return
        block = self._can_enter(self.step + 1)
        if block:
            self._toast(block, "warn")
            return
        self._show_step(self.step + 1)

    def action_back(self) -> None:
        if self._done or self.step == 0:
            return
        self._capture(self.step)
        self._show_step(self.step - 1)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-next":
            self.action_next()
        elif bid == "btn-back":
            self.action_back()
        elif bid == "bind-submit":
            self.run_worker(self._submit_bind(), exclusive=True, group="net")
        elif bid == "relogin":
            self._force_relogin = True
            self._safe_update("#login-status", Text("正在生成新二维码…", style="grey62"))
            self.run_worker(self._do_login(), exclusive=True, group="net")

    # ---------- step bodies ----------
    def _build(self, idx: int):
        return [self._b_device, self._b_proxy, self._b_login,
                self._b_bind, self._b_sessions, self._b_settings][idx]()

    def _b_device(self):
        cur = self.draft.impersonate
        return Vertical(
            Static(Text("① 选择移动端指纹", style="bold")),
            Static(Text("所有请求都模拟这个设备(新版 iOS Safari / 安卓 Chrome),UA 自动匹配。", style="grey62")),
            RadioSet(*[RadioButton(c, value=(c == cur)) for c in IMPERSONATE_CHOICES], id="dev-radio"),
            classes="step")

    def _b_proxy(self):
        init = self.draft.proxies or []
        try:
            total0 = len(resolve_pool(init))
        except Exception:
            total0 = len(init)
        if total0:
            status0 = f"已有 {total0} 个代理 · 留空=保持不变,输入则替换,输入「-」清空(检测/过滤在主控台做)"
            ph = f"已有 {total0} 个 · 留空不改"
        else:
            status0 = "未填 = 直连"
            ph = "留空 = 直连"
        return Vertical(
            Static(Text("② 代理池(仅抢票阶段使用)", style="bold")),
            Static(Text("逗号/空格分隔;支持 socks5:// http:// ip:port user:pass@host:port;可填 @proxies.txt。", style="grey62")),
            Input(value="", placeholder=ph, id="px-input"),     # 不回显已有代理,避免把整串代理打到输入框
            Checkbox("代理都失效时转直连", value=self.draft.fallback_direct, id="px-fb"),
            Static(Text(status0, style="grey62"), id="px-status"),
            classes="step")

    def _b_login(self):
        if self._logged_in and self._info and not self._force_relogin:
            i = self._info
            extra = " · 大会员" if i.get("is_vip") else ""
            status = Text(f"当前账号:{i.get('uname','?')}(uid {i.get('uid','?')} · Lv.{i.get('level',0)}{extra})\n"
                          f"换号请点下方「重新登录 / 切换账号」", style="#51CF66")
        else:
            status = Text("正在准备登录环境…", style="grey62")
        return Vertical(
            Static(Text("③ 扫码登录", style="bold")),
            Horizontal(
                Static("", id="qr-area"),
                Vertical(
                    Static(status, id="login-status"),
                    Static("", id="qr-url"),
                    Button("重新登录 / 切换账号", id="relogin", variant="default"),
                    id="login-side"),
                id="login-row"),
            classes="step")

    def _b_bind(self):
        status = (Text("已绑定门票实名信息(→ 继续)", style="#51CF66") if self._bound_ok
                  else Text("检查绑定状态…", style="grey62"))
        return Vertical(
            Static(Text("④ 票号绑定(实名)", style="bold")),
            Static(status, id="bind-status"),
            Vertical(id="bind-form"),
            classes="step")

    def _b_sessions(self):
        t = DataTable(id="sess-table", cursor_type="row", zebra_stripes=True)
        t.add_columns("✓", "类型", "日期", "场次 / 商品", "开抢")
        return Vertical(
            Static(Text("⑤ 选择要抢的场次", style="bold")),
            Input(placeholder="搜索(中文/拼音/首字母)", id="sess-filter"),
            Static(Text("加载场次中…", style="grey62"), id="sess-status"),
            t, classes="step")

    def _b_settings(self):
        return Vertical(
            Static(Text("⑥ 抢票设置", style="bold")),
            Static(Text("基础发包间隔(ms)", style="grey62")),
            Input(value=str(self.draft.base_interval), id="set-base"),
            Static(Text("提前发包(ms,>0 提前)", style="grey62")),
            Input(value=str(self.draft.offset), id="set-offset"),
            Static(Text("配置名", style="grey62")),
            Input(value=self.draft.name, id="set-name"),
            classes="step")

    # ---------- enter (idempotent & guarded) ----------
    def _enter(self, idx: int) -> None:
        if idx == 2:
            if self._logged_in and self._info and not self._force_relogin:
                self._show_account()
            else:
                self.run_worker(self._do_login(), exclusive=True, group="net")
        elif idx == 3:
            if self._bound_ok:
                self._safe_update("#bind-status", Text("已绑定门票实名信息(→ 继续)", style="#51CF66"))
            else:
                self.run_worker(self._do_bind_check(), exclusive=True, group="net")
        elif idx == 4:
            if self._sessions_loaded:
                self._fill_sessions(self.sessions_opts)
                self._update_sess_count()
            else:
                self.run_worker(self._do_load_sessions(), exclusive=True, group="net")

    def _show_account(self) -> None:
        i = self._info or {}
        extra = " · 大会员" if i.get("is_vip") else ""
        self._login_url = None
        self._safe_update("#qr-area", Text(""))
        self._safe_update("#qr-url", Text(""))
        self._safe_update("#login-status",
                          Text(f"当前账号:{i.get('uname','?')}(uid {i.get('uid','?')} · Lv.{i.get('level',0)}{extra})\n"
                               f"换号请点下方「重新登录 / 切换账号」", style="#51CF66"))

    # ---------- capture ----------
    def _capture(self, idx: int) -> None:
        try:
            if idx == 0:
                rs = self.query_one("#dev-radio", RadioSet)
                if rs.pressed_index is not None and rs.pressed_index >= 0:
                    self.draft.impersonate = IMPERSONATE_CHOICES[rs.pressed_index]
            elif idx == 1:
                raw = self.query_one("#px-input", Input).value.strip()
                if raw in ("-", "清空", "无"):
                    self.draft.proxies = []
                elif raw:
                    self.draft.proxies = [x for x in re.split(r"[,，\s]+", raw) if x]
                # 留空:保持原有代理不变
                self.draft.fallback_direct = self.query_one("#px-fb", Checkbox).value
            elif idx == 4:
                if self._sessions_loaded:
                    chosen = [o for o in self.sessions_opts
                              if o.get("reserve_id") in self.selected and selectable(o)]
                    self.draft.sessions = [session_snapshot(o) for o in chosen]
            elif idx == 5:
                for wid, attr in (("#set-base", "base_interval"), ("#set-offset", "offset")):
                    v = self.query_one(wid, Input).value.strip()
                    if v:
                        try:
                            setattr(self.draft, attr, int(v))
                        except ValueError:
                            pass
                nm = self.query_one("#set-name", Input).value.strip()
                if nm:
                    self.draft.name = nm
        except Exception:
            pass

    # ---------- workers (DOM only after awaits + guard on step) ----------
    async def _preflight_edit(self) -> None:
        if not self.draft.cookies:
            return
        info = await login.fetch_user_info(self.draft.cookies, impersonate=self.draft.impersonate)
        if not info:
            return
        self._logged_in, self._info = True, info
        client = BwsClient(self.draft.cookies, self.draft.impersonate)
        try:
            b = await client.is_bound()
        finally:
            await client.aclose()
        self._bound_ok = (b is not False)
        self._render_sidebar()
        if self.step == 2 and not self._force_relogin:
            self._show_account()
        elif self.step == 3 and self._bound_ok:
            self._safe_update("#bind-status", Text("已绑定门票实名信息(→ 继续)", style="#51CF66"))

    async def _do_login(self) -> None:
        imp = self.draft.impersonate
        if self.editing and self.draft.cookies and not self._force_relogin:
            info = await login.fetch_user_info(self.draft.cookies, impersonate=imp)
            if info and self.step == 2:
                self._logged_in, self._info = True, info
                self._show_account()
                self._render_sidebar()
                return
        self._force_relogin = False
        try:
            async with new_async_session(imp) as s:
                await login._warmup_session(s)
                url, key = await login.generate_qrcode(s)
                if self.step != 2:
                    return
                self._login_url = url
                self._safe_update("#qr-area", Text(login.render_qr(url, compact=True, fit=self._qr_fit()), no_wrap=True))
                self._safe_update("#qr-url", Text(f"扫不出可复制:{url}", style="grey50"))
                self._safe_update("#login-status", Text("用「哔哩哔哩」App 扫码并确认", style=PINK))
                cookies = await login.poll_qrcode_cb(
                    s, key, on_state=lambda st: self._safe_update("#login-status", Text(LOGIN_STATE.get(st, st), style="grey62")))
        except Exception:
            self._safe_update("#login-status", Text("登录出错,按 r 重试", style="#FF6B6B"))
            return
        if not cookies:
            return
        info = await login.fetch_user_info(cookies, impersonate=imp)
        if not info:
            self._safe_update("#login-status", Text("拿到 cookie 但校验失败,按 r 重试", style="#FF6B6B"))
            return
        self.draft.cookies = cookies
        self.draft.uid, self.draft.uname = info["uid"], info["uname"]
        if not self.editing and self.draft.name in ("", "新配置"):
            self.draft.name = self._unique_name(info["uname"])
        profiles.save(self.draft)
        self._saved_name = self.draft.name
        self._logged_in, self._info = True, info
        self._render_sidebar()
        if self.step == 2:
            self._safe_update("#qr-area", Text(""))
            self._safe_update("#qr-url", Text(""))
            self._safe_update("#login-status", Text(f"登录成功:{info['uname']}(已保存,→ 继续)", style="#51CF66"))
        self._toast(f"登录成功 {info['uname']}", "ok")

    async def _do_bind_check(self) -> None:
        client = BwsClient(self.draft.cookies, self.draft.impersonate)
        try:
            b = await client.is_bound()
        finally:
            await client.aclose()
        if self.step != 3:
            self._bound_ok = (b is not False)
            self._render_sidebar()
            return
        try:
            status = self.query_one("#bind-status", Static)
            form = self.query_one("#bind-form", Vertical)
        except Exception:
            return
        form.remove_children()
        if b is True:
            self._bound_ok = True
            status.update(Text("已绑定门票实名信息(→ 继续)", style="#51CF66"))
        elif b is None:
            self._bound_ok = True
            status.update(Text("绑定状态查询失败(网络),按已绑定继续", style="#FFD43B"))
        else:
            self._bound_ok = False
            status.update(Text("尚未绑定,填写实名信息后提交:", style="#FFD43B"))
            form.mount(
                Input(placeholder="姓名", id="bind-name"),
                RadioSet(*[RadioButton(v, value=(k == 0)) for k, v in ID_TYPES.items()], id="bind-idtype"),
                Input(placeholder="证件号(完整)", id="bind-id"),
                Input(placeholder="票号后4位", id="bind-tk4"),
                Button("提交绑定", id="bind-submit", variant="primary"))
        self._render_sidebar()

    async def _submit_bind(self) -> None:
        try:
            name = self.query_one("#bind-name", Input).value.strip()
            idtype = self.query_one("#bind-idtype", RadioSet).pressed_index
            pid = self.query_one("#bind-id", Input).value.strip()
            tk4 = self.query_one("#bind-tk4", Input).value.strip()
        except Exception:
            return
        if not (name and pid and len(tk4) == 4):
            self._safe_update("#bind-status", Text("姓名/证件号不能为空,票号必须后4位", style="#FF6B6B"))
            return
        idtype = idtype if (idtype is not None and idtype >= 0) else 0
        client = BwsClient(self.draft.cookies, self.draft.impersonate)
        try:
            resp = await client.ticket_bind(name, pid, tk4, idtype)
        finally:
            await client.aclose()
        if self.step != 3:
            return
        if resp.get("code") == 0:
            self._bound_ok = True
            try:
                self.query_one("#bind-form", Vertical).remove_children()
            except Exception:
                pass
            self._safe_update("#bind-status", Text("绑定成功(→ 继续)", style="#51CF66"))
            self._toast("绑定成功", "ok")
            self._render_sidebar()
        else:
            from core.api import BIND_MESSAGES
            self._safe_update("#bind-status",
                              Text(f"绑定失败:{BIND_MESSAGES.get(resp.get('code'), resp.get('message', '未知'))}", style="#FF6B6B"))

    async def _do_load_sessions(self) -> None:
        client = BwsClient(self.draft.cookies, self.draft.impersonate)
        try:
            opts = await collect_sessions(client)
        except Exception:
            self._safe_update("#sess-status", Text("拉取场次失败,可返回上一步重试", style="#FF6B6B"))
            return
        finally:
            await client.aclose()
        self.sessions_opts = opts
        self.selected = {s.get("reserve_id") for s in (self.draft.sessions or [])}
        for o in opts:
            o["_key"] = _pinyin_key(f"{o['title']} {o.get('location','')} {o['date']}")
        self._sessions_loaded = True
        if self.step != 4:
            return
        self._fill_sessions(opts)
        self._update_sess_count()

    # ---------- sessions table ----------
    def _fill_sessions(self, opts: list[dict]) -> None:
        try:
            t = self.query_one("#sess-table", DataTable)
        except Exception:
            return
        t.clear()
        for o in opts:
            rid = o.get("reserve_id")
            mark = Text("✓", style=f"bold {PINK}") if rid in self.selected else Text(" ")
            ok = selectable(o)
            title = o["title"][:22] + ("" if ok else " (不可选)")
            t.add_row(mark, o["type_name"][:2], o["date"],
                      Text(title, style="white" if ok else "grey50"),
                      fmt_ts(o["begin"]), key=str(rid))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sess-filter":
            q = event.value.strip().lower()
            opts = ([o for o in self.sessions_opts if q in o.get("_key", "")]
                    if q else self.sessions_opts)
            self._fill_sessions(opts)
        elif event.input.id == "px-input":
            raw = event.value.strip()
            if not raw:
                try:
                    n0 = len(resolve_pool(self.draft.proxies or []))
                except Exception:
                    n0 = len(self.draft.proxies or [])
                msg = f"已有 {n0} 个代理 · 留空不改" if n0 else "未填 = 直连"
            elif raw in ("-", "清空", "无"):
                msg = "将清空代理(直连)"
            else:
                toks = [x for x in re.split(r"[,，\s]+", raw) if x]
                msg = f"将替换为 {len(toks)} 项" + ("(含文件)" if any(t.startswith("@") for t in toks) else "")
            self._safe_update("#px-status", Text(msg, style="grey62"))

    def action_toggle_session(self) -> None:
        if self.step != 4:
            return
        try:
            t = self.query_one("#sess-table", DataTable)
        except Exception:
            return
        if t.row_count == 0:
            return
        row = t.cursor_row
        key = t.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value
        try:
            rid = int(key)
        except (TypeError, ValueError):
            rid = key
        if rid in self.selected:
            self.selected.discard(rid)
            t.update_cell_at(Coordinate(row, 0), Text(" "))
        else:
            self.selected.add(rid)
            t.update_cell_at(Coordinate(row, 0), Text("✓", style=f"bold {PINK}"))
        self._update_sess_count()

    def action_select_all(self) -> None:
        if self.step != 4 or not self.sessions_opts:
            return
        self.selected = {o.get("reserve_id") for o in self.sessions_opts if selectable(o)}
        self._fill_sessions(self.sessions_opts)
        self._update_sess_count()
        self._toast(f"已全选 {len(self.selected)} 个可抢场次", "ok")

    def _update_sess_count(self) -> None:
        self._safe_update("#sess-status",
                          Text(f"已选 {len(self.selected)} / 共 {len(self.sessions_opts)} · 空格勾选 · Ctrl+A 全选", style="grey62"))

    # ---------- finish ----------
    def _unique_name(self, name: str, exclude: set | None = None) -> str:
        existing = set(profiles.list_profiles()) - {x for x in (exclude or set()) if x}
        if name not in existing:
            return name
        i = 2
        while f"{name}_{i}" in existing:
            i += 1
        self._toast(f"已有同名配置,另存为「{name}_{i}」", "warn")
        return f"{name}_{i}"

    def _finish(self) -> None:
        known = {self._saved_name, self._orig_name}
        if self.draft.name not in known:
            self.draft.name = self._unique_name(self.draft.name, exclude=known)
        if self._saved_name and self.draft.name != self._saved_name:
            profiles.delete(self._saved_name)
        if self._orig_name and self.draft.name != self._orig_name:
            profiles.delete(self._orig_name)
        profiles.save(self.draft)
        self._done = True
        self._toast("配置已保存", "ok")
        body = self.query_one("#stepbody", VerticalScroll)
        body.remove_children()
        verb = "已更新" if self.editing else "已创建"
        body.mount(Vertical(
            Static(Text(f"✓ {verb}配置「{self.draft.name}」", style="bold #51CF66")),
            Static(Text(f"{len(self.draft.sessions)} 个场次 · {len(self.draft.proxies)} 个代理 · {self.draft.impersonate}", style="grey62")),
            Static(Text("回到主控台会自动出现 — 按 q 关闭此窗口", style="grey62")),
            classes="step"))
        self.query_one("#btn-next").disabled = True
        self.query_one("#btn-back").disabled = True

    def action_retry(self) -> None:
        if self.step == 2 and not self._logged_in:
            self._force_relogin = True
            self._show_step(2)


async def run_wizard(args, existing: Profile | None = None) -> None:
    await WizardApp(args, existing).run_async()
