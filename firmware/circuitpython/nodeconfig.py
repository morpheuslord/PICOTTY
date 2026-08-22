# nodeconfig.py — load and validate node configuration from settings.toml.
#
# os.getenv() returns strings only (or None when missing/malformed), so every
# non-string value is coerced here. Required keys raise ConfigError so a
# misconfigured node fails loudly at startup instead of silently misbehaving.

import os


class ConfigError(Exception):
    pass


def _str(key, default=None, required=False):
    v = os.getenv(key)
    if v is None or v == "":
        if required:
            raise ConfigError("missing required setting: " + key)
        return default
    return v


def _int(key, default):
    v = os.getenv(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(key, default):
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _derive_mac(node_id):
    """A stable, unique, locally-administered MAC per node id.

    Nodes must not share a MAC on the management VLAN. We hash the node id
    (FNV-1a) into the lower 4 octets and set the locally-administered bit.
    """
    h = 2166136261
    for b in node_id.encode("utf-8"):
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return bytes([0x02, 0x00, (h >> 24) & 0xFF, (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF])


def _parse_mac(text):
    parts = text.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ConfigError("NODE_MAC must be six hex octets")
    return bytes(int(p, 16) for p in parts)


class NodeConfig:
    def __init__(self):
        # Identity
        self.node_id = _str("NODE_ID", required=True)
        self.token = _str("NODE_TOKEN", required=True)

        # Hub location (HUB_IP preferred; HUB_HOST accepted as an alias).
        # hub_host/hub_port are the ACTIVE target the transport dials; the
        # HubSelector (code.py) points them at whichever hub it has chosen. They
        # start on the primary so single-hub nodes behave exactly as before.
        self.hub_host = _str("HUB_IP") or _str("HUB_HOST", required=True)
        self.hub_port = _int("HUB_PORT", 9000)

        # Optional BACKUP hub for failover. When HUB_IP_BACKUP (or
        # HUB_HOST_BACKUP) is set, the node prefers the primary and fails over to
        # the backup when the primary is unreachable; a hub can also redirect the
        # node at runtime (see HubSelector). Labels are for display/telemetry.
        # Absent -> a single-element list, i.e. today's single-hub behavior.
        self.hub_label = _str("HUB_LABEL", "primary")
        self.hubs = [{"label": self.hub_label, "host": self.hub_host, "port": self.hub_port}]
        backup_host = _str("HUB_IP_BACKUP") or _str("HUB_HOST_BACKUP")
        if backup_host:
            self.hubs.append({
                "label": _str("HUB_LABEL_BACKUP", "backup") or "backup",
                "host": backup_host,
                "port": _int("HUB_PORT_BACKUP", self.hub_port),
            })
        # Failback policy: "sticky" (default) stays on whichever hub is working;
        # "preemptive" retries the most-preferred hub first on every reconnect.
        self.hub_failback = (_str("HUB_FAILBACK", "sticky") or "sticky").strip().lower()
        # Consecutive failures to reach the current hub before rotating to the next.
        self.hub_failover_tries = _int("HUB_FAILOVER_TRIES", 2)

        # Networking
        self.static_ip = _str("STATIC_IP")
        self.static_subnet = _str("STATIC_SUBNET", "255.255.255.0")
        self.static_gateway = _str("STATIC_GATEWAY")
        self.static_dns = _str("STATIC_DNS", "8.8.8.8")
        self.use_dhcp = self.static_ip is None

        mac_override = _str("NODE_MAC")
        self.mac = _parse_mac(mac_override) if mac_override else _derive_mac(self.node_id)

        # Timing / behavior
        self.heartbeat_ms = _int("HEARTBEAT_MS", 5000)
        self.char_delay_ms = _int("CHAR_DELAY_MS", 0)
        self.settle_ms = _int("KEYBOARD_SETTLE_MS", 1000)
        # Keyboard layout the target expects. "us" uses the built-in Adafruit
        # layout; any other code (de/uk/fr/...) loads a matching community layout
        # library, falling back to US at runtime if that library isn't staged.
        # Absent -> "us", so an old settings.toml keeps typing exactly as before.
        self.keyboard_layout = (_str("KEYBOARD_LAYOUT", "us") or "us").strip().lower()
        self.backoff_start_ms = _int("RECONNECT_BACKOFF_START_MS", 1000)
        self.backoff_max_ms = _int("RECONNECT_BACKOFF_MAX_MS", 30000)
        # Dead-hub detection: if no frame arrives from the hub within this window,
        # treat the link as dead and reconnect — even if our own sends still
        # "succeed" into the local TX buffer (a half-open connection). Relies on
        # the hub pinging us periodically (hub >= this firmware's matching hub);
        # keep it a few ping intervals wide. Set to 0 to disable the check.
        self.hub_timeout_ms = _int("HUB_TIMEOUT_MS", 20000)
        # Bound how long a single send may stall on a full TX buffer before we
        # call the link dead. Keeps a wedged socket from spinning until the
        # watchdog resets the whole node; stays under the watchdog timeout.
        self.send_max_wait_ms = _int("SEND_MAX_WAIT_MS", 2000)
        # After this many back-to-back failed connect attempts (never reached the
        # hub), re-initialize the interface to re-acquire DHCP. Recovers a node
        # that powered up before its DHCP server (e.g. after a site power cut) or
        # whose lease went invalid. 0 disables the re-acquire.
        self.rebind_after_failures = _int("REBIND_AFTER_FAILURES", 5)
        # Bound the connect() call so a down hub can't hang the loop (or, with the
        # watchdog armed, trigger a resetting reconnect storm). Keep it under the
        # watchdog timeout.
        self.connect_timeout_s = _int("CONNECT_TIMEOUT_S", 5)
        self.max_frame_bytes = _int("MAX_FRAME_BYTES", 16384)
        self.output_chunk_max = _int("OUTPUT_CHUNK_MAX", 1024)
        # Serial-TX (the `send` command). Bound caps buffered-but-unsent bytes so
        # a large paste cannot grow memory; budget caps bytes written per loop so
        # draining never starves RX forwarding or trips the watchdog.
        self.serial_tx_bound = _int("SERIAL_TX_BOUND", 4096)
        self.serial_tx_budget = _int("SERIAL_TX_BUDGET", 1024)
        self.dhcp_maintain_ms = _int("DHCP_MAINTAIN_MS", 1000)
        self.gc_interval_ms = _int("GC_INTERVAL_MS", 1000)
        self.loop_idle_ms = _int("LOOP_IDLE_MS", 5)

        # Over-the-air firmware updates. Needs a filesystem writable to
        # CircuitPython, which means boot.py hides the USB drive (production
        # posture). A node only advertises the `ota` capability when this is on
        # AND the write probe + sha256 lib actually succeed at runtime.
        self.ota_enabled = _bool("OTA_ENABLED", False)

        # Watchdog
        self.watchdog_enabled = _bool("WATCHDOG_ENABLED", True)
        self.watchdog_timeout_s = _int("WATCHDOG_TIMEOUT_S", 8)
        if self.watchdog_timeout_s > 8:
            # RP2040 hardware limit is ~8.3 s (erratum RP2040-E1).
            self.watchdog_timeout_s = 8
