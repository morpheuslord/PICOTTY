# code.py — the node's main program.
#
# One cooperative loop, no threads. It brings up Ethernet once, then repeatedly
# dials the hub and runs a session: read hub frames and act on them, forward the
# target's serial output, and send a heartbeat on interval. On any link error it
# closes the socket, backs off, and reconnects. A hardware watchdog resets the
# node if the loop ever hangs.
#
# Hardened firmware built against the current CircuitPython and
# adafruit_wiznet5k / adafruit_hid APIs.

import gc
import time

import board
import digitalio
import supervisor

from nodeconfig import NodeConfig, ConfigError
from netlink import NetLink
from backchannel import BackChannel
from injector import Injector, InjectError
from wire import FrameReader, ProtocolError, encode
import messages

FW_VERSION = "1.1.0"

# Editing files on the CIRCUITPY drive must not yank a running node into a reload
# mid-command. We reload deliberately (on a reboot command), never by surprise.
supervisor.runtime.autoreload = False


def mono_ms():
    """Milliseconds since boot from the integer-ns clock (no float precision loss)."""
    return time.monotonic_ns() // 1_000_000


def decode_hex(s):
    """Decode a hex string like '030d' to bytes. Raises ValueError on bad input.

    Used by the `send` command's `raw` field (binary bytes travel as hex so the
    wire stays UTF-8 JSON). No bytes.fromhex on all CircuitPython builds, so this
    stays explicit and dependency-free."""
    if not isinstance(s, str):
        raise ValueError("raw must be a string")
    s = s.strip()
    if len(s) % 2 != 0:
        raise ValueError("odd-length hex")
    return bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))


def log_error(msg):
    """Append a line to /error.txt so a headless node's fault is readable by just
    plugging it into a computer. A no-op unless LOG_TO_FILE is enabled in
    settings.toml (which makes boot.py remount the filesystem writable), so this
    is always safe to call. The file self-truncates past ~32 KB."""
    try:
        import os
        try:
            size = os.stat("/error.txt")[6]
        except OSError:
            size = 0
        with open("/error.txt", "w" if size > 32768 else "a") as f:
            f.write("[+%dms] %s\n" % (mono_ms(), msg))
    except OSError:
        pass  # filesystem read-only (logging disabled) — nothing to do


class State:
    """The small amount of runtime state the hub can change while connected."""

    def __init__(self, heartbeat_ms):
        self.heartbeat_ms = heartbeat_ms


# --- status LED (the headless debug channel) ---------------------------------

class StatusLED:
    """The onboard LED as an at-a-glance status indicator for a node with no
    console. Read the node by looking at it:

      solid on      connected to the hub, healthy
      slow blink    powered + networked, but not connected to the hub (retrying)
      medium blink  Ethernet/link problem (cannot bring the network up)
      fast blink    fatal config or HID error (needs attention)
    """

    _PERIOD_MS = {"online": None, "connecting": 500, "netdown": 200, "fatal": 100}

    def __init__(self):
        self._led = None
        try:
            self._led = digitalio.DigitalInOut(board.LED)
            self._led.direction = digitalio.Direction.OUTPUT
        except Exception:
            self._led = None  # some boards differ (e.g. Pico W); degrade silently
        self._state = "connecting"

    def set(self, state):
        self._state = state

    def tick(self, now_ms):
        if self._led is None:
            return
        period = self._PERIOD_MS.get(self._state)
        self._led.value = True if period is None else ((now_ms // period) % 2 == 0)


STATUS = None  # set in main(); referenced by feed() so every loop updates the LED


def host_present():
    """True if the TARGET's USB host has enumerated us — a proxy for 'the machine
    is powered and running'. False when the node is still powered (e.g. the target
    supplies USB standby 5V) but the target itself is off or hung, which is how a
    node reports a DEAD machine while it is still alive to the hub. Returns None if
    the runtime doesn't expose it."""
    try:
        return bool(supervisor.runtime.usb_connected)
    except Exception:
        return None


def was_watchdog_reset():
    """True if this boot followed a watchdog reset (i.e. the loop hung last time)."""
    try:
        import microcontroller
        return microcontroller.cpu.reset_reason == microcontroller.ResetReason.WATCHDOG
    except Exception:
        return False


# --- watchdog ----------------------------------------------------------------

def setup_watchdog(cfg):
    if not cfg.watchdog_enabled:
        return None
    try:
        import microcontroller
        from watchdog import WatchDogMode

        wdt = microcontroller.watchdog
        if wdt is None:
            return None
        wdt.timeout = cfg.watchdog_timeout_s  # set timeout before mode
        wdt.mode = WatchDogMode.RESET
        return wdt
    except Exception as e:
        print("watchdog unavailable:", e)
        return None


def feed(wdt):
    if wdt is not None:
        wdt.feed()
    if STATUS is not None:
        STATUS.tick(mono_ms())


def sleep_feeding(seconds, wdt, net):
    """Sleep in small slices so the watchdog stays fed and the DHCP lease stays
    alive even while we are between hub connections."""
    end = time.monotonic_ns() + int(seconds * 1_000_000_000)
    while time.monotonic_ns() < end:
        feed(wdt)
        net.maintain()
        time.sleep(0.2)


def idle_forever(wdt):
    """A dead-end for unrecoverable startup errors: keep feeding the watchdog so
    the node does not reset-loop, and blink the fatal LED pattern. The error is
    also on the console for anyone who can attach one."""
    while True:
        feed(wdt)          # ticks the LED (fast/fatal blink) too
        time.sleep(0.1)


# --- HID setup ---------------------------------------------------------------

def resolve_layout(name, kbd):
    """Return (layout, resolved_name) for the requested keyboard layout code.

    "us" (or unset) uses the built-in Adafruit US layout. Any other code loads
    the community library `keyboard_layout_win_<code>` (which pairs with its own
    `keycode_win_<code>`); if that library isn't on the board we log it and fall
    back to US, so a misconfigured layout degrades to a working keyboard rather
    than a fatal HID error. The mapping character->keycode lives in these layout
    objects, which is why layout selection is a firmware choice, not hub-side."""
    code = (name or "us").strip().lower()
    if code in ("", "us", "en", "en_us"):
        from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS
        return KeyboardLayoutUS(kbd), "us"
    try:
        mod = __import__("keyboard_layout_win_" + code)
        return mod.KeyboardLayout(kbd), code
    except Exception as e:
        log_error("keyboard layout %r unavailable (%s); using US" % (code, e))
        print("keyboard layout %r unavailable (%s); using US" % (code, e))
        from adafruit_hid.keyboard_layout_us import KeyboardLayoutUS
        return KeyboardLayoutUS(kbd), "us"


def setup_hid(cfg):
    import usb_hid
    from adafruit_hid.keyboard import Keyboard

    if not usb_hid.devices:
        raise RuntimeError("no USB HID devices; did boot.py run usb_hid.enable()?")
    kbd = Keyboard(usb_hid.devices)      # blocks until the target enumerates us
    time.sleep(cfg.settle_ms / 1000)     # let the host get ready to accept input
    layout, layout_name = resolve_layout(cfg.keyboard_layout, kbd)
    return kbd, layout, layout_name


# --- command dispatch (hub -> node) ------------------------------------------

def dispatch(msg, net, injector, backchannel, state, cfg, ota=None):
    """Handle one hub->node message. Command replies (result/pong/error) go back
    down the same socket. A link error while replying propagates up to trigger a
    reconnect; a command-level failure becomes a 'failed' result, not a crash."""
    t = msg.get("type")
    cmd_id = msg.get("cmd_id")

    if t == "type":
        try:
            injector.type_text(msg.get("text", ""), msg.get("char_delay_ms"))
            net.send(encode(messages.result(cmd_id, "ok")))
        except InjectError as e:
            net.send(encode(messages.result(cmd_id, "failed", str(e))))

    elif t == "keys":
        try:
            injector.send_chord(msg.get("chord", []))
            net.send(encode(messages.result(cmd_id, "ok")))
        except InjectError as e:
            net.send(encode(messages.result(cmd_id, "failed", str(e))))

    elif t == "sequence":
        ok, detail = injector.run_sequence(
            msg.get("steps", []), msg.get("stop_on_error", False)
        )
        net.send(encode(messages.result(cmd_id, "ok" if ok else "failed", detail)))

    elif t == "read":
        net.send(encode(messages.result(cmd_id, "ok", backchannel.read_all())))

    elif t == "send":
        # Write bytes to the target's serial getty. Exactly one of data/raw.
        data = msg.get("data")
        raw = msg.get("raw")
        has_data = isinstance(data, str)
        has_raw = isinstance(raw, str)
        if has_data == has_raw:  # both present or both absent
            net.send(encode(messages.error(
                "send requires exactly one of 'data' or 'raw'", cmd_id)))
        else:
            try:
                payload = decode_hex(raw) if has_raw else data.encode("utf-8")
            except ValueError as e:
                net.send(encode(messages.result(cmd_id, "failed", "bad raw hex: %s" % e)))
                return
            if not payload:
                # Nothing to write (e.g. data=""); it is already "handed off".
                net.send(encode(messages.result(cmd_id, "ok")))
                return
            ok, detail = backchannel.queue_write(cmd_id, payload)
            if not ok:
                net.send(encode(messages.result(cmd_id, "failed", detail)))
            # else: the 'ok' result is sent by run_session once fully drained.

    elif t == "ping":
        net.send(encode(messages.pong(msg.get("nonce"))))

    elif t == "config":
        hb = msg.get("heartbeat_ms")
        if isinstance(hb, int) and hb > 0:
            state.heartbeat_ms = hb
        # config carries no cmd_id and expects no result.

    elif t == "reboot":
        try:
            net.send(encode(messages.bye()))
        except Exception:
            pass
        net.close()
        # Soft reload: re-runs code.py (fresh sockets/buffers) WITHOUT re-enumerating
        # USB, so the target keeps seeing our keyboard and serial port.
        supervisor.reload()

    elif t == "ota_begin":
        if ota is None or not ota.available:
            net.send(encode(messages.result(cmd_id, "failed", "ota not available")))
        else:
            try:
                detail = ota.begin(msg)
                net.send(encode(messages.result(cmd_id, "ok", detail)))
            except Exception as e:
                net.send(encode(messages.result(cmd_id, "failed", "ota_begin: %s" % e)))

    elif t == "ota_chunk":
        if ota is None:
            net.send(encode(messages.result(cmd_id, "failed", "ota not available")))
        else:
            try:
                ota.chunk(msg, decode_hex)
                net.send(encode(messages.result(cmd_id, "ok")))
            except Exception as e:
                net.send(encode(messages.result(cmd_id, "failed", "ota_chunk: %s" % e)))

    elif t == "ota_commit":
        if ota is None:
            net.send(encode(messages.result(cmd_id, "failed", "ota not available")))
        else:
            try:
                detail = ota.commit(msg)
            except Exception as e:
                net.send(encode(messages.result(cmd_id, "failed", "ota_commit: %s" % e)))
            else:
                # Ack success BEFORE reloading, so the hub records the commit,
                # then soft-reload onto the new firmware (which reconnects and,
                # once healthy, finalizes to drop the .bak set).
                net.send(encode(messages.result(cmd_id, "ok", detail)))
                try:
                    net.send(encode(messages.bye()))
                except Exception:
                    pass
                net.close()
                supervisor.reload()

    else:
        if cmd_id is not None:
            net.send(encode(messages.result(cmd_id, "failed", "unknown command type: %r" % t)))
        else:
            net.send(encode(messages.error("unknown message type: %r" % t)))


# --- session loop ------------------------------------------------------------

def run_session(net, reader, injector, backchannel, state, cfg, wdt, ota=None):
    """Run until the connection drops (which surfaces as a raised exception)."""
    scratch = bytearray(512)
    mv = memoryview(scratch)

    now = time.monotonic_ns()
    last_hb = now
    last_lease = now
    last_gc = now

    while True:
        feed(wdt)
        did_work = False

        # 1) Inbound hub frames.
        n = net.recv_into(scratch)
        if n:
            reader.feed(mv[:n])
            while True:
                try:
                    incoming = reader.pop()
                except ProtocolError as e:
                    try:
                        net.send(encode(messages.error("protocol error: %s" % e)))
                    except Exception:
                        pass
                    raise ConnectionError("protocol error")
                if incoming is None:
                    break
                dispatch(incoming, net, injector, backchannel, state, cfg, ota)
                did_work = True

        # 2) Outbound target serial output, bounded per pass.
        out = backchannel.poll_output(cfg.output_chunk_max)
        if out:
            net.send(encode(messages.output(out, mono_ms())))
            did_work = True

        # 2b) Drain pending serial-TX (from `send`) toward the target, bounded
        # per pass just like the RX forward above so neither starves the other.
        # A finished job yields its deferred 'ok' (or 'failed') result now.
        finished = backchannel.drain_tx(cfg.serial_tx_budget)
        if finished:
            for cid, status, detail in finished:
                net.send(encode(messages.result(cid, status, detail)))
            did_work = True

        now = time.monotonic_ns()

        # 3) Heartbeat on interval (interval may have changed via a config frame).
        if now - last_hb >= state.heartbeat_ms * 1_000_000:
            # Carry target-machine liveness (USB host present) so the hub can show
            # whether the attached MACHINE is up, distinct from the node itself.
            net.send(encode(messages.heartbeat(cfg.node_id, host_present())))
            last_hb = now
            # Reaching a heartbeat means we booted, networked, connected, and ran
            # the loop — healthy enough to finalize a pending OTA update (drop the
            # marker + .bak set). Idempotent and a cheap no-op when nothing pends.
            if ota is not None:
                try:
                    ota.finalize()
                except Exception:
                    pass

        # 4) DHCP lease upkeep (no-op on a static node).
        if cfg.use_dhcp and now - last_lease >= cfg.dhcp_maintain_ms * 1_000_000:
            net.maintain()
            last_lease = now

        # 5) Periodic GC at a quiet point keeps the heap compact for the long run.
        if now - last_gc >= cfg.gc_interval_ms * 1_000_000:
            gc.collect()
            last_gc = now

        # Idle-only pause: no delay when there is work, a small yield when idle so
        # we neither busy-spin nor add latency under load.
        if not did_work:
            time.sleep(cfg.loop_idle_ms / 1000)


# --- entry point -------------------------------------------------------------

def main():
    global STATUS
    print("swarm node: starting")
    STATUS = StatusLED()
    if was_watchdog_reset():
        log_error("boot after WATCHDOG reset (previous run hung)")

    try:
        cfg = NodeConfig()
    except ConfigError as e:
        print("CONFIG ERROR:", e)
        print("edit settings.toml on the CIRCUITPY drive, then reset.")
        log_error("CONFIG ERROR: %s" % e)
        STATUS.set("fatal")
        idle_forever(None)
        return

    # The watchdog is deliberately NOT armed yet. HID setup blocks until the USB
    # host enumerates us, and DHCP can be slow; those are legitimate startup waits
    # that could exceed the ~8 s watchdog and cause a reset loop. We arm the
    # watchdog only once the network is up, to guard the steady session loop.
    wdt = None

    try:
        kbd, layout, layout_name = setup_hid(cfg)
    except Exception as e:
        print("HID ERROR:", e)
        log_error("HID ERROR: %s" % e)
        STATUS.set("fatal")
        idle_forever(None)
        return

    injector = Injector(kbd, layout, cfg.char_delay_ms)
    backchannel = BackChannel(cfg.serial_tx_bound)
    # 'cdc' = can read the target's console; 'serial_tx' = can write into it
    # (the `send` command). Both need usb_cdc.data, so they travel together, but
    # they are separate flags so the hub/dashboard can tell old firmware apart.
    cap = ["hid"]
    if backchannel.available:
        cap.append("cdc")
    if backchannel.can_write:
        cap.append("serial_tx")

    # OTA: only offered when enabled AND the node can actually flash (writable
    # filesystem + sha256 lib). The capability gate is what stops the hub ever
    # pushing firmware to a node that can't safely receive it.
    ota = None
    if cfg.ota_enabled:
        try:
            from otaflash import OTA
            ota = OTA()
            if ota.available:
                cap.append("ota")
            else:
                log_error("OTA_ENABLED but filesystem read-only or no adafruit_hashlib")
        except Exception as e:
            log_error("OTA init failed: %s" % e)
            ota = None

    net = NetLink(cfg)
    STATUS.set("netdown")
    eth_logged = False
    while True:
        try:
            net.bring_up()
            break
        except Exception as e:
            print("ethernet init failed:", e)
            if not eth_logged:
                log_error("ethernet init failed: %s" % e)
                eth_logged = True
            sleep_feeding(3, None, net)

    print("node", cfg.node_id, "ip", net.ip_address, "cap", cap)
    gc.collect()

    # Network is up; now arm the watchdog to catch a hung steady loop.
    wdt = setup_watchdog(cfg)

    state = State(cfg.heartbeat_ms)
    reader = FrameReader(cfg.max_frame_bytes)
    backoff = cfg.backoff_start_ms
    boot_watchdog = was_watchdog_reset()
    reported_reset = False

    while True:
        try:
            STATUS.set("connecting")
            feed(wdt)
            print("connecting to hub", cfg.hub_host, cfg.hub_port)
            net.connect()
            reader.reset()
            backchannel.reset_tx()  # abandon any serial writes left from a dropped link
            net.send(encode(messages.hello(cfg.node_id, cfg.token, FW_VERSION, cap, layout_name)))
            print("connected; hello sent")
            if boot_watchdog and not reported_reset:
                # Surface a prior hang in the hub's Events feed, since there is no
                # console to see it on a deployed node.
                net.send(encode(messages.error("recovered from watchdog reset")))
                reported_reset = True
            backoff = cfg.backoff_start_ms  # reset backoff on a good connect
            STATUS.set("online")
            run_session(net, reader, injector, backchannel, state, cfg, wdt, ota)
        except (ConnectionError, OSError, RuntimeError, ProtocolError) as e:
            print("link down:", e)
        finally:
            net.close()
        STATUS.set("connecting")
        sleep_feeding(backoff / 1000, wdt, net)
        backoff = min(backoff * 2, cfg.backoff_max_ms)


try:
    main()
except Exception as e:
    # Record an otherwise-invisible crash before CircuitPython halts.
    try:
        log_error("crash in main: %r" % (e,))
    except Exception:
        pass
    raise
