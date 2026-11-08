"""Finding phones on the network, without asking anyone to read out a port.

Android 11+ advertises wireless debugging over mDNS:

    _adb-tls-pairing._tcp   while the "Pair device with pairing code" dialog is open
    _adb-tls-connect._tcp   whenever wireless debugging is on
    _adb._tcp               plain ADB on port 5555 (TVs, `adb tcpip`)

`adb mdns services` can list these, but only if adb was built with mDNS
support. Debian and Ubuntu ship it compiled out, so on a lot of Linux boxes
that command silently returns nothing. This is a small DNS-SD client over
multicast UDP using nothing but the standard library, so discovery works the
same everywhere.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

MCAST_ADDR = "224.0.0.251"
MCAST_PORT = 5353

SERVICE_PAIRING = "_adb-tls-pairing._tcp.local"
SERVICE_CONNECT = "_adb-tls-connect._tcp.local"
SERVICE_PLAIN = "_adb._tcp.local"

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_SRV = 1, 12, 16, 33


@dataclass
class Found:
    service: str        # which of the three above
    instance: str       # the advertised name, usually "adb-<serial>-<random>"
    host: str = ""      # IPv4, once we resolve it
    port: int = 0

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def kind(self) -> str:
        return {
            SERVICE_PAIRING: "pairing",
            SERVICE_CONNECT: "wireless",
            SERVICE_PLAIN: "plain",
        }.get(self.service, "?")


# --- the smallest DNS codec that does the job -------------------------------


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        if len(b) > 63:
            raise ValueError(f"label too long: {label!r}")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _decode_name(buf: bytes, off: int) -> tuple[str, int]:
    """Returns (name, offset after the name). Follows compression pointers."""
    labels: list[str] = []
    jumped = False
    end = off
    hops = 0
    while True:
        if off >= len(buf):
            raise ValueError("truncated name")
        n = buf[off]
        if n == 0:
            off += 1
            if not jumped:
                end = off
            break
        if n & 0xC0 == 0xC0:          # pointer
            if off + 1 >= len(buf):
                raise ValueError("truncated pointer")
            ptr = struct.unpack_from("!H", buf, off)[0] & 0x3FFF
            if not jumped:
                end = off + 2
            off = ptr
            jumped = True
            hops += 1
            if hops > 32:             # malformed packet pointing at itself
                raise ValueError("compression loop")
            continue
        off += 1
        labels.append(buf[off:off + n].decode("utf-8", "replace"))
        off += n
    return ".".join(labels), end


def _query(names: list[str], unicast: bool = True) -> bytes:
    """A PTR query for each name.

    The top bit of QCLASS is the QU ("unicast response") bit of RFC 6762 §5.4.
    With it set, responders answer our own ephemeral socket directly. Without
    it they answer the multicast group on port 5353, which we would only hear
    if we had joined the group and bound 5353 — and on a desktop that port is
    usually already taken by avahi or mDNSResponder.
    """
    qclass = 0x8001 if unicast else 0x0001
    header = struct.pack("!HHHHHH", 0, 0, len(names), 0, 0, 0)
    body = b""
    for n in names:
        body += _encode_name(n) + struct.pack("!HH", TYPE_PTR, qclass)
    return header + body


def _parse(buf: bytes) -> tuple[dict[str, list[str]], dict[str, tuple[str, int]], dict[str, str]]:
    """-> (ptrs by service, srv by instance, addrs by hostname)"""
    ptrs: dict[str, list[str]] = {}
    srv: dict[str, tuple[str, int]] = {}
    addrs: dict[str, str] = {}
    try:
        _id, _flags, qd, an, ns, ar = struct.unpack_from("!HHHHHH", buf, 0)
    except struct.error:
        return ptrs, srv, addrs
    off = 12
    for _ in range(qd):
        try:
            _, off = _decode_name(buf, off)
            off += 4
        except (ValueError, struct.error):
            return ptrs, srv, addrs
    for _ in range(an + ns + ar):
        try:
            name, off = _decode_name(buf, off)
            rtype, _rclass, _ttl, rdlen = struct.unpack_from("!HHIH", buf, off)
            off += 10
            rdata, rend = buf[off:off + rdlen], off + rdlen
            if rtype == TYPE_PTR:
                target, _ = _decode_name(buf, off)
                ptrs.setdefault(name.rstrip("."), []).append(target.rstrip("."))
            elif rtype == TYPE_SRV:
                _prio, _weight, port = struct.unpack_from("!HHH", rdata, 0)
                target, _ = _decode_name(buf, off + 6)
                srv[name.rstrip(".")] = (target.rstrip("."), port)
            elif rtype == TYPE_A and rdlen == 4:
                addrs[name.rstrip(".")] = socket.inet_ntoa(rdata)
            off = rend
        except (ValueError, struct.error):
            break
    return ptrs, srv, addrs


# --- the public call --------------------------------------------------------


def scan(timeout: float = 3.0,
         services: tuple[str, ...] = (SERVICE_CONNECT, SERVICE_PAIRING, SERVICE_PLAIN),
         ) -> list[Found]:
    """Listen for `timeout` seconds and return whatever answered.

    Sends the query three times: multicast UDP is lossy, and a phone that has
    just had wireless debugging switched on can take a moment to start
    answering.
    """
    wanted = {s.rstrip(".") for s in services}
    ptrs: dict[str, list[str]] = {}
    srv: dict[str, tuple[str, int]] = {}
    addrs: dict[str, str] = {}

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass            # not on every platform, and not essential
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.bind(("", 0))     # an ephemeral port: we are a one-shot client, not a responder
        s.settimeout(0.3)

        pkt = _query(sorted(wanted))
        deadline = time.monotonic() + timeout
        next_send = 0.0
        sends = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send and sends < 3:
                try:
                    s.sendto(pkt, (MCAST_ADDR, MCAST_PORT))
                except OSError:
                    pass    # no route to the multicast group: nothing to find
                sends += 1
                next_send = now + 0.8
            try:
                data, _src = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            p, v, a = _parse(data)
            for k, vals in p.items():
                ptrs.setdefault(k, []).extend(x for x in vals if x not in ptrs.get(k, []))
            srv.update(v)
            addrs.update(a)
    finally:
        s.close()

    out: list[Found] = []
    for service, instances in ptrs.items():
        if service not in wanted:
            continue
        for inst in instances:
            host, port = srv.get(inst, ("", 0))
            ip = addrs.get(host, "")
            short = inst[:-len(service) - 1] if inst.endswith(service) else inst
            out.append(Found(service=service, instance=short, host=ip, port=port))
    # Resolvable entries first; they are the ones we can act on.
    out.sort(key=lambda f: (not (f.host and f.port), f.kind, f.instance))
    return out


def local_subnets() -> list[str]:
    """The /24s this machine sits on, as 'a.b.c.' prefixes.

    Found by asking the routing table which address would be used to reach a
    few private ranges. No subprocess, no parsing of `ip addr`.
    """
    prefixes: list[str] = []
    for probe in ("192.168.1.1", "192.168.29.1", "10.0.0.1", "172.16.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))        # UDP connect sends nothing
            ip = s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
        if ip.startswith("127."):
            continue
        prefix = ip.rsplit(".", 1)[0] + "."
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def sweep(prefix: str | None = None, timeout: float = 4.0,
          services: tuple[str, ...] = (SERVICE_CONNECT, SERVICE_PAIRING, SERVICE_PLAIN),
          ) -> list[Found]:
    """Ask every host on the /24 directly, instead of shouting at the group.

    Plenty of home access points drop multicast between wireless clients, or
    have client isolation on, and then `scan()` finds nothing even though the
    phone is sitting right there. A responder will still answer a unicast
    query sent straight to its port 5353, so 254 small packets finds what
    multicast cannot. It costs about 10 KB of traffic.
    """
    prefixes = [prefix] if prefix else local_subnets()
    if not prefixes:
        return []
    wanted = [s.rstrip(".") for s in services]
    pkt = _query(wanted, unicast=False)

    by_ip: dict[str, bytes] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setblocking(False)
        for p in prefixes:
            for host in range(1, 255):
                try:
                    s.sendto(pkt, (f"{p}{host}", MCAST_PORT))
                except OSError:
                    pass     # ICMP unreachable from an earlier packet; keep going
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, src = s.recvfrom(9000)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError:
                continue     # someone replied with ICMP; not fatal
            by_ip[src[0]] = data
    finally:
        s.close()

    out: list[Found] = []
    for ip, data in by_ip.items():
        ptrs, srv, addrs = _parse(data)
        for service, instances in ptrs.items():
            if service not in wanted:
                continue
            for inst in instances:
                host, port = srv.get(inst, ("", 0))
                short = inst[:-len(service) - 1] if inst.endswith(service) else inst
                out.append(Found(service=service, instance=short,
                                 host=addrs.get(host, "") or ip, port=port))
    out.sort(key=lambda f: (not (f.host and f.port), f.kind, f.instance))
    return out


def find(timeout: float = 4.0) -> list[Found]:
    """Multicast first, then the unicast sweep if that came back empty."""
    found = scan(timeout=min(timeout, 3.0))
    if found:
        return found
    return sweep(timeout=timeout)


def pairable(found: list[Found]) -> list[Found]:
    return [f for f in found if f.service == SERVICE_PAIRING and f.host and f.port]


def connectable(found: list[Found]) -> list[Found]:
    return [f for f in found
            if f.service in (SERVICE_CONNECT, SERVICE_PLAIN) and f.host and f.port]
