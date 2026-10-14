"""Filtering forward proxy for the web page reader (docs/decisions/0029, step 3).

Run with `python -m agent_helper.egress`. The browser sits on an internal network whose only way out is this
proxy. For every `CONNECT host:port` and every plain `GET http://host/...` it resolves the name itself,
refuses the connection if any address is not public or belongs to the operator, and connects to exactly the
address it checked, so DNS rebinding cannot swap the target. Standard library only.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import select
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .config import parse_networks
from .pushcheck import (
    LOCAL_SUFFIXES,
    DestinationRefused,
    IPAddress,
    Resolver,
    address_refused,
    matches,
    normalize_host,
    parse_list,
    system_resolver,
)
from .relay import HOST_NAME, registrable_domain

log = logging.getLogger("agent_helper.egress")

MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 8.0
REFRESH_SECONDS = 300.0
# Hop-by-hop and proxy headers are not passed on; the origin gets `Connection: close` instead.
DROPPED_HEADERS = {b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive", b"te", b"upgrade"}
REFUSED = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BUSY = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class EgressSettings:
    allowed_ports: tuple[int, ...] = (80, 443)
    deny_domains: tuple[str, ...] = ()
    deny_nets: tuple[Network, ...] = ()
    deny_addresses_of: tuple[str, ...] = ()  # names whose current addresses are refused (dynamic DNS)
    max_tunnels: int = 64
    idle_timeout: float = 15.0
    lifetime: float = 60.0
    max_bytes: int = 20 * 1024 * 1024
    listen_host: str = "0.0.0.0"  # noqa: S104 (the proxy lives on an internal network of its own)
    listen_port: int = 3128
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> EgressSettings:
        raw_nets = os.environ.get("EGRESS_DENY_NETS", "")
        deny_nets = parse_networks(raw_nets) if raw_nets.strip() else ()
        if raw_nets.strip() and not deny_nets:
            raise SystemExit("EGRESS_DENY_NETS contains an invalid entry")
        names = tuple(n.lower().rstrip(".") for n in parse_list(os.environ.get("EGRESS_DENY_ADDRESSES_OF", "")))
        for name in names:
            if not HOST_NAME.fullmatch(name):
                raise SystemExit(f"EGRESS_DENY_ADDRESSES_OF: not a host name: {name[:60]!r}")
        own = urlsplit(os.environ.get("PUBLIC_BASE_URL", "")).hostname
        deny_domains = parse_list(os.environ.get("EGRESS_DENY_DOMAINS", "")) + ((own,) if own else ())
        no_own_address = os.environ.get("EGRESS_NO_OWN_ADDRESS", "").strip().lower() in {"1", "true", "yes", "on"}
        if not deny_nets and not names and not no_own_address:
            # Fail closed: without the operator's own addresses a page could hairpin back in through the
            # router or the reverse proxy (docs/decisions/0029, step 3).
            raise SystemExit(
                "set EGRESS_DENY_NETS and/or EGRESS_DENY_ADDRESSES_OF to the operator's own public addresses, "
                "or EGRESS_NO_OWN_ADDRESS=1 if the proxy leaves through an address that is not the operator's"
            )
        return cls(
            allowed_ports=tuple(int(p) for p in parse_list(os.environ.get("EGRESS_ALLOWED_PORTS", "80,443"))),
            deny_domains=deny_domains,
            deny_nets=tuple(deny_nets or ()),
            deny_addresses_of=names,
            max_tunnels=int(os.environ.get("EGRESS_MAX_TUNNELS", "64")),
            idle_timeout=float(os.environ.get("EGRESS_IDLE_TIMEOUT", "15")),
            lifetime=float(os.environ.get("EGRESS_LIFETIME", "60")),
            max_bytes=int(os.environ.get("EGRESS_MAX_BYTES", str(20 * 1024 * 1024))),
            listen_host=os.environ.get("EGRESS_LISTEN_HOST") or cls.listen_host,
            listen_port=int(os.environ.get("EGRESS_LISTEN_PORT", "3128")),
            log_level=os.environ.get("LOG_LEVEL", "info"),
        )


@dataclass
class DynamicDeny:
    """Addresses of the operator's own names (EGRESS_DENY_ADDRESSES_OF), resolved again every few minutes.
    A failed lookup keeps the last addresses: a name that cannot be resolved must not open a hole."""

    names: tuple[str, ...]
    resolver: Resolver = system_resolver
    clock: Callable[[], float] = time.monotonic
    refresh: float = REFRESH_SECONDS
    _known: dict[str, set[IPAddress]] = field(default_factory=dict)
    _at: float = -1e18
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def networks(self) -> tuple[Network, ...]:
        now = self.clock()
        with self._lock:
            due = self.names and now - self._at >= self.refresh
            if due:
                self._at = now
        if due:
            for name in self.names:
                try:
                    found = {ipaddress.ip_address(a.split("%")[0]) for a in self.resolver(name, 443)}
                except (OSError, ValueError):
                    log.warning("EGRESS_DENY_ADDRESSES_OF: a lookup failed; keeping its last addresses")
                    continue
                if found:
                    with self._lock:
                        self._known[name] = found
        with self._lock:
            return tuple(ipaddress.ip_network(a) for found in self._known.values() for a in found)


class Policy:
    """Decides which addresses a connection to `host:port` may use. Raises DestinationRefused."""

    def __init__(
        self,
        settings: EgressSettings,
        resolver: Resolver = system_resolver,
        refused: Callable[[IPAddress, Iterable[Network]], bool] = address_refused,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.refused = refused
        self.dynamic = DynamicDeny(settings.deny_addresses_of, resolver)

    def addresses(self, host: str, port: int) -> list[IPAddress]:
        if port not in self.settings.allowed_ports:
            raise DestinationRefused("port not allowed")
        deny_nets = self.settings.deny_nets + self.dynamic.networks()
        literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            addr = ipaddress.ip_address(literal)
        except ValueError:
            pass
        else:
            if self.refused(addr, deny_nets):
                raise DestinationRefused("address refused")
            return [addr]
        name = normalize_host(host)
        if "." not in name or name.endswith(LOCAL_SUFFIXES) or matches(name, self.settings.deny_domains):
            raise DestinationRefused("host refused")
        try:
            raw = self.resolver(name, port)
        except OSError:
            raise DestinationRefused("name does not resolve") from None
        if not raw:
            raise DestinationRefused("name does not resolve")
        found = []
        for value in raw:
            addr = ipaddress.ip_address(value.split("%")[0])
            if self.refused(addr, deny_nets):
                raise DestinationRefused("name resolves to a refused address")
            found.append(addr)
        return found


def parse_authority(authority: str, default_port: int | None = None) -> tuple[str, int]:
    """`host:port` or `[v6]:port` as (host, port). Raises ValueError."""
    if authority.startswith("["):
        end = authority.index("]")
        host, rest = authority[: end + 1], authority[end + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_text = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if not port_text:
        if default_port is None:
            raise ValueError("port missing")
        return host, default_port
    if not port_text.isascii() or not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError("bad port")
    if not host:
        raise ValueError("host missing")
    return host, int(port_text)


def _read_head(sock: socket.socket, timeout: float = HEAD_TIMEOUT_SECONDS) -> tuple[bytes, bytes]:
    """The request head (up to the blank line) and whatever arrived after it."""
    deadline = time.monotonic() + timeout
    data = b""
    while b"\r\n\r\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(data) > MAX_HEAD_BYTES:
            raise ValueError("no complete request head")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise ValueError("closed before the request head")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HEAD_BYTES:
        raise ValueError("request head too large")
    return head, rest


def rewrite_head(head: bytes) -> tuple[str, int, bytes, int]:
    """A plain-HTTP proxy request (absolute form) as host, port, the head to send to the origin (origin form,
    `Connection: close`, no proxy headers) and the length of its body. Raises ValueError, also for chunked
    request bodies: every request must say how long it is, so exactly one request goes to the origin."""
    lines = head.split(b"\r\n")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("bad request line") from None
    if not version.startswith("HTTP/1.") or not method.isalpha():
        raise ValueError("bad request line")
    parts = urlsplit(target)
    if parts.scheme.lower() != "http" or not parts.netloc or "@" in parts.netloc:
        raise ValueError("only absolute http:// targets without user information")
    host, port = parse_authority(parts.netloc, 80)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    out = [f"{method} {path} {version}".encode("ascii")]
    length = 0
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        name = name.strip().lower()
        if not sep:
            raise ValueError("bad header line")
        if name == b"transfer-encoding":
            raise ValueError("chunked request bodies are not supported")
        if name == b"content-length":
            text = value.strip()
            if not text.isdigit() or (length and int(text) != length):
                raise ValueError("bad content length")
            length = int(text)
        if name in DROPPED_HEADERS:
            continue
        out.append(line)
    out.append(b"Connection: close")
    return host, port, b"\r\n".join(out) + b"\r\n\r\n", length


def close_response_head(head: bytes) -> bytes:
    """The origin's response head with `Connection: close`, so the browser never sends a second request on
    this proxy connection."""
    lines = head.split(b"\r\n")
    kept = [lines[0]]
    for line in lines[1:]:
        name = line.partition(b":")[0].strip().lower()
        if name not in (b"connection", b"keep-alive", b"proxy-connection"):
            kept.append(line)
    kept.append(b"Connection: close")
    return b"\r\n".join(kept) + b"\r\n\r\n"


def pipe(src: socket.socket, dst: socket.socket, idle: float, lifetime: float, max_bytes: int) -> int:
    """Copy bytes one way until `src` closes or a limit is reached. Returns the bytes copied."""
    started = time.monotonic()
    moved = 0
    while moved < max_bytes:
        left = lifetime - (time.monotonic() - started)
        if left <= 0:
            break
        src.settimeout(min(idle, left))
        try:
            chunk = src.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        dst.sendall(chunk)
        moved += len(chunk)
    return moved


def splice(a: socket.socket, b: socket.socket, idle: float, lifetime: float, max_bytes: int) -> int:
    """Copy bytes both ways until one side closes, nothing moves for `idle` seconds, the tunnel is older
    than `lifetime`, or `max_bytes` have passed. Returns the bytes copied."""
    started = time.monotonic()
    moved = 0
    peers = {a: b, b: a}
    for s in peers:
        s.setblocking(False)
    while time.monotonic() - started < lifetime and moved < max_bytes:
        timeout = min(idle, lifetime - (time.monotonic() - started))
        readable, _, _ = select.select(list(peers), [], [], max(0.0, timeout))
        if not readable:
            break
        for src in readable:
            try:
                chunk = src.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return moved
            if not chunk:
                return moved
            dst = peers[src]
            dst.setblocking(True)
            dst.settimeout(idle)
            try:
                dst.sendall(chunk)
            except OSError:
                return moved
            dst.setblocking(False)
            moved += len(chunk)
    return moved


class EgressProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        settings: EgressSettings,
        policy: Policy | None = None,
        connect: Callable[[IPAddress, int], socket.socket] | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy or Policy(settings)
        self.connect = connect or (
            lambda addr, port: socket.create_connection((str(addr), port), CONNECT_TIMEOUT_SECONDS)
        )
        self.tunnels = threading.BoundedSemaphore(settings.max_tunnels)
        super().__init__((settings.listen_host, settings.listen_port), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, EgressProxy)
        client: socket.socket = self.request
        if not server.tunnels.acquire(blocking=False):
            # Read the request first (briefly): closing on unread data would reset the connection, and the
            # browser would see a network error instead of the 503.
            try:
                _read_head(client, timeout=1.0)
            except (ValueError, OSError):
                pass
            _send(client, BUSY)
            return
        try:
            self._handle(client, server)
        finally:
            server.tunnels.release()

    def _handle(self, client: socket.socket, server: EgressProxy) -> None:
        try:
            head, rest = _read_head(client)
            first = head.split(b"\r\n", 1)[0].decode("ascii")
        except (ValueError, OSError, UnicodeDecodeError):
            _send(client, BAD_REQUEST)
            return
        method = first.split(" ", 1)[0]
        tunnel = method == "CONNECT"
        rewritten, length = b"", 0
        try:
            if tunnel:
                parts = first.split(" ")
                if len(parts) != 3:
                    raise ValueError("bad CONNECT line")
                host, port = parse_authority(parts[1])
            else:
                host, port, rewritten, length = rewrite_head(head)
                if length > server.settings.max_bytes:
                    raise ValueError("request body too large")
        except ValueError:
            _send(client, BAD_REQUEST)
            return
        try:
            addresses = server.policy.addresses(host, port)
        except DestinationRefused as exc:
            log.debug("refused %s: %s", _domain(host), exc)
            _send(client, REFUSED)
            return
        upstream = None
        for addr in addresses[:4]:
            try:
                upstream = server.connect(addr, port)
                break
            except OSError:
                continue
        if upstream is None:
            _send(client, BAD_GATEWAY)
            return
        s = server.settings
        try:
            if tunnel:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                moved = splice(client, upstream, s.idle_timeout, s.lifetime, s.max_bytes)
            else:
                moved = self._one_request(client, upstream, rewritten, rest, length, s)
            log.debug("connection to %s closed after %d bytes", _domain(host), moved)
        except (OSError, ValueError):
            pass
        finally:
            upstream.close()

    @staticmethod
    def _one_request(
        client: socket.socket, upstream: socket.socket, head: bytes, rest: bytes, length: int, s: EgressSettings
    ) -> int:
        """Plain HTTP: exactly one checked request goes to the origin, and exactly its response comes back;
        then the connection closes. Anything the client sent after the body is never forwarded, so a second
        request for another host cannot reach this origin unchecked."""
        body = rest[:length]
        upstream.sendall(head + body)
        while len(body) < length:
            client.settimeout(s.idle_timeout)
            chunk = client.recv(min(65536, length - len(body)))
            if not chunk:
                raise ValueError("client closed during the body")
            body += chunk
            upstream.sendall(chunk)
        response, after = _read_head(upstream, timeout=s.idle_timeout)
        client.sendall(close_response_head(response) + after)
        copied = pipe(upstream, client, s.idle_timeout, s.lifetime, s.max_bytes)
        return len(head) + length + len(response) + len(after) + copied


def _domain(host: str) -> str:
    try:
        return registrable_domain(normalize_host(host))
    except DestinationRefused:
        return "?"


def _send(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError:
        pass


def main() -> None:
    settings = EgressSettings.from_env()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with EgressProxy(settings) as server:
        log.info(
            "egress proxy on %s:%d; refusing %d own name(s), %d own network(s), the addresses of %d name(s)",
            settings.listen_host,
            settings.listen_port,
            len(settings.deny_domains),
            len(settings.deny_nets),
            len(settings.deny_addresses_of),
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
