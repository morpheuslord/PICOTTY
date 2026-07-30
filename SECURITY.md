# Security Policy

PICOTTY is a homelab management tool: its nodes inject USB-HID keystrokes into
machines and read their serial consoles, coordinated by a network-reachable hub.
That makes both the **software supply chain** and the **deployment posture**
security-relevant, so please read this before reporting.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Use GitHub's private reporting instead:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability** (GitHub Private Vulnerability Reporting).
3. Include a description, affected component (firmware / hub / dashboard /
   scripts), reproduction steps, and impact.

If private reporting is not available to you, open a minimal issue that says only
"security report — please enable a private channel" without any details, and a
maintainer will follow up.

**What to expect:** an acknowledgement within a few days, an assessment of
severity and scope, and — for confirmed issues — a fix and a coordinated
disclosure once a patch is available. This is a volunteer-maintained homelab
project, so timelines are best-effort, not contractual.

## Supported versions

This project is pre-1.0 and moves on the `main` branch. Security fixes land on
`main`; there is no back-port stream. Run recent `main`.

| Version | Supported |
|---|---|
| `main` (latest) | ✅ |
| older commits / tags | ❌ |

## Scope

In scope for a vulnerability report:

- The hub's network-facing surfaces: the swarm TCP server (`:9000`), the REST API
  and WebSocket (`:8080`), and how the shared node token is validated and stored.
- Node token handling and the wire protocol (framing, parsing, resource bounds).
- The node firmware's input handling (frame parsing, buffer bounds).
- The deployment scripts (`hub/scripts/`, `firmware/scripts/`, `target-setup/`).

Explicitly **out of scope** (these are documented design properties, not bugs):

- The `:9000` link is unencrypted by design — it relies on an isolated management
  VLAN. Tunnel it if it must cross an untrusted network.
- The `hello` token is a *second line* behind network isolation, not a substitute
  for it.
- The dashboard has optional, off-by-default auth; the design assumes the hub is
  reached through a private tunnel, not exposed to the internet.

## Responsible-use expectation

PICOTTY types into and reads from whatever machine a node is plugged into. It is
built to manage **your own** hardware on a **network you control**. Do not use it
against systems you are not authorised to administer. Reports describing misuse
against third-party systems are not "vulnerabilities" and will be closed.

## Hardening checklist for operators

- Keep the hub and nodes on an isolated management VLAN; never expose `:8080` or
  `:9000` to the internet — reach the dashboard through a VPN/tunnel.
- Rotate the node token (`POST /api/settings/token/rotate`) and keep
  `private/` (which holds the token and per-node config) out of version control.
- Prefer static IPs / DHCP reservations on the management segment.
- Treat `private/hub-token.txt` and each node's `settings.toml` as secrets.
