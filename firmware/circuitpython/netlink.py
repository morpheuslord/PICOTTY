# netlink.py — the transport to the hub over the W5100S (SPI Ethernet).
#
# Owns the WIZNET5K interface (brought up once) and one TCP client socket
# (recreated on every reconnect). This layer moves bytes only; framing lives in
# wire.py. All behavior below follows the current adafruit_wiznet5k SocketPool
# API (library >= 7.0.0), whose quirks differ from CPython sockets:
#
#   * pool.socket(AF_INET, SOCK_STREAM) — AF_INET is 3, not 2; use the constant.
#   * settimeout(0) = non-blocking. With no data, recv_into RAISES OSError(EAGAIN),
#     it does NOT return 0/b"". recv_into == 0 means the PEER CLOSED the socket.
#   * connect() raises RuntimeError on failure (reads/writes raise OSError).
#   * There is no sendall(); send() may do a partial write, so we loop.
#   * The W5100S has only 4 sockets total, so a dead socket must be close()d.
#   * DHCP leases are not auto-renewed inside recv/send; call maintain() on a timer.

import errno
import time

import board
import busio
import digitalio

from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
import adafruit_wiznet5k.adafruit_wiznet5k_socketpool as socketpool

# W5100S HAT wiring on the Pico's SPI0. Confirm against your HAT's silkscreen;
# revisions vary. INT (GP21) is unused by the polling driver.
_PIN_MISO = board.GP16
_PIN_CS = board.GP17
_PIN_SCK = board.GP18
_PIN_MOSI = board.GP19
_PIN_RST = board.GP20


class NetLink:
    def __init__(self, config):
        self._cfg = config
        self._spi = None
        self._cs = None
        self._rst = None
        self._eth = None
        self._pool = None
        self._sock = None

    # -- interface bring-up (once per boot) -----------------------------------

    def bring_up(self):
        """Initialize the W5100S and acquire an IP. Safe to call again to retry.

        The SPI bus and CS/RST pins are allocated once and reused, so a retry
        after a DHCP failure re-inits the chip without re-grabbing pins that are
        already in use.
        """
        if self._spi is None:
            self._spi = busio.SPI(_PIN_SCK, MOSI=_PIN_MOSI, MISO=_PIN_MISO)
            self._cs = digitalio.DigitalInOut(_PIN_CS)
            self._rst = digitalio.DigitalInOut(_PIN_RST)
        self._eth = WIZNET5K(
            self._spi,
            self._cs,
            reset=self._rst,
            is_dhcp=self._cfg.use_dhcp,
            mac=self._cfg.mac,
            hostname=self._cfg.node_id,
        )
        if not self._cfg.use_dhcp:
            eth = self._eth
            self._eth.ifconfig = (
                eth.unpretty_ip(self._cfg.static_ip),
                eth.unpretty_ip(self._cfg.static_subnet),
                eth.unpretty_ip(self._cfg.static_gateway or self._cfg.static_ip),
                eth.unpretty_ip(self._cfg.static_dns),
            )
        # SocketPool is a singleton per interface.
        self._pool = socketpool.SocketPool(self._eth)

    @property
    def ip_address(self):
        if self._eth is None:
            return "0.0.0.0"
        try:
            return self._eth.pretty_ip(self._eth.ip_address)
        except Exception:
            return "0.0.0.0"

    # -- per-connection socket ------------------------------------------------

    def connect(self):
        """Open a fresh TCP socket to the hub. Raises on failure.

        A bounded timeout is set for the connect itself so an unreachable hub
        fails fast instead of hanging the loop; the socket then switches to
        non-blocking for the cooperative session loop.
        """
        pool = self._pool
        sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        try:
            sock.settimeout(self._cfg.connect_timeout_s)
            sock.connect((self._cfg.hub_host, self._cfg.hub_port))
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        sock.settimeout(0)  # non-blocking for the cooperative loop
        self._sock = sock

    def send(self, data):
        """Send an entire frame, looping over partial writes (no sendall)."""
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        mv = memoryview(data)
        while mv:
            try:
                sent = sock.send(mv)
            except OSError as e:
                raise ConnectionError("send failed: %s" % e)
            if sent <= 0:
                # Transient full TX buffer; yield briefly and retry.
                time.sleep(0.001)
                continue
            mv = mv[sent:]

    def recv_into(self, scratch):
        """Read available bytes into `scratch`.

        Returns the byte count read, or None if nothing is available right now.
        Raises ConnectionError if the peer closed or the link errored.
        """
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        try:
            n = sock.recv_into(scratch)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.ETIMEDOUT):
                return None  # no data this tick — normal, not a disconnect
            raise ConnectionError("recv error: %s" % e)
        if n == 0:
            raise ConnectionError("peer closed")
        return n

    def maintain(self):
        """Renew the DHCP lease. No-op on a static-IP node."""
        if self._cfg.use_dhcp and self._eth is not None:
            try:
                self._eth.maintain_dhcp_lease()
            except Exception:
                pass

    def close(self):
        """Close the current socket so the 4-socket pool is not leaked."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
