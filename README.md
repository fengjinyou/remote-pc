# WebRC — self-hosted browser remote desktop for Windows

[简体中文](README.zh-CN.md)

WebRC lets you view and control an authorized Windows PC from a browser. The agent on the PC makes one outbound WebSocket connection to a relay that you run; browsers connect to that relay. The agent does not listen for inbound connections.

[Try it locally](#local-quickstart) · [Deploy your relay](#relay-only-installation) · [Contribute](CONTRIBUTING.md)

![WebRC viewer displaying a synthetic test desktop](docs/images/viewer-demo.png)

*Synthetic screen rendered through the actual relay and browser viewer. This illustrates the interface; it is not a real remote desktop or a performance measurement.*

## Key capabilities

- **Outbound-only agent** — `agent.py` connects out to your relay and does not open an agent-side listening port.
- **Multi-PC switching** — connect several agents to one relay and select one from the viewer toolbar; the viewer WebSocket does not reconnect when switching PCs.
- **Mobile touch and IME input** — touch gestures, soft-keyboard input, and composition/IME text are supported where the browser provides them.
- **Monitor selection** — choose one monitor or the combined virtual desktop (`--monitor 0`); the viewer can switch monitors while connected.
- **JPEG and optional H.264** — JPEG sends changed strips and is the broad-compatibility path. H.264 is optional and uses ffmpeg plus the browser `VideoDecoder`; availability is discovered at runtime.
- **Ack backpressure** — the agent waits for frame acknowledgements, so a slow viewer does not cause an unbounded backlog of old frames.
- **Pause when nobody watches** — viewers disconnect after 10 seconds in the background, and agents stop capturing when there are no active viewers.

## Architecture

```text
Windows PC A  agent.py ── outbound WSS ─┐
Windows PC B  agent.py ── outbound WSS ─┤→ relay.py ── WSS ─→ browser viewer
                                       └← input events ←── browser viewer
```

- The agent captures the interactive Windows desktop, encodes frames, and sends them to the relay.
- The relay forwards frames and control messages. It does not decode video.
- The viewer decodes JPEG or H.264 and sends mouse, keyboard, touch, and settings messages back through the relay.
- The shared token is checked by the relay for both agent and viewer connections.

## Requirements

- **Agent:** Windows with an interactive, logged-in desktop and Python 3.11+. Base dependencies are `aiohttp`, `mss`, `numpy`, `opencv-python-headless`, and `pynput`; install them from [requirements.txt](requirements.txt).
- **Optional agent dependencies:** `dxcam` is optional and installed separately from [requirements-optional.txt](requirements-optional.txt); capture automatically falls back to `mss` when it is unavailable. `--monitor 0` uses `mss`. ffmpeg is a separate optional executable used only for H.264.
- **Relay:** Python 3.11+; the relay source imports only `aiohttp` beyond the standard library; install it from [requirements-relay.txt](requirements-relay.txt). Other relay operating systems still need validation.
- **Viewer:** a browser with WebSocket and canvas support. JPEG is the broad-compatibility codec. H.264 requires a secure context, browser `VideoDecoder` support, and a working ffmpeg encoder on the agent.

## Local quickstart

This preview binds the relay to loopback, uses JPEG and `mss`, and uses an example token only for the local session.

> **Safety:** `--dry-run` still captures and transmits the desktop. It only logs input events instead of injecting them. Do not run this preview while sensitive content is visible.

1. Download or clone the repository, then open two PowerShell terminals in its root directory.
2. In terminal 1, create a virtual environment and install the agent requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

3. In terminal 1, start the relay:

```powershell
.\.venv\Scripts\python.exe relay.py --host 127.0.0.1 --port 18765 --token local-demo-only-change-me
```

4. In terminal 2, start the agent:

```powershell
.\.venv\Scripts\python.exe agent.py --relay ws://127.0.0.1:18765 --token local-demo-only-change-me --codec jpeg --capture mss --dry-run
```

5. Open:

```text
http://127.0.0.1:18765/?token=local-demo-only-change-me
```

The example token is only for this loopback preview. Never use it on a deployed relay. This agent process skips H.264 encoder initialization, so it uses JPEG even if the viewer requests H.264. To try H.264 later, install ffmpeg and restart the agent without `--codec jpeg`.

## Relay-only installation

The relay can run separately from the agent.

1. Copy `relay.py`, `index.html`, and `requirements-relay.txt` to the relay host.
2. Install it with its own virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-relay.txt
```

3. Provision certificates and start the relay as described below. Allow its chosen TCP port through the host firewall and any cloud/network security layer.
4. With `--token` omitted, the relay generates or reuses `relay.token` beside `relay.py`. In a separate terminal on that host, read it locally:

```powershell
(Get-Content -LiteralPath .\relay.token -Raw).Trim()
```

Keep this value private and use it for every authorized agent and viewer. Startup logs do not print the token. An existing empty token file is an error; stop the relay and remove the empty file before restarting to generate a new token.

## TLS deployment

Use HTTPS/WSS for anything beyond the loopback quickstart. Prefer a certificate trusted by your browsers. For a private CA or self-signed certificate, verify its fingerprint out of band, configure the relay with `--cert` and `--key`, and configure each agent with `--cafile`.

If OpenSSL is installed, this PowerShell example creates a private test certificate and key in the git-ignored `certs` directory. Replace the example DNS name and IP address with your relay's real values:

```powershell
New-Item -ItemType Directory -Force -Path .\certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout .\certs\relay.key -out .\certs\relay.crt -subj "/CN=relay.example.invalid" -addext "subjectAltName=DNS:relay.example.invalid,IP:203.0.113.10"
```

Run the relay with the certificate:

```powershell
.\.venv\Scripts\python.exe relay.py --host 0.0.0.0 --port 33445 --cert .\certs\relay.crt --key .\certs\relay.key
```

For a private certificate, configure trust in your browser or OS only after verifying its fingerprint. Copy only the public certificate or CA certificate to the agent; keep the private key on the relay. Keep certificate verification enabled rather than bypassing browser warnings or using `--insecure`.

## Agent deployment

On the authorized Windows PC:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Optionally install `dxcam`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

For H.264, install ffmpeg separately and ensure it is on `PATH`. Copy the verified public relay certificate to `certs/relay.crt` on the agent, then connect (replace the example host with your relay host):

```powershell
$relayToken = Read-Host 'Relay token'
.\.venv\Scripts\python.exe agent.py --relay wss://relay.example.invalid:33445 --token $relayToken --cafile .\certs\relay.crt --name home-pc
```

Open `https://relay.example.invalid:33445/?token=YOUR_TOKEN` in the viewer, replacing the host and `YOUR_TOKEN` with your values. For more PCs, run another agent with a different `--name`; duplicate names replace the previous connection.

Run the agent as a standard user by default. A standard-user agent cannot control elevated windows because of Windows UIPI. Running it as administrator broadens what it can control and the impact of compromise; it still cannot capture or control the secure desktop, UAC prompts, or the lock screen. Only control devices you are authorized to control.

## Controls

| Action | Desktop | Touch |
|---|---|---|
| Left click / double click | click / double-click | tap / double-tap |
| Right click | right-click | two-finger tap, or long-press then release |
| Drag | press and drag | long-press, then drag |
| Scroll | wheel or trackpad | two-finger drag |
| Zoom | pinch or toolbar `−` / `+` | pinch; two-finger double-tap toggles zoom |
| Pan while zoomed | move the pointer to the view edge | one-finger drag |
| Type | keyboard, including IME | soft keyboard; composition text is sent |
| Modifiers | hold `Ctrl` / `Alt` / `Shift` / `Win` | tap the toolbar modifier, then act |
| Full screen | toolbar button or browser full screen | toolbar button where supported |

The status bar shows measured frame rate, throughput, round-trip time, resolution, and codec. It is diagnostic output, not a performance guarantee.

The viewer toolbar currently uses Chinese labels. Full-screen shortcut capture depends on browser support; hold `Esc` to leave full screen.

## Options

`agent.py`:

- `--relay`, `--token`, `--name`
- `--cafile`, `--insecure` (do not use `--insecure` in deployment)
- `--monitor` — `1`, `2`, … or `0` for the combined virtual desktop
- `--codec auto|h264|jpeg` — initial codec
- `--encoder auto|qsv|nvenc|amf|x264`
- `--ffmpeg PATH`
- `--capture auto|dxcam|mss`
- `--width`, `--quality`, `--fps`, `--kbps`
- `--dry-run`, `--log`

`relay.py`:

- `--host`, `--port`
- `--token`
- `--cert`, `--key`
- `--log`

Viewer toolbar settings are runtime configuration and can change the agent initial codec and quality settings.

## Troubleshooting and limitations

- **Viewer cannot connect** — check the relay process, token, TLS certificate, host/port, and firewall.
- **Agent cannot connect** — check the relay URL, token, and `--cafile`; certificate errors mean the agent does not trust the relay certificate.
- **H.264 is unavailable or falls back** — the browser must be in a secure context with `VideoDecoder` support, and the agent must have ffmpeg plus a working encoder.
- **`--monitor 0` uses `mss`** — this is expected; the combined virtual-desktop capture path does not use `dxcam`.
- **Locked screen, UAC, or secure desktop** — the agent runs in the interactive session and cannot capture or control these desktops. Do not disable the lock screen as a workaround.
- **Elevated windows do not respond** — a standard-user agent cannot inject input into higher-integrity windows. Administrator runtime broadens control and risk, and still does not cover secure desktops.
- **One selected view** — WebRC streams one monitor or the combined virtual desktop. There is no audio, clipboard, or file transfer.
- **Shared runtime settings** — viewers of the same agent share the selected monitor and quality settings; the most recent change applies.

## Security

WebRC uses a shared-token access model without per-user or per-PC permissions. Review these boundaries before deployment:

- The token is the whole credential. All agents and viewers connected to one relay share it.
- A trusted relay can read screenshots and input. TLS protects each hop when configured; WebRC has no end-to-end encryption.
- Agent names are labels, not authorization. Anyone with the token can connect a fake agent name or control a connected PC.
- The token appears in viewer URLs and may leak through browser history, bookmarks, proxy logs, and access logs. Runtime logs may contain sensitive input and window information.

See [SECURITY.md](SECURITY.md) for the complete threat model and reporting guidance.

## Contributing and testing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and verification. The [release checklist](docs/release-checklist.md) records completed checks and the remaining work before publication.

## License

[MIT](LICENSE)
