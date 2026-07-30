# backchannel.py — the serial path to and from the target, over USB CDC data.
#
# usb_cdc.data is the dedicated data channel enabled in boot.py, separate from
# the REPL console. Its read timeout defaults to None (blocking forever), which
# would freeze the cooperative loop, so we force non-blocking reads and only ever
# read what `in_waiting` reports is present.
#
# Two directions share this one port:
#   * RX (read): the target's serial console, forwarded to the hub as `output`.
#   * TX (write): the `send` command, bytes we push into the target's serial
#     getty so the console tab becomes an interactive login. Writes are chunked,
#     non-blocking, and drained across loop passes so a large paste never stalls
#     the cooperative loop (or trips the ~8 s watchdog).
#
# Reality check: the node can only read what the target actually
# emits to this serial port. A target with no serial console configured sends
# nothing, and the node is a blind keyboard. That is a target-config fact, not a
# firmware gap.

import usb_cdc

# The CDC TX buffer is small; assume a conservative window so we never hand the
# port more than it can accept without blocking. Writes are non-blocking anyway
# (write_timeout = 0), so this only shapes how much we attempt per chunk.
_TX_WINDOW = 128


class BackChannel:
    def __init__(self, tx_bound=4096):
        self._serial = usb_cdc.data  # None if data channel wasn't enabled in boot.py
        # Pending serial-TX jobs, drained across loop passes. Each job is
        # [cmd_id, buffer, position]; a job completes when position == len(buffer).
        self._tx_queue = []
        self._tx_pending = 0        # unwritten bytes across the whole queue
        self._tx_bound = tx_bound   # hard cap on buffered-but-unsent bytes
        if self._serial is not None:
            # Never block the loop on a read or a write to a dead host.
            self._serial.timeout = 0
            try:
                self._serial.write_timeout = 0
            except Exception:
                pass

    @property
    def available(self):
        """True when this node has a usable back-channel (advertised as 'cdc')."""
        return self._serial is not None

    @property
    def can_write(self):
        """True when this node can write to the serial channel (advertised as
        'serial_tx'). Same physical port as the read path, so it is enabled
        whenever usb_cdc.data exists."""
        return self._serial is not None

    def poll_output(self, max_bytes):
        """Return up to max_bytes of pending target output as text, or None.

        Bounded per call so a chatty target cannot starve command handling.
        """
        s = self._serial
        if s is None:
            return None
        n = s.in_waiting
        if not n:
            return None
        if n > max_bytes:
            n = max_bytes
        data = s.read(n)
        if not data:
            return None
        return data.decode("utf-8", "replace")

    def read_all(self):
        """Flush and return everything currently buffered, as text (for `read`)."""
        s = self._serial
        if s is None:
            return ""
        n = s.in_waiting
        if not n:
            return ""
        data = s.read(n)
        return data.decode("utf-8", "replace") if data else ""

    # -- serial TX (the `send` command) --------------------------------------

    def queue_write(self, cmd_id, data):
        """Queue `data` (bytes) to be written to the target's serial port.

        Returns (True, None) if accepted, or (False, detail) if it cannot be
        taken: no data port, or the pending buffer is already over its bound.
        The bytes are NOT written here — drain_tx() hands them to the port across
        subsequent loop passes so a large paste never blocks. The `ok` result for
        cmd_id is emitted by drain_tx() once the whole payload has been written.
        """
        if self._serial is None:
            return False, "serial data port not enabled (boot.py usb_cdc.data)"
        if self._tx_pending + len(data) > self._tx_bound:
            return False, "serial tx backlog"
        self._tx_queue.append([cmd_id, data, 0])
        self._tx_pending += len(data)
        return True, None

    def drain_tx(self, budget):
        """Write up to `budget` bytes of pending serial-TX data this pass.

        Bounded like the RX forward cap so TX and RX never starve each other.
        Returns a list of (cmd_id, status, detail) for every job that finished
        (or errored) this pass, so the caller can send the matching results.
        """
        s = self._serial
        if s is None or not self._tx_queue:
            return None
        done = []
        left = budget
        while self._tx_queue and left > 0:
            job = self._tx_queue[0]
            cmd_id, buf, pos = job
            remaining = len(buf) - pos
            chunk = remaining if remaining < left else left
            if chunk > _TX_WINDOW:
                chunk = _TX_WINDOW
            try:
                written = s.write(buf[pos:pos + chunk])
            except Exception as e:
                # Port write error: fail this job and drop it.
                self._tx_pending -= remaining
                self._tx_queue.pop(0)
                done.append((cmd_id, "failed", "serial write error: %s" % e))
                continue
            if written is None:
                written = chunk  # some ports return None on a full non-blocking write
            if written <= 0:
                break  # TX buffer full this pass; try again next loop
            job[2] = pos + written
            self._tx_pending -= written
            left -= written
            if job[2] >= len(buf):
                self._tx_queue.pop(0)
                done.append((cmd_id, "ok", None))
        return done or None

    def reset_tx(self):
        """Drop any pending serial-TX jobs. Called on reconnect: like any other
        in-flight work, unsent bytes are abandoned when the socket drops."""
        self._tx_queue = []
        self._tx_pending = 0
