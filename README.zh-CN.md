# webrc · 用浏览器远程操控自己的 Windows 电脑

[English](README.md)

三个文件。被控电脑只**出站**连一个你自己的中继，家里电脑和手机开浏览器就能看画面、动鼠标键盘。
客户端不装任何东西，被控端不用公网 IP、不用端口映射，中间没有第三方服务。

```
被控电脑（Windows）              你的服务器                任意浏览器
    agent.py  ── 出站 WSS ──▶     relay.py    ◀── WSS ──   电脑 / 手机
```

- **被控端只出站**：不监听、不要公网 IP，NAT 再差也能连。
- **客户端就是浏览器**：电脑、手机都行；手机有触屏手势，中文输入法能用。
- **中继只转发**：三百来行 Python，不解码画面，有公网 IP 的机器都能跑，Windows Server 2012 R2 也行。
- **H.264 / JPEG 随时切**。H.264：常驻 ffmpeg 用显卡硬编（QSV / NVENC / AMF，都不行才 x264），码率封顶，
  浏览器 WebCodecs 硬解。JPEG：64px 分块比对只发变化条带，零依赖。
- **桌面不动零流量，一帧一回执**：发一帧等回执再截下一帧，慢链路上不堆积旧画面。
- **多显示器**：工具栏上切，也能把所有屏拼成一张。

被控端只支持 Windows；中继哪都能跑；观看端是任意现代浏览器。

## 本机先试

```bash
pip install -r requirements.txt
```

```bash
python relay.py --port 18765 --token test
```

```bash
python agent.py --relay ws://127.0.0.1:18765 --token test --dry-run
```

浏览器开 `http://127.0.0.1:18765/?token=test`。`--dry-run` 只打印输入事件不真注入。

## 部署

**1. 证书和口令。** WebCodecs 要求 HTTPS，给中继 IP 自签一次证书（Git for Windows 自带 openssl）：

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout certs/relay.key -out certs/relay.crt -subj "/CN=webrc-relay" -addext "subjectAltName=IP:<公网IP>,IP:127.0.0.1"
```

`relay.py` 第一次启动会把口令生成到 `relay.token`，agent 用同一个串。这两样都已 gitignore。

**2. 中继**（有公网 IP 的机器）。把 `relay.py`、`index.html`、`certs/`、`relay.token` 复制过去，
系统防火墙和云安全组放行 TCP 33445，然后：

```bash
pip install aiohttp
```

```bash
python relay.py --port 33445 --cert certs/relay.crt --key certs/relay.key --log relay.log
```

**3. 被控端**（那台 Windows）。`pip install -r requirements.txt`；H.264 要 PATH 里有 ffmpeg
（`winget install Gyan.FFmpeg`，没有就只有 JPEG）；然后：

```bash
python agent.py --relay wss://<公网IP>:33445 --token <口令> --cafile certs\relay.crt --log agent.log
```

要操作弹 UAC 的窗口就从管理员终端起。关掉自动锁屏（设置 → 账户 → 登录选项 → 要求重新登录：从不），
锁屏后 agent 看不到也控不了。开着 TUN 代理的话给中继 IP 加一条直连规则，不然绕代理出口。

**4. 观看端。** `https://<公网IP>:33445/?token=<口令>`，证书警告点一次继续。手机上「添加到主屏幕」。

两边都是普通进程：升级或重启就是杀掉旧的再起一个。新旧版本可以混用——旧页面只是拿到 JPEG、没有屏幕下拉框。

## 操作

| | 电脑 | 手机 |
|---|---|---|
| 左键 / 右键 | 点 | 点一下 / 长按松开 |
| 拖动 | 按住拖 | 长按后拖 |
| 滚轮 | 滚轮 | 双指滑 |
| 打字 | 直接打（含输入法） | ⌨ 弹软键盘 |
| Ctrl / Alt / Shift / Win | 直接按 | 先点亮工具栏上的修饰键再操作 |
| 编码 · 码率 · 分辨率 · 帧率 · 屏幕 | 工具栏下拉，即时生效 | 同左 |

状态栏：fps · KB/s · 往返 ms · 分辨率 · 编码。H.264 下码率上限就是流量上限（2M 档最坏 ≈ 250 KB/s，
打字时每帧几百字节）；JPEG 下整屏变化一帧 200–600 KB。

## 参数

`agent.py`：`--monitor N`（0 = 全部屏）、`--codec auto|h264|jpeg`、`--encoder auto|qsv|nvenc|amf|x264`、
`--capture auto|dxcam|mss`、`--width`（1280）、`--fps`（12）、`--kbps`（2000）、`--quality`（60）、
`--cafile` / `--insecure`、`--dry-run`、`--log`。画质参数都是初始值，页面上随时改。

`relay.py`：`--host`、`--port`（33445）、`--token`、`--cert` / `--key`（不给就是明文 ws）、`--log`。

## 出问题

| 现象 | 多半是 |
|---|---|
| 「连接断开，重连中…」 | 中继没起来，或云安全组没放端口 |
| 「被控端离线」 | 看 `agent.log`：`Cannot connect` = 端口；`certificate verify failed` = 证书 SAN 里没有中继 IP；`403` = 口令不一致 |
| 选不了 H.264 | 没有 WebCodecs：要 HTTPS（或 localhost）且 Chrome/Edge 94+、Safari 16.4+、Firefox 130+ |
| 选了 H.264 状态栏还是 JPEG | `agent.log` 的 `probe` 行：ffmpeg 不在 PATH 或没有能用的编码器 |
| 「硬解无输出」 | 该浏览器 / 显卡静默失败，页面已自动退到软解、再退 JPEG |
| 画面黑 / 不动 | 锁屏了，或有 UAC 弹窗 |

## 原理

- 一帧 = 小段二进制头（尺寸、光标、flags、条带数）+ 条带。H.264 每帧一个 Annex B 访问单元；JPEG 只发变化的瓦片，
  叠在上一帧上。帧格式和控制消息写在 `agent.py`、`relay.py` 开头的 docstring 里。
- agent 发一帧等中继回执再截下一帧（最多 2 帧在途）；6 秒不回执的观众被踢掉。中继缓存最近一个关键帧起的所有帧，
  新观众进来直接回放，基本不用惊动 agent（H.264 要关键帧得重开 ffmpeg，约 1 秒）。
- H.264：常驻 ffmpeg 从 stdin 读 BGRA、往 stdout 写 **AVI**（每块自带长度；裸 Annex B 流的最后一帧会永远压在编码器里）。
  零延迟参数、显式 4:2:0（浏览器遇到 4:4:4 不报错只黑屏）、`dump_extra` 让每个 IDR 前带 SPS/PPS。坑都注释在 `agent.py` 里。
- 解码：Annex B → avcC → `VideoDecoder`。硬解 3 秒没出帧就换软解，软解也不行才切 JPEG。
  比帧尺寸用 `visibleRect`，别用 `codedHeight`（1080 会报 1088）。
- 截屏：`dxcam` 走 DXGI 桌面复制（2560×1440 一帧 7–9 ms，画面变了才给），不行退回 `mss`（GDI，约 40 ms）。
  `timeBeginPeriod(1)` 让 asyncio 的 sleep 准到能跑满 12 fps。

## 已知取舍

- 锁屏和 UAC 安全桌面看不见：agent 跑在你的登录会话里。要解决得做成 Windows 服务 + `CreateProcessAsUser`，VNC 就是这么干的。
- 一次只传一块屏；没有声音、剪贴板、文件传输。
- 多观众共用一路编码，谁最后改设置按谁的。

## 安全

口令即一切：拿到网址的人就能控这台机器。要换就删掉 `relay.token` 重启中继。自签证书的警告说的是身份不是加密，
链路本来就是 TLS；想要浏览器认的证书就挂域名走 ACME。所有流量都从中继过，用自己的那台。

## 许可

[MIT](LICENSE)
