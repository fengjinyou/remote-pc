# Preparing a public release

Use a reviewed commit as the release source. The local deployment directory may
contain credentials, logs, certificates, and old ZIP files that do not belong in
a public repository or release archive.

## Preparation record — 2026-10-15

- The existing MIT license is retained.
- English and Chinese READMEs describe installation, deployment, controls, and
  limitations. Contribution and security guidance are included.
- Relay dependencies, Windows agent dependencies, and optional DXGI capture
  dependencies are listed separately.
- A clean Windows Python 3.13.8 virtual environment installed all three dependency
  lists successfully; `pip check` found no broken requirements.
- The relay regression suite uses loopback HTTP/WebSocket connections and mock
  agents. It does not capture a desktop or inject input.
- All 19 automated tests passed locally, including authentication, multi-PC
  routing, acknowledgements, disconnect cleanup, and credential-log redaction.
- A Chrome smoke test used the actual relay and viewer with two synthetic agents:
  JPEG rendering, switching PCs, zoom controls, and input routing were checked.
  The screenshot in the README shows generated test content.
- Python compilation and inline JavaScript syntax were checked locally.
- The original nine tracked files and the one existing historical commit were
  scanned for common credential patterns, private key headers, nonlocal IP
  literals, and personal absolute Windows paths, with no matches. This limited
  pattern check is not a comprehensive security audit.

## Before changing repository visibility

- [ ] Review the complete diff, including changes that were already uncommitted
  before this preparation. Stage only the intended source, documentation, and
  tests.
- [ ] Inspect staged filenames and content. Keep `private/`, `certs/`, token
  files, real logs, virtual environments, and deployment ZIP files local.
- [ ] Review commit metadata for any author identity information you do not want
  published. Recheck history if additional commits or branches are included.
- [ ] Push the reviewed commit and confirm the configured Windows/Linux CI matrix
  passes. Local Windows validation does not establish a Linux result.
- [ ] Enable GitHub private vulnerability reporting and confirm the reporting
  instructions in `SECURITY.md` lead to a working private channel.
- [ ] Add a concise repository description and relevant topics, such as
  `remote-desktop`, `self-hosted`, `windows`, `websocket`, and `python`.

## Before calling a release stable

- [ ] Test the documented installation on a separate Windows machine.
- [ ] Verify actual JPEG capture and input on an authorized desktop, separately
  from the mock-agent browser test.
- [ ] Verify H.264 hardware/software decode and JPEG fallback on the browsers and
  hardware you intend to support.
- [ ] Check mobile gestures, IME input, multiple monitors, reconnection, and the
  documented lock-screen/UAC limitations on actual devices.
- [ ] Verify the intended HTTPS/WSS deployment, certificate trust, and token
  rotation. No existing remote service was updated during preparation.
- [ ] Describe any remaining gaps explicitly if publishing a preview release.

## Source archive

After the intended changes are committed and reviewed, archive that exact commit.
Do not ZIP the entire working directory or reuse old deployment ZIP files.

```powershell
New-Item -ItemType Directory -Force -Path ./dist | Out-Null
git archive --format=zip --prefix=webrc/ --output=dist/webrc-source.zip HEAD
```

Inspect the resulting archive before uploading it. `git archive` includes only
the selected commit: uncommitted preparation changes will not be included.
