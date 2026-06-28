# AUTOBWS

B站 bw乐园(BWS）预约脚本 —— 多账号、代理池、错峰限速、本地 **Web GUI**（FastAPI + 浏览器）。

**预约前请确认您有bw门票，否则无法进行活动预约**

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

默认在 `http://127.0.0.1:8765` 启动并自动打开浏览器。常用参数:

| 参数 | 说明 |
|---|---|
| `--port N` / `--host` | 改端口 / 绑定地址(默认仅本机) |
| `--no-browser` | 不自动开浏览器 |
| `--plain` | 纯文本无头模式(不开 Web) |
| `--profile NAME` | 无头直接用指定配置抢(可多次 = 多账号并发) |
| `--list-profiles` | 列出配置后退出 |

## 功能

- **配置向导**:模拟设备 → 代理 → 扫码登录(网页二维码) → 票号绑定 → 选场次 → 抢票设置。编辑可任意跳步。
- **抢票引擎**:多账号多线程并发;同账号多场次**错峰限速 + 抖动**降风控;NTP 校时;到点定时发包。
- **代理**:支持 socks5/http/带认证/`@file`;主控台「代理检测」可调并发数,过滤不可用并按延迟保存;运行期持续测活、失效无缝切换。
- **抢票监控**:倒计时、实时进度表、统计(发包/抢中/拥挤/风控/退避/网络异常)、事件日志;MVP 结算动画 + 可选胜利音乐(`music/`)。
- **通知**:SMTP / Webhook / Telegram(Telegram 可单独配代理),抢中或完成时推送。
- **全部场次**:查看各账号票种与未预约/已预约状态。

## 目录

```
main.py        启动器(默认 Web;--plain/--profile 无头)
cli.py         纯文本无头流程
core/          api · login · profiles · grabber · lock · notify   ← 抢票引擎
net/           http · proxy · proxycheck · ntp
utils/ paths/  辅助
web/           app(FastAPI+WS) · managers · settings · static(Vue 单页)
music/         胜利音乐    profiles/ cookie 数据(本地,不入库)
legacy/        旧 Textual 终端 UI 备份
```

## 打包 exe

```bash
pip install pyinstaller
pyinstaller --onefile --console --name AUTOBWS \
  --add-data "web/static;web/static" --add-data "music;music" \
  --collect-all curl_cffi --collect-all uvicorn --collect-all websockets \
  --collect-submodules web --collect-submodules core --collect-submodules net \
  --hidden-import web.app --collect-all qrcode main.py
```

生成 `dist/AUTOBWS.exe`(单文件)。运行后 `profiles/`、`cookie.json`、`settings.json` 存在 exe 同目录,绿色便携。

## 声明
仅供个人学习与研究使用。未经作者书面授权，不得用于任何商业用途、商业服务、代抢服务或其他营利行为。严禁将本项目用于违法行为或违反相关平台规则的用途。由此产生的一切后果均由使用者自行承担，与作者无关。 若您 fork 或使用本项目，请务必遵守相关法律法规与目标平台规则。