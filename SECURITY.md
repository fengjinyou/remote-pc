# Security policy

## Scope

WebRC is a self-hosted browser remote desktop for Windows. Its access model uses one shared token and a trusted relay, with the boundaries described below. Use it only on systems you are authorized to control.

## Threat model

### Shared token grants full control

The relay token is the complete access credential. Every agent and viewer connected to the same relay uses that credential:

- There is no per-user authorization.
- There is no per-PC authorization.
- All relay-connected PCs share the same credential.
- Anyone with the token can control any connected PC.
- Anyone with the token can connect an agent using a fake agent name; agent names are labels, not identity or authorization.
- A leaked viewer URL grants the same control as the token itself.

Assume compromise of the token is compromise of every connected Windows interactive session.

### Trusted relay sees everything

The relay forwards screen frames and input messages. It can read screenshots and remote input. TLS protects each connection hop when configured, but WebRC does not provide end-to-end encryption between viewer and agent. The relay, and anyone who compromises it, has access to the stream.

Do not treat TLS alone as an authorization boundary. TLS verifies transport identity and confidentiality per hop; it does not make the relay operator trusted.

### Windows desktop and elevation boundaries

The agent runs in an interactive logged-in Windows session:

- It does not support the secure desktop, UAC prompts, or the lock screen.
- It cannot capture or control those desktops.
- A standard-user agent cannot inject input into elevated windows because of Windows UIPI.
- Running the agent as administrator broadens control and the impact of compromise; it still cannot control the secure desktop or UAC prompts.
- Do not run WebRC against devices you are not authorized to control.

Do not disable the lock screen to work around these limitations. Use an appropriate, supported remote-access path when you must unlock or respond to a secure prompt locally.

## Secrets, certificates, and logs

Do not commit tokens, certificates, private keys, personal configurations, environment credentials, or real logs.

- Viewer URLs contain the token. It may appear in browser history, bookmarks, proxy logs, and web access logs.
- Relay and agent runtime logs may contain sensitive input, window titles, hostnames, addresses, or diagnostic details.
- If `--token` is omitted, the relay generates or reuses `relay.token` beside `relay.py`. Read that file locally; startup logs omit the token. An existing empty token file is rejected.
- Store private keys and `relay.token` outside source control, and restrict filesystem access to the relay service account.
- Use HTTPS/WSS for deployment. Do not use `--insecure` and do not bypass browser certificate warnings.
- For a private CA or self-signed certificate, verify the certificate fingerprint out of band before trusting it with `--cafile`.

## Token rotation

To rotate the shared token:

1. Stop the relay.
2. Replace an explicitly configured `--token` with a new high-entropy value. If using the token file instead, remove `relay.token` and restart the relay to generate a new value.
3. Read the replacement value locally and reconfigure every authorized agent and viewer.
4. Restart affected processes so old credentials are no longer accepted.
5. Treat every old URL, bookmark, log line, proxy log, and access log containing the previous token as sensitive.

Rotation does not provide per-user or per-PC isolation; the replacement token is still shared by every peer.

## Reporting a vulnerability

Do not open a public issue with exploit details, tokens, screenshots, hostnames, logs, or other private data.

If GitHub private vulnerability reporting is enabled, use **Security → Advisories → Report a vulnerability** ([GitHub instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)). If it is unavailable, open a public issue that only asks maintainers to enable private vulnerability reporting; do not disclose the vulnerability details in that issue. Report the details privately once private reporting is available.

No separate security contact email is currently published.
