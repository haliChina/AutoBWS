# legacy/textual-ui — 旧的 Textual 终端 GUI 备份(2026-06-28 起停用)

项目从 Textual 终端 GUI 改成 FastAPI + 浏览器 Web GUI。这里是改版前的 Textual UI 备份:
- hub.py / wizard.py / grabwin.py / toast.py / picker.py(已删)/ flow.py(已删) — 旧 ui/
- cli.py — 旧的 Textual 分发 + 纯文本兜底

引擎(core/ net/ utils/)未变,Web GUI 直接复用。Web 出问题时可参考/回退此备份。
