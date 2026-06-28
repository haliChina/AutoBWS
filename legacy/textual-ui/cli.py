"""AUTOBWS 哔哩乐园抢票 —— 入口 + 参数分发。

控制台 Hub(默认 TTY,开抢另开独立窗口)与纯文本/非 TTY 菜单(本窗口内联运行);
多配置 + 每账号代理池(仅抢票阶段用,抢前测质量)+ 多账号多线程并发。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import threading

from core import profiles
from net import proxycheck
from core.profiles import Profile, session_snapshot
from net.proxy import parse_proxy, proxy_label, resolve_pool
from core.login import ensure_login, fetch_user_info
from core.api import BwsClient, ServerClock, ID_TYPES, BIND_MESSAGES
from core.grabber import collect_sessions, selectable, jobs_from_profile, ThreadedGrab
from utils.fmt import fmt_ts, fmt_duration
from net.http import IMPERSONATE_CHOICES, DEFAULT_IMPERSONATE

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_plk = threading.Lock()


def log(msg: str) -> None:
    with _plk:
        print("\r" + " " * 48 + "\r" + msg, flush=True)


async def ensure_bound_plain(client: BwsClient, *, force: bool = False) -> bool:
    if not force:
        b = await client.is_bound()
        if b is True:
            log("[OK] 账号已绑定门票实名信息")
            return True
        if b is None:
            log("[!] 绑定状态查询失败(网络波动),按已绑定继续")
            return True
    log("[!] 账号尚未绑定,需先绑定才能抢票。")
    while True:
        name = input("姓名: ").strip()
        print("证件类型: " + " / ".join(f"{k}={v}" for k, v in ID_TYPES.items()))
        try:
            id_type = int(input("证件类型编号 [默认0]: ").strip() or "0")
        except ValueError:
            id_type = 0
        if id_type not in ID_TYPES:
            id_type = 0
        personal_id = input("证件号(完整): ").strip()
        ticket4 = input("票号后4位: ").strip()
        if not (name and personal_id and len(ticket4) == 4):
            log("[X] 信息不全(票号必须4位),重试。")
            continue
        if input(f"确认 {name}/{ID_TYPES[id_type]}/{personal_id}/尾号{ticket4}?(y/n): ").strip().lower() not in ("y", "yes", ""):
            continue
        resp = await client.ticket_bind(name, personal_id, ticket4, id_type)
        if resp.get("code") == 0:
            log("[OK] 绑定成功")
            return True
        log(f"[X] 绑定失败:[{resp.get('code')}] {BIND_MESSAGES.get(resp.get('code'), resp.get('message', '未知'))}")
        if input("再试?(y/n): ").strip().lower() not in ("y", "yes", ""):
            return False


def choose_sessions_plain(options: list[dict], clock: ServerClock) -> list[dict]:
    if not options:
        log("没有可预约的场次。")
        return []
    now = clock.now_ms()
    print("\n=== 可选场次 ===")
    for i, o in enumerate(options, 1):
        opened = "[已开抢]" if o["begin"] * 1000 <= now else "" + fmt_duration(o["begin"] * 1000 - now)
        bad = "" if selectable(o) else " [不可选]"
        print(f"[{i:>2}] {o['type_name']} {o['date']} | {o['title']} | {o['location']} | 开抢 {fmt_ts(o['begin'])} {opened}{bad}")
    raw = input("\n序号(逗号分隔;all=全部可抢): ").strip().lower()
    if not raw:
        return []
    if raw == "all":
        return [o for o in options if selectable(o)]
    idxs = [int(x) - 1 for x in raw.replace("，", ",").split(",") if x.strip().isdigit() and 1 <= int(x.strip()) <= len(options)]
    return [options[i] for i in idxs if selectable(options[i])]


async def new_profile_plain(args, existing: Profile | None = None) -> Profile | None:
    editing = existing is not None
    print("\n--- 编辑配置 ---" if editing else "\n--- 新建配置 ---")
    n0 = len(existing.proxies) if (editing and existing.proxies) else 0
    hint = f"(已有 {n0} 个;留空=保持不变,输入则替换,输入 - 清空)" if n0 else "(逗号/空格分隔;可填@file;留空=直连)"
    px_in = input(f"代理池{hint}: ").strip()
    if px_in in ("-", "清空", "无"):
        proxies_raw = []
    elif px_in:
        proxies_raw = [x for x in re.split(r"[,，\s]+", px_in) if x]
    else:
        proxies_raw = list(existing.proxies) if (editing and existing.proxies) else []
    fallback_direct = (input("代理都失效时转直连?(Y/n): ").strip().lower() in ("y", "yes", "")) if proxies_raw else True
    impersonate = existing.impersonate if editing else args.impersonate

    cookies = info = None
    if editing and existing.cookies:
        info = await fetch_user_info(existing.cookies, impersonate=impersonate)
        if info:
            cookies = existing.cookies
            log(f"[OK] 复用已保存登录:{info['uname']}")
        else:
            log("[!] 已保存登录已失效,需重新登录")
    if not cookies:
        cookies = await ensure_login(impersonate=impersonate)
        info = await fetch_user_info(cookies, impersonate=impersonate)
    if not info:
        log("登录态校验失败。")
        return None

    prof = existing or Profile(name=info["uname"] or "新配置")
    prof.uid, prof.uname, prof.impersonate = info["uid"], info["uname"], impersonate
    prof.proxies, prof.fallback_direct, prof.cookies = proxies_raw, fallback_direct, cookies
    profiles.save(prof)
    log(f"[OK] 登录态已保存到「{prof.name}」(可稍后编辑补全场次)")

    client = BwsClient(cookies, impersonate)
    try:
        if not await ensure_bound_plain(client, force=args.rebind):
            return prof
        clock = ServerClock(client)
        log(f"{clock.describe()}" if await clock.sync() else "[!] 校时失败")
        options = await collect_sessions(client, notify=log)
        chosen = choose_sessions_plain(options, clock)
        if chosen:
            prof.sessions = [session_snapshot(o) for o in chosen]
        elif editing:
            log("未改动场次,保留原有。")
        else:
            log("未选场次,已存为待完善配置(稍后可编辑添加)。")
        try:
            prof.base_interval = int(input(f"基础间隔ms [{prof.base_interval}]: ").strip() or prof.base_interval)
            prof.offset = int(input(f"提前ms [{prof.offset}]: ").strip() or prof.offset)
        except ValueError:
            pass
        newname = input(f"配置名 [{prof.name}]: ").strip() or prof.name
        if newname != prof.name:
            profiles.delete(prof.name)
            prof.name = newname
        profiles.save(prof)
        log(f"[OK] 配置已保存:{prof.name}({len(prof.sessions)} 场次, {len(proxies_raw)} 代理)")
        return prof
    finally:
        await client.aclose()


async def run_profiles_plain(profs: list[Profile], args) -> None:
    from core.lock import acquire_accounts, release_all
    profs, skipped, _locks = acquire_accounts(profs)
    for nm in skipped:
        log(f"账号「{nm}」已在其它窗口抢票,跳过")
    if not profs:
        log("所选账号都已在其它窗口抢票,无可执行。")
        return
    try:
        await _run_profiles_plain_inner(profs, args)
    finally:
        release_all(_locks)


async def _run_profiles_plain_inner(profs: list[Profile], args) -> None:
    jobs = []
    for p in profs:
        jobs += jobs_from_profile(p)
    if not jobs:
        log("所选配置没有可抢的场次。")
        return
    log(f"将以 {len(profs)} 账号 / {len(jobs)} 场次并发抢。")

    account_opts: dict[str, dict] = {}
    for p in profs:
        raws = resolve_pool(p.proxies)
        if not raws:
            ranked = [None]
        else:
            log(f"检测「{p.name}」的 {len(raws)} 个代理...")
            res = await proxycheck.evaluate(raws, p.impersonate)
            for r in res["results"]:
                log("  " + proxycheck.fmt_result(r))
            ranked = res["ranked"] or [None]
            log(f"→ {p.name}: {('用 ' + proxy_label(res['best'])) if res['best'] else '代理不可用,直连'}")
        account_opts[p.name] = {"proxies": ranked, "fallback_direct": getattr(p, "fallback_direct", True)}

    p0 = profs[0]
    sc = BwsClient(p0.cookies, p0.impersonate)
    clock = ServerClock(sc)
    log(f"{clock.describe()}" if await clock.sync() else "[!] 校时失败,用本地墙钟")
    await sc.aclose()

    if args.dry_run:
        for j in jobs:
            log(f"[dry-run] {j.account} inter_reserve_id={j.sess['reserve_id']} ticket_no={j.sess['ticket_no']}")
        return
    if args.probe:
        for j in jobs:
            proxy = (account_opts.get(j.account, {}).get("proxies") or [None])[0]
            c = BwsClient(j.cookies, j.impersonate, proxy)
            try:
                r = await c.reserve_do(j.sess["reserve_id"], j.sess["ticket_no"])
                log(f"[probe] {j.account} {j.sess['title'][:12]}: http={r.get('http')} code={r.get('code')} {r.get('message')}")
            finally:
                await c.aclose()
        return

    tg = ThreadedGrab(jobs, clock, account_opts=account_opts, notify=log, refresh=True)
    tg.start()
    aborted = False
    try:
        announced = False
        while not tg.all_done:
            rem = tg.earliest_target - clock.now_ms()
            if rem > 0:
                print(f"\r距最早开抢 {fmt_duration(rem)} ...", end="", flush=True)
                await asyncio.sleep(1)
            else:
                if not announced:
                    print("\r" + " " * 48 + "\r", end="", flush=True)
                    announced = True
                await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        aborted = True
        tg.stop()
    finally:
        tg.stop()
        tg.join()
        tg.close()
    log("—— 抢票结果 ——")
    for p in sorted(tg.progress.values(), key=lambda x: (x["account"], x["date"])):
        log(f"  {p['account']} {p['date'][4:]} {p['title'][:16]}: {p['result'] or p['phase']}")
    log("已中止。" if aborted else "全部场次处理完毕。")


def _pause() -> None:
    try:
        input("\n—— 按回车关闭此窗口 ——")
    except Exception:
        pass


async def amain_plain(args) -> None:
    while True:
        names = profiles.list_profiles()
        if not names:
            prof = await new_profile_plain(args)
            if prof and input(f"现在用「{prof.name}」开抢?(y/n): ").strip().lower() in ("y", "yes", ""):
                await run_profiles_plain([prof], args)
            return
        print("\n配置文件:")
        for i, n in enumerate(names, 1):
            p = profiles.load(n)
            px = "直连" if not p.proxies else (proxy_label(parse_proxy(p.proxies[0])) if len(p.proxies) == 1 else f"{len(p.proxies)}个池")
            print(f"  [{i}] {n} | {p.uname} | {len(p.sessions)}场次 | {px}")
        raw = input("序号(多选用空格)开抢 / n新建 / e N编辑 / d N删除 / q退出: ").strip().lower()
        if raw in ("q", ""):
            return
        if raw == "n":
            prof = await new_profile_plain(args)
            if prof and input(f"现在用「{prof.name}」开抢?(y/n): ").strip().lower() in ("y", "yes", ""):
                await run_profiles_plain([prof], args)
            continue
        if raw.startswith("e"):
            rest = raw[1:].strip()
            if rest.isdigit() and 1 <= int(rest) <= len(names):
                await new_profile_plain(args, existing=profiles.load(names[int(rest) - 1]))
            continue
        if raw.startswith("d"):
            rest = raw[1:].strip()
            if rest.isdigit() and 1 <= int(rest) <= len(names):
                profiles.delete(names[int(rest) - 1])
                print("已删除。")
            continue
        idxs = [int(x) - 1 for x in raw.replace(",", " ").split() if x.isdigit() and 1 <= int(x) <= len(names)]
        profs = [profiles.load(names[i]) for i in dict.fromkeys(idxs)]
        if profs:
            await run_profiles_plain(profs, args)


async def amain(args) -> None:
    use_rich = (not args.plain) and sys.stdout.isatty()

    if args.grab_worker:
        if use_rich:
            from ui.grabwin import run_grab_app
            await run_grab_app(args)
        else:
            profs = profiles.load_all(args.profiles or [])
            if profs:
                await run_profiles_plain(profs, args)
            else:
                log(f"找不到配置:{args.profiles}")
            _pause()
        return

    if args.new_profile or args.edit_profile:
        existing = profiles.load(args.edit_profile) if args.edit_profile else None
        if use_rich:
            from ui.wizard import run_wizard
            await run_wizard(args, existing=existing)
        else:
            await new_profile_plain(args, existing=existing)
            _pause()
        return

    if args.profiles:
        profs = profiles.load_all(args.profiles)
        if not profs:
            log(f"找不到配置:{args.profiles}")
            return
        if use_rich:
            from ui.grabwin import run_grab_app
            await run_grab_app(args)
        else:
            await run_profiles_plain(profs, args)
        return

    if use_rich:
        try:
            from ui.hub import HubApp
            await HubApp(args).run_async()
            return
        except Exception as e:
            print(f"[Textual GUI 加载失败,转纯文本:{e}]")
    await amain_plain(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="AUTOBWS 哔哩乐园抢票")
    parser.add_argument("--profile", action="append", dest="profiles", metavar="NAME",
                        help="用指定配置抢(可多次=多账号并发)")
    parser.add_argument("--grab-worker", action="store_true",
                        help="(内部)在独立窗口里只跑抢票仪表盘")
    parser.add_argument("--new-profile", action="store_true",
                        help="(内部)在独立窗口里新建一个配置")
    parser.add_argument("--edit-profile", metavar="NAME", default=None,
                        help="(内部)在独立窗口里编辑指定配置")
    parser.add_argument("--list-profiles", action="store_true", help="列出所有配置后退出")
    parser.add_argument("--base-interval", type=int, default=80, help="基础发包间隔(ms),默认80")
    parser.add_argument("--offset", type=int, default=50, help="提前发包毫秒数(>0提前),默认50")
    parser.add_argument("--impersonate", choices=IMPERSONATE_CHOICES, default=DEFAULT_IMPERSONATE,
                        help="移动端指纹模拟目标")
    parser.add_argument("--proxy", default="", help="新建配置时代理池默认值")
    parser.add_argument("--theme", default="ansi-dark", help="Textual 主题")
    parser.add_argument("--plain", action="store_true", help="纯文本模式")
    parser.add_argument("--rebind", action="store_true", help="强制重新绑定")
    parser.add_argument("--probe", action="store_true", help="选完立即各发一次看返回码")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发的包")
    args = parser.parse_args()

    if args.list_profiles:
        for n in profiles.list_profiles():
            p = profiles.load(n)
            px = "直连" if not p.proxies else f"{len(p.proxies)}代理"
            print(f"{n}\t{p.uname}\t{len(p.sessions)}场次\t{px}")
        return

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n已中止。")


if __name__ == "__main__":
    main()
