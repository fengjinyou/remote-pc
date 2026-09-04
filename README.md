# webrc — control your Windows PC from any browser

[简体中文](README.zh-CN.md)

Three files. The controlled PC opens one **outbound** WebSocket to a relay you run; you open a
URL on your laptop or phone and you are looking at the desktop, moving the mouse, typing.
Nothing to install on the client, no public IP or port forwarding on the controlled machine,
no third-party service in between.

```
controlled PC (Windows)            your server               any browser
     agent.py  ── outbound WSS ──▶   relay.py   ◀── WSS ──   laptop / phone
```

- **The agent only dials out** — never listens, works behind any NAT.
- **The client is a browser** — desktop or phone; touch gestures and IME input on mobile.
- **The relay only forwards** — ~300 lines of Python, never decodes a frame. Any box with a
  public IP will do, even Windows Server 2012 R2.
- **H.264 or JPEG, switchable live.** H.264: a resident `ffmpeg` encodes on the GPU
  (QSV / NVENC / AMF, x264 fallback) under a hard bitrate cap; the browser decodes with
  WebCodecs. JPEG: 64 px tile diff, only changed strips are sent, zero dependencies.
- **Zero traffic when idle, one frame in flight** — the agent waits for an ack before the
  next capture, so a slow link never queues stale video.
- **Multi-monitor** — switch from the toolbar, or stitch all screens into one.

The agent is Windows-only; the relay runs anywhere Python does; the viewer is any modern browser.

## Try it locally

```bash
pip install -r requirements.txt
```

```bash
python relay.py --port 18765 --token test
```

```bash
python agent.py --relay ws://127.0.0.1:18765 --token test --dry-run
```

Open `http://127.0.0.1:18765/?token=test`. `--dry-run` prints input events instead of
injecting them.

## Deploy

**1. Certificate and token.** WebCodecs needs HTTPS, so self-sign a certificate for the relay's
IP once (Git for Windows ships `openssl`):

```bash
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout certs/relay.key -out certs/relay.crt -subj "/CN=webrc-relay" -addext "subjectAltName=IP:<PUBLIC-IP>,IP:127.0.0.1"
```

`relay.py` generates a token into `relay.token` on first start; the agent passes the same
string. Both files are git-ignored.

**2. Relay** (any machine with a public IP). Copy `relay.py`, `index.html`, `certs/` and
`relay.token` there, open TCP 33445 in the OS firewall and in your cloud security group, then:

```bash
pip install aiohttp
```

```bash
python relay.py --port 33445 --cert certs/relay.crt --key certs/relay.key --log relay.log
```

**3. Agent** (the Windows PC). `pip install -r requirements.txt`; put `ffmpeg` on `PATH` for
H.264 (`winget install Gyan.FFmpeg` — without it you get JPEG only); then:

```bash
python agent.py --relay wss://<PUBLIC-IP>:33445 --token <TOKEN> --cafile certs\relay.crt --log agent.log
```

Run it from an Administrator terminal if you need to click on elevated windows. Turn off
automatic lock (Settings → Accounts → Sign-in options → require sign-in: Never) — the agent
cannot see or control the lock screen. If the PC runs a TUN-mode proxy, add a direct-route
rule for the relay IP or the traffic detours through your proxy exit.

**4. Viewer.** `https://<PUBLIC-IP>:33445/?token=<TOKEN>`, accept the certificate warning
once. On a phone, "Add to Home screen".

Both sides are plain processes: to upgrade or restart, kill the old one and start it again.
Mixed versions interoperate — an old page simply gets JPEG and no screen dropdown.

## Controls

| | Desktop | Phone |
|---|---|---|
| Left / right click | click | tap / long-press then release |
| Drag | hold and drag | long-press, then drag |
| Scroll | wheel | two-finger drag |
| Type | type (IME works) | ⌨ opens the soft keyboard |
| Ctrl / Alt / Shift / Win | press | tap the modifier in the toolbar, then act |
| Codec · bitrate · resolution · fps · screen | toolbar dropdowns, applied live | same |

The status bar shows fps · KB/s · round-trip ms · resolution · codec. Under H.264 the bitrate
cap is the traffic cap (2M ≈ 250 KB/s worst case; a few hundred bytes per frame while typing).
Under JPEG a full-screen change costs 200–600 KB per frame.

## Options

`agent.py`: `--monitor N` (0 = all screens), `--codec auto|h264|jpeg`,
`--encoder auto|qsv|nvenc|amf|x264`, `--capture auto|dxcam|mss`, `--width` (1280),
`--fps` (12), `--kbps` (2000), `--quality` (60), `--cafile` / `--insecure`, `--dry-run`,
`--log`. Picture settings are initial values — the page changes them live.

`relay.py`: `--host`, `--port` (33445), `--token`, `--cert` / `--key` (plain `ws` without
them), `--log`.

## Troubleshooting

| Symptom | Usually |
|---|---|
| "disconnected, reconnecting…" | relay down, or the port is not open in the cloud security group |
| "agent offline" | `agent.log`: `Cannot connect` = port; `certificate verify failed` = the cert's SAN lacks the relay IP; `403` = token mismatch |
| H.264 cannot be selected | no WebCodecs — needs HTTPS (or localhost) and Chrome/Edge 94+, Safari 16.4+, Firefox 130+ |
| H.264 selected, status bar stays JPEG | `probe` lines in `agent.log`: ffmpeg not on PATH, or no working encoder |
| "hardware decode produced nothing" | that browser/GPU fails silently; the page already fell back to software decoding, then JPEG |
| Black or frozen picture | the PC is locked, or a UAC prompt is up |

## How it works

- A frame is a small binary header (size, cursor, flags, strip count) plus strips. H.264 sends
  one Annex B access unit per frame; JPEG sends the changed tiles, composited onto the previous
  frame. The wire format and control messages are documented at the top of `agent.py` and
  `relay.py`.
- The agent waits for the relay's ack before capturing again (at most 2 frames in flight); a
  viewer that stops acking for 6 s is dropped. The relay caches every frame since the last
  keyframe and replays it to new viewers, so the agent — which would have to restart ffmpeg,
  about a second — is rarely asked for one.
- H.264: the resident ffmpeg reads BGRA on stdin and writes **AVI** on stdout (self-delimiting
  chunks; a raw Annex B stream leaves the last frame stuck in the encoder). Zero-latency
  settings, an explicit 4:2:0 pixel format (browsers silently reject 4:4:4), `dump_extra` so
  SPS/PPS precede every IDR. The gotchas are commented in `agent.py`.
- Decoding: Annex B → avcC → `VideoDecoder`. If hardware decoding accepts data but emits
  nothing for 3 s, the page retries with software decoding, then JPEG. Frame size is compared
  through `visibleRect`, never `codedHeight` (1080 comes back as 1088).
- Capture: DXGI Desktop Duplication via `dxcam` (7–9 ms at 2560×1440, and only when something
  changed), falling back to `mss` (GDI, ~40 ms). `timeBeginPeriod(1)` keeps asyncio's sleeps
  accurate enough for 12 fps.

## Limitations

- The lock screen and UAC secure desktop are invisible — the agent runs inside your session.
  Fixing that means a Windows service plus `CreateProcessAsUser`, as VNC does.
- One screen is streamed at a time. No audio, clipboard or file transfer.
- Viewers share one encode; whoever changed the settings last wins.

## Security

The token is the whole credential: anyone with the URL controls the machine. Rotate it by
deleting `relay.token` and restarting the relay. The self-signed certificate warning is about
identity, not encryption — the link is TLS either way; use a domain and ACME if you want a
trusted certificate. The relay carries everything, so run your own.

## License

[MIT](LICENSE)
