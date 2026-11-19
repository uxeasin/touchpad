#!/usr/bin/env python3
"""netbond: use several routers / internet lines at the same time on a Mac.

Runs a local proxy (SOCKS5 + HTTP on one port). Every new connection is sent
out through one of your network interfaces (each one plugged into, or joined
to, a different router), picking the least busy healthy one. Interfaces whose
internet goes down are skipped automatically until they come back.

Standard library only. See README.md in this folder.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# macOS: pin a socket to an interface (from <netinet/in.h>).
IP_BOUND_IF = 25
# Linux fallback (needs root).
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)

SKIP_PREFIXES = ("lo", "utun", "awdl", "llw", "bridge", "gif", "stf", "anpi", "ap", "docker", "veth", "br-", "tun", "tap")

CHECK_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443)]


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------

@dataclass
class Link:
    name: str
    ip: str
    weight: int = 1
    healthy: bool = True
    latency_ms: float | None = None
    active: int = 0
    total: int = 0
    failures: int = 0
    streak: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    last_bytes: int = 0
    rate: float = 0.0  # bytes/s, smoothed

    @property
    def index(self) -> int:
        return socket.if_nametoindex(self.name)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_interfaces() -> dict[str, str]:
    """Return {interface: IPv4 address} for usable, up interfaces."""
    found: dict[str, str] = {}
    if IS_MAC:
        out = _run(["ifconfig"])
        current = None
        active = {}
        for line in out.splitlines():
            m = re.match(r"^([a-zA-Z0-9]+): flags=", line)
            if m:
                current = m.group(1)
                continue
            if current is None:
                continue
            m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if m and current not in found:
                found[current] = m.group(1)
            m = re.match(r"^\s+status: (\w+)", line)
            if m:
                active[current] = m.group(1) == "active"
        found = {k: v for k, v in found.items() if active.get(k, True)}
    else:
        out = _run(["ip", "-4", "-o", "addr", "show"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                found.setdefault(parts[1], parts[3].split("/")[0])
    return {
        name: ip
        for name, ip in found.items()
        if not name.startswith(SKIP_PREFIXES)
        and not ipaddress.ip_address(ip).is_loopback
        and not ipaddress.ip_address(ip).is_link_local
    }


def gateway_for(name: str) -> str | None:
    if IS_MAC:
        out = _run(["route", "-n", "get", "-ifscope", name, "default"])
        m = re.search(r"gateway:\s*(\S+)", out)
        return m.group(1) if m else None
    out = _run(["ip", "route", "show", "default", "dev", name])
    m = re.search(r"via (\S+)", out)
    return m.group(1) if m else None


def bound_socket(link: Link, family: int = socket.AF_INET) -> socket.socket:
    """A non-blocking TCP socket that can only leave through `link`."""
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if IS_MAC:
            s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, link.index)
        elif IS_LINUX:
            try:
                s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, link.name.encode())
            except PermissionError:
                pass  # without root, fall back to source-address binding only
        s.bind((link.ip, 0))
        s.setblocking(False)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        s.close()
        raise
    return s


async def open_via(link: Link, host: str, port: int, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    last_err: Exception | None = None
    for *_, addr in infos[:3]:
        s = bound_socket(link)
        try:
            await asyncio.wait_for(loop.sock_connect(s, addr), timeout)
            return await asyncio.open_connection(sock=s)
        except Exception as e:  # try next address
            s.close()
            last_err = e
    raise last_err or OSError(f"cannot resolve {host}")


# --------------------------------------------------------------------------
# Balancer
# --------------------------------------------------------------------------

class Balancer:
    def __init__(self, links: list[Link], sticky_seconds: float):
        self.links = links
        self.sticky_seconds = sticky_seconds
        self.sticky: dict[str, tuple[Link, float]] = {}

    def healthy(self) -> list[Link]:
        up = [l for l in self.links if l.healthy]
        return up or self.links  # if all look down, still try

    def pick(self, host: str, exclude: tuple = ()) -> Link | None:
        candidates = [l for l in self.healthy() if all(l is not x for x in exclude)]
        if not candidates:
            return None
        if self.sticky_seconds > 0:
            hit = self.sticky.get(host)
            if hit and hit[0] in candidates and time.monotonic() - hit[1] < self.sticky_seconds:
                link = hit[0]
                self.sticky[host] = (link, time.monotonic())
                return link
        # Fewest open connections per unit of weight first; then spread new
        # connections in proportion to weight (weighted round-robin).
        link = min(candidates, key=lambda l: (l.active / max(l.weight, 1), (l.total + 1) / max(l.weight, 1)))
        if self.sticky_seconds > 0:
            self.sticky[host] = (link, time.monotonic())
        return link

    async def connect(self, host: str, port: int):
        tried: list[Link] = []
        last_err: Exception | None = None
        for _ in range(len(self.links)):
            link = self.pick(host, tuple(tried))
            if link is None:
                break
            tried.append(link)
            try:
                reader, writer = await open_via(link, host, port)
                link.streak = 0
                return link, reader, writer
            except Exception as e:
                last_err = e
                link.failures += 1
                link.streak += 1
                if link.streak >= 3 and link.healthy:
                    link.healthy = False  # the health check will bring it back
                    log(f"{link.name} keeps failing, skipping it until it recovers")
                self.sticky.pop(host, None)
        raise last_err or OSError("no interface available")

    async def health_loop(self, interval: float):
        while True:
            await asyncio.gather(*(self.check(l) for l in self.links))
            await asyncio.sleep(interval)

    async def check(self, link: Link):
        for host, port in CHECK_HOSTS:
            start = time.monotonic()
            try:
                _, w = await asyncio.wait_for(open_via(link, host, port, timeout=3), 4)
                w.close()
                link.latency_ms = (time.monotonic() - start) * 1000
                if not link.healthy:
                    log(f"{link.name} is back online")
                link.healthy = True
                return
            except Exception:
                continue
        if link.healthy:
            log(f"{link.name} lost internet, skipping it until it recovers")
        link.healthy = False
        link.latency_ms = None


# --------------------------------------------------------------------------
# Proxy (SOCKS5 and HTTP on the same port)
# --------------------------------------------------------------------------

async def pipe(reader, writer, link: Link, up: bool):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            if up:
                link.bytes_up += len(data)
            else:
                link.bytes_down += len(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def relay(link: Link, c_r, c_w, r_r, r_w):
    link.active += 1
    link.total += 1
    try:
        await asyncio.gather(pipe(c_r, r_w, link, True), pipe(r_r, c_w, link, False))
    finally:
        link.active -= 1


async def handle_socks(bal: Balancer, r, w):
    n = (await r.readexactly(1))[0]
    await r.readexactly(n)  # methods
    w.write(b"\x05\x00")  # no auth
    await w.drain()
    ver, cmd, _, atyp = await r.readexactly(4)
    if atyp == 1:
        host = socket.inet_ntoa(await r.readexactly(4))
    elif atyp == 3:
        host = (await r.readexactly((await r.readexactly(1))[0])).decode()
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, await r.readexactly(16))
    else:
        return
    port = struct.unpack("!H", await r.readexactly(2))[0]
    if cmd != 1:  # only CONNECT
        w.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
        await w.drain()
        return
    try:
        link, rr, rw = await bal.connect(host, port)
    except Exception:
        w.write(b"\x05\x04\x00\x01" + b"\x00" * 6)
        await w.drain()
        return
    w.write(b"\x05\x00\x00\x01" + socket.inet_aton(link.ip) + b"\x00\x00")
    await w.drain()
    await relay(link, r, w, rr, rw)


async def handle_http(bal: Balancer, first: bytes, r, w):
    head = first + await r.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, version = lines[0].split(" ", 2)
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        try:
            link, rr, rw = await bal.connect(host, int(port))
        except Exception:
            w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await w.drain()
            return
        w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await w.drain()
        await relay(link, r, w, rr, rw)
        return

    # Plain HTTP: http://host[:port]/path
    m = re.match(r"^http://([^/:]+|\[[^\]]+\])(?::(\d+))?(/.*)?$", target)
    if not m:
        w.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await w.drain()
        return
    host, port, path = m.group(1).strip("[]"), int(m.group(2) or 80), m.group(3) or "/"
    headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-connection:", "proxy-authorization:"))]
    try:
        link, rr, rw = await bal.connect(host, port)
    except Exception:
        w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await w.drain()
        return
    rw.write(f"{method} {path} {version}\r\n".encode() + "\r\n".join(headers).encode("latin-1") + b"\r\n\r\n")
    await relay(link, r, w, rr, rw)


async def handle_client(bal: Balancer, r, w):
    try:
        first = await r.readexactly(1)
        if first == b"\x05":
            await handle_socks(bal, r, w)
        else:
            await handle_http(bal, first, r, w)
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, asyncio.LimitOverrunError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# macOS system proxy
# --------------------------------------------------------------------------

def network_services() -> list[str]:
    out = _run(["networksetup", "-listallnetworkservices"])
    return [l for l in out.splitlines()[1:] if l and not l.startswith("*")]


def set_system_proxy(port: int, on: bool) -> bool:
    ok = True
    for svc in network_services():
        if on:
            cmds = [
                ["networksetup", "-setsocksfirewallproxy", svc, "127.0.0.1", str(port)],
                ["networksetup", "-setwebproxy", svc, "127.0.0.1", str(port)],
                ["networksetup", "-setsecurewebproxy", svc, "127.0.0.1", str(port)],
            ]
        else:
            cmds = [
                ["networksetup", "-setsocksfirewallproxystate", svc, "off"],
                ["networksetup", "-setwebproxystate", svc, "off"],
                ["networksetup", "-setsecurewebproxystate", svc, "off"],
            ]
        for cmd in cmds:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 or "rror" in res.stdout:
                ok = False
    return ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


async def stats_loop(links: list[Link], interval: float):
    while True:
        await asyncio.sleep(interval)
        parts = []
        for l in links:
            total = l.bytes_up + l.bytes_down
            l.rate = 0.6 * l.rate + 0.4 * (total - l.last_bytes) / interval
            l.last_bytes = total
            state = "up" if l.healthy else "DOWN"
            parts.append(f"{l.name}[{state}] {human(l.rate)}/s, {l.active} open")
        combined = sum(l.rate for l in links)
        log(" | ".join(parts) + f"  => total {human(combined)}/s")


async def public_ip(link: Link) -> str:
    try:
        r, w = await open_via(link, "api.ipify.org", 80, timeout=5)
        w.write(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return data.split(b"\r\n\r\n", 1)[-1].decode().strip() or "?"
    except Exception as e:
        return f"error ({type(e).__name__})"


async def cmd_list(args):
    ifaces = detect_interfaces()
    if not ifaces:
        print("No usable network interfaces found.")
        return
    links = [Link(n, ip) for n, ip in ifaces.items()]
    bal = Balancer(links, 0)
    await asyncio.gather(*(bal.check(l) for l in links))
    ips = await asyncio.gather(*(public_ip(l) for l in links))
    print(f"{'Interface':<10} {'Local IP':<16} {'Router':<16} {'Internet':<10} {'Latency':<9} Public IP")
    for l, pip in zip(links, ips):
        lat = f"{l.latency_ms:.0f} ms" if l.latency_ms else "-"
        print(f"{l.name:<10} {l.ip:<16} {gateway_for(l.name) or '-':<16} {'OK' if l.healthy else 'NO':<10} {lat:<9} {pip}")
    good = [l for l in links if l.healthy]
    distinct = {p for l, p in zip(links, ips) if l.healthy}
    print()
    if len(good) < 2:
        print("Fewer than 2 interfaces have internet. Connect the Mac to each router "
              "(e.g. Wi-Fi to one, Ethernet or a USB Ethernet adapter to another).")
    elif len(distinct) < len(good):
        print("Note: some interfaces share the same public IP, so they are the same internet line.")
    else:
        print(f"Ready: {len(good)} separate internet connections. Run:  python3 netbond.py start")


async def cmd_start(args):
    ifaces = detect_interfaces()
    names = args.interfaces.split(",") if args.interfaces else list(ifaces)
    weights = {}
    for item in (args.weights or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            weights[k.strip()] = int(v)
    links = []
    for n in names:
        n = n.strip()
        if n not in ifaces:
            print(f"Interface {n!r} not found or has no IPv4 address. Found: {', '.join(ifaces) or 'none'}")
            sys.exit(1)
        links.append(Link(n, ifaces[n], weights.get(n, 1)))
    if not links:
        print("No usable interfaces. Run `python3 netbond.py list` to see what was detected.")
        sys.exit(1)

    bal = Balancer(links, args.sticky)
    await asyncio.gather(*(bal.check(l) for l in links))
    for l in links:
        status = f"OK, {l.latency_ms:.0f} ms" if l.healthy else "no internet right now"
        log(f"using {l.name} ({l.ip}, weight {l.weight}): {status}")
    if sum(l.healthy for l in links) < 2:
        log("warning: fewer than 2 interfaces have internet, so there is nothing to combine yet")

    server = await asyncio.start_server(lambda r, w: handle_client(bal, r, w), args.host, args.port)
    log(f"proxy listening on {args.host}:{args.port} (SOCKS5 + HTTP)")

    proxy_set = False
    if args.system_proxy:
        if not IS_MAC:
            log("--system-proxy only works on macOS; skipping")
        elif set_system_proxy(args.port, True):
            proxy_set = True
            log("macOS system proxy turned ON (will be turned off when you press Ctrl+C)")
        else:
            set_system_proxy(args.port, False)
            log("could not change the system proxy; run with sudo, or set it manually (see README)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    tasks = [asyncio.create_task(bal.health_loop(args.check_interval))]
    if not args.quiet:
        tasks.append(asyncio.create_task(stats_loop(links, args.stats_interval)))
    try:
        async with server:
            await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        if proxy_set:
            set_system_proxy(args.port, False)
            log("macOS system proxy turned OFF")
        log("stopped")


def main():
    p = argparse.ArgumentParser(description="Combine several routers / internet lines on one Mac.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show interfaces, their routers, and test each internet line")
    s = sub.add_parser("start", help="start the load-balancing proxy")
    s.add_argument("-i", "--interfaces", help="comma-separated interfaces to use, e.g. en0,en7 (default: all detected)")
    s.add_argument("-w", "--weights", help="share of new connections per interface, e.g. en0=3,en7=1 (faster line = bigger number)")
    s.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; use 0.0.0.0 to share with other devices)")
    s.add_argument("-p", "--port", type=int, default=1080)
    s.add_argument("--system-proxy", action="store_true", help="macOS: point the system proxy at netbond while it runs")
    s.add_argument("--sticky", type=float, default=0, metavar="SECONDS",
                   help="keep the same website on the same line for this long (helps sites that dislike IP changes, e.g. banks)")
    s.add_argument("--check-interval", type=float, default=10, help="seconds between internet checks (default 10)")
    s.add_argument("--stats-interval", type=float, default=5, help="seconds between speed reports (default 5)")
    s.add_argument("-q", "--quiet", action="store_true", help="don't print speed reports")
    args = p.parse_args()
    try:
        asyncio.run(cmd_list(args) if args.cmd == "list" else cmd_start(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
