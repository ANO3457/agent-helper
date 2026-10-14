"""The egress proxy of the web page reader (docs/decisions/0029, step 3)."""

import ipaddress
import socket
import threading

import pytest

from agent_helper.egress import (
    DynamicDeny,
    EgressProxy,
    EgressSettings,
    Policy,
    close_response_head,
    parse_authority,
    rewrite_head,
)
from agent_helper.pushcheck import DestinationRefused, address_refused


def resolver_for(table):
    calls = []

    def resolve(host, port):
        calls.append(host)
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    resolve.calls = calls
    return resolve


PUBLIC = {"example.org": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]}


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["192.168.1.1"],
        ["100.64.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fd00::1"],
        ["::ffff:192.168.0.1"],
        ["64:ff9b::a00:1"],
        ["2002:c0a8:101::1"],
        ["93.184.215.14", "10.0.0.1"],  # one private address is enough to refuse
    ],
)
def test_names_resolving_to_private_addresses_are_refused(answer):
    policy = Policy(EgressSettings(), resolver_for({"evil.example": answer}))
    with pytest.raises(DestinationRefused):
        policy.addresses("evil.example", 443)


def test_public_names_and_literals_pass():
    policy = Policy(EgressSettings(), resolver_for(PUBLIC))
    assert [str(a) for a in policy.addresses("Example.ORG.", 443)][0] == "93.184.215.14"
    assert policy.addresses("93.184.215.14", 80) == [ipaddress.ip_address("93.184.215.14")]
    assert policy.addresses("[2606:2800:21f:cb07:6820:80da:af6b:8b2c]", 443)


@pytest.mark.parametrize(
    "host,port",
    [
        ("example.org", 22),
        ("example.org", 8080),
        ("127.0.0.1", 443),
        ("[::1]", 443),
        ("10.0.0.1", 80),
        ("localhost", 80),
        ("gateway", 8095),  # a container name on the browser's network
        ("agent-helper", 8080),
        ("printer.local", 80),
        ("router.home.arpa", 443),
        ("nothing.example", 443),  # does not resolve
    ],
)
def test_refused_targets(host, port):
    with pytest.raises(DestinationRefused):
        Policy(EgressSettings(), resolver_for(PUBLIC)).addresses(host, port)


def test_operator_names_and_networks_are_refused():
    settings = EgressSettings(
        deny_domains=("*.own.example", "agents.example"),
        deny_nets=(ipaddress.ip_network("198.51.100.0/24"),),
    )
    table = PUBLIC | {"www.own.example": ["93.184.215.14"], "hairpin.example": ["198.51.100.7"]}
    policy = Policy(settings, resolver_for(table))
    for host in ("own.example", "www.own.example", "agents.example", "hairpin.example", "198.51.100.7"):
        with pytest.raises(DestinationRefused):
            policy.addresses(host, 443)
    assert policy.addresses("example.org", 443)


def test_dynamic_dns_of_the_operator_is_refused_and_kept_when_lookups_fail():
    now = [0.0]
    answers = {"home.dyndns.example": ["203.0.114.9"]}

    def resolve(host, port):
        if host not in answers:
            raise OSError("lookup failed")
        return answers[host]

    deny = DynamicDeny(("home.dyndns.example",), resolve, clock=lambda: now[0])
    assert ipaddress.ip_network("203.0.114.9/32") in deny.networks()
    answers["home.dyndns.example"] = ["203.0.114.10"]  # the public address changed
    assert ipaddress.ip_network("203.0.114.9/32") in deny.networks()  # not yet refreshed
    now[0] += 301
    assert deny.networks() == (ipaddress.ip_network("203.0.114.10/32"),)
    del answers["home.dyndns.example"]  # the lookup fails: the last address stays refused
    now[0] += 301
    assert deny.networks() == (ipaddress.ip_network("203.0.114.10/32"),)

    policy = Policy(EgressSettings(deny_addresses_of=("home.dyndns.example",)), resolver_for(PUBLIC))
    policy.dynamic = deny
    with pytest.raises(DestinationRefused):
        policy.addresses("203.0.114.10", 443)


def test_parse_authority_and_rewrite_head():
    assert parse_authority("example.org:443") == ("example.org", 443)
    assert parse_authority("[::1]:8443") == ("[::1]", 8443)
    assert parse_authority("example.org", 80) == ("example.org", 80)
    for bad in ("example.org", "example.org:0", "example.org:99999", "example.org:८०", ":443"):
        with pytest.raises(ValueError):
            parse_authority(bad)

    head = (
        b"GET http://example.org/a?b=1 HTTP/1.1\r\nHost: example.org\r\nProxy-Connection: keep-alive\r\n"
        b"Proxy-Authorization: Basic eA==\r\nConnection: keep-alive\r\nUpgrade: h2c\r\nAccept: */*"
    )
    host, port, out, length = rewrite_head(head)
    assert (host, port, length) == ("example.org", 80, 0)
    assert out == b"GET /a?b=1 HTTP/1.1\r\nHost: example.org\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    post = b"POST http://example.org/f HTTP/1.1\r\nHost: example.org\r\nContent-Length: 5"
    assert rewrite_head(post)[3] == 5
    for bad in (
        b"POST http://example.org/ HTTP/1.1\r\nTransfer-Encoding: chunked",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 5",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 0",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: \xd9\xa5",
        b"POST http://example.org/ HTTP/1.1\r\nContent-Length: -1",
        b"GET http://example.org/ HTTP/1.1\r\nno colon here",
        b"GET /relative HTTP/1.1",
        b"GET https://example.org/ HTTP/1.1",
        b"GET http://user@example.org/ HTTP/1.1",
        b"GET ftp://example.org/ HTTP/1.1",
        b"GE T http://example.org/ HTTP/1.1",
    ):
        with pytest.raises(ValueError):
            rewrite_head(bad)


def test_response_head_is_marked_close():
    head = b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nKeep-Alive: timeout=5\r\nContent-Length: 2"
    assert close_response_head(head) == b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"


@pytest.mark.parametrize(
    "env,ok",
    [
        ({}, False),
        ({"PUBLIC_BASE_URL": "https://own.example"}, False),  # the own name alone is not enough
        ({"EGRESS_DENY_NETS": "198.51.100.0/24"}, True),
        ({"EGRESS_DENY_ADDRESSES_OF": "home.dyndns.example"}, True),
        ({"EGRESS_NO_OWN_ADDRESS": "1"}, True),
        ({"EGRESS_DENY_NETS": "not-a-network"}, False),
        ({"EGRESS_DENY_ADDRESSES_OF": "bad name!"}, False),
    ],
)
def test_proxy_fails_closed_without_the_operators_addresses(monkeypatch, env, ok):
    for name in ("EGRESS_DENY_NETS", "EGRESS_DENY_ADDRESSES_OF", "EGRESS_NO_OWN_ADDRESS", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if ok:
        EgressSettings.from_env()
    else:
        with pytest.raises(SystemExit):
            EgressSettings.from_env()


def test_listen_host_from_env(monkeypatch):
    monkeypatch.setenv("EGRESS_NO_OWN_ADDRESS", "1")
    monkeypatch.setenv("EGRESS_LISTEN_HOST", "10.250.29.3")
    assert EgressSettings.from_env().listen_host == "10.250.29.3"


# --- the proxy on a real socket, with a loopback "origin" allowed only for the test ---------------


class Origin:
    """A one-shot TCP server on loopback that records what it receives and answers."""

    def __init__(self, answer=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", keep_open=False, body=0):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received = b""
        self.answer = answer
        self.keep_open = keep_open  # like a keep-alive origin: read on after answering
        self.body = body
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.settimeout(3)
        try:
            while b"\r\n\r\n" not in self.received or len(self.received.partition(b"\r\n\r\n")[2]) < self.body:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                self.received += chunk
            conn.sendall(self.answer)
            if self.keep_open:
                conn.settimeout(1)
                try:
                    while chunk := conn.recv(4096):
                        self.received += chunk
                except OSError:
                    pass
        finally:
            conn.close()
            self.done.set()


@pytest.fixture
def proxy():
    started = []

    def start(origin_port, table):
        settings = EgressSettings(listen_host="127.0.0.1", listen_port=0, allowed_ports=(origin_port,), lifetime=5)
        resolve = resolver_for(table)
        loopback_ok = ipaddress.ip_address("127.0.0.1")

        def refused(addr, nets):  # the test's origin lives on loopback; everything else as in production
            return addr != loopback_ok and address_refused(addr, nets)

        server = EgressProxy(settings, Policy(settings, resolve, refused))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        server.resolve = resolve
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _talk(server, data, read_until_close=True):
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
            if not read_until_close and b"\r\n\r\n" in out:
                break
        return out


def test_plain_http_is_forwarded_in_origin_form(proxy):
    origin = Origin()
    server = proxy(origin.port, {"site.example": ["127.0.0.1"]})
    request = (
        f"GET http://site.example:{origin.port}/page HTTP/1.1\r\nHost: site.example\r\n"
        "Proxy-Authorization: Basic eA==\r\n\r\n"
    ).encode()
    answer = _talk(server, request)
    assert answer.endswith(b"ok")
    assert origin.received.startswith(b"GET /page HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in origin.received and b"Connection: close" in origin.received
    assert server.resolve.calls == ["site.example"]  # resolved once; the checked address is the one used


def test_plain_http_carries_one_request_only(proxy):
    # A keep-alive origin, and a client that sends a second request for another host on the same connection:
    # the second one must never reach this origin, and the client is told to close.
    origin = Origin(answer=b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nok", keep_open=True)
    server = proxy(origin.port, {"site.example": ["127.0.0.1"], "other.example": ["127.0.0.1"]})
    first = f"GET http://site.example:{origin.port}/one HTTP/1.1\r\nHost: site.example\r\n\r\n".encode()
    second = f"GET http://other.example:{origin.port}/two HTTP/1.1\r\nHost: other.example\r\n\r\n".encode()
    answer = _talk(server, first + second)
    origin.done.wait(5)
    assert answer.startswith(b"HTTP/1.1 200 OK\r\n") and b"Connection: close" in answer
    assert b"keep-alive" not in answer and answer.endswith(b"ok")
    assert b"/one" in origin.received and b"/two" not in origin.received
    assert server.resolve.calls == ["site.example"]


def test_plain_http_body_is_forwarded(proxy):
    origin = Origin(body=5)
    server = proxy(origin.port, {"site.example": ["127.0.0.1"]})
    request = f"POST http://site.example:{origin.port}/f HTTP/1.1\r\nHost: site.example\r\nContent-Length: 5\r\n\r\n"
    answer = _talk(server, request.encode() + b"hello" + b"GET http://x/ HTTP/1.1\r\n\r\n")
    origin.done.wait(5)
    assert answer.endswith(b"ok") and origin.received.endswith(b"\r\n\r\nhello")
    chunked = f"POST http://site.example:{origin.port}/f HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
    assert _talk(server, chunked.encode()).startswith(b"HTTP/1.1 400")


def test_connect_tunnels_to_the_checked_address(proxy):
    origin = Origin(answer=b"tunnelled")
    server = proxy(origin.port, {"site.example": ["127.0.0.1"]})
    data = f"CONNECT site.example:{origin.port} HTTP/1.1\r\n\r\nhello\r\n\r\n".encode()
    answer = _talk(server, data)
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and answer.endswith(b"tunnelled")
    assert origin.received == b"hello\r\n\r\n"


def test_refusals_on_the_wire(proxy):
    origin = Origin()
    server = proxy(origin.port, {"rebind.example": ["127.0.0.2"], "site.example": ["127.0.0.1"]})
    assert _talk(server, f"CONNECT rebind.example:{origin.port} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 403")
    assert _talk(server, b"CONNECT site.example:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _talk(server, b"CONNECT 10.0.0.1:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _talk(server, b"GET /relative HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert _talk(server, b"garbage\r\n\r\n").startswith(b"HTTP/1.1 400")
    # Nothing was ever sent to the origin.
    assert origin.received == b""


def test_tunnel_limit(proxy):
    origin = Origin()
    server = proxy(origin.port, {"site.example": ["127.0.0.1"]})
    for _ in range(server.settings.max_tunnels):
        assert server.tunnels.acquire(blocking=False)
    assert _talk(server, b"CONNECT site.example:1 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 503")
