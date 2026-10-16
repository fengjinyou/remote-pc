# Contributing to WebRC

Focused bug reports, documentation improvements, and small pull requests are welcome.

## Development setup

Fork and clone the repository, then open PowerShell in your checkout. The agent needs an interactive Windows desktop and Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Optional capture dependencies are installed separately:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

For H.264, install ffmpeg separately and make it available on `PATH`. A relay-only environment needs only [requirements-relay.txt](requirements-relay.txt).

## Verification

Run the tests and syntax checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile agent.py relay.py
```

The tests use synthetic inputs and do not capture a desktop or inject keyboard/mouse input. The syntax check compiles files without importing or running the application.

For manual testing, start with the [local dry-run](README.md#local-quickstart) on a desktop you are authorized to access. `--dry-run` still captures and transmits the desktop; it logs input instead of injecting it. Use a test desktop without sensitive content, and only enable real input when you intend to control that desktop.

## Useful bug reports

Provide the smallest reproduction, expected and actual results, and relevant environment details:

- Windows and Python versions; whether the agent ran as a standard user or administrator.
- Browser/version and whether the page used HTTP, HTTPS, or localhost.
- Capture backend, codec, monitor selection, and ffmpeg version/encoder for H.264 issues.
- Relay operating system and TLS setup.

Share short diagnostic excerpts or screenshots when useful, removing tokens, personal information, and private configuration first. Do not commit credentials, private keys, token files, or personal deployment files.

## Pull requests

Keep changes focused. Describe the problem, resulting behavior, and the checks you actually performed. Include environment details for manual results and update both READMEs when user-facing instructions change.

For security reports, follow [SECURITY.md](SECURITY.md). Before publishing a release, use the [release checklist](docs/release-checklist.md).
