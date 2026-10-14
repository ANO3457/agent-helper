"""The browse gateway (docs/decisions/0028, step 6), with a fake browser driver."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_helper.browse import JOB_PATH, GatewayClient
from agent_helper.browse_gateway import _readable, create_gateway_app, valid_job
from agent_helper.relay import sign

SECRET = "gateway-secret-" + "z" * 32
JOB = {"job_id": "job_1", "url": "https://example.org/", "format": "text", "max_chars": 1000, "wait_ms": 0}


class FakeDriver:
    def __init__(self, result=None, delay=0.0):
        self.jobs = []
        self.result = result or {"outcome": "ok", "status": 200, "content": "hi"}
        self.delay = delay

    async def read(self, job, deadline):
        self.jobs.append(job)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def gateway(driver, **kw):
    return TestClient(create_gateway_app(SECRET, driver, **kw))


def post(client, job, secret=SECRET, headers=None):
    body = json.dumps(job).encode()
    hdrs = GatewayClient("http://gateway:8095", secret, 5).signed_headers(body) | (headers or {})
    return client.post(JOB_PATH, content=body, headers=hdrs)


def test_signed_job_runs():
    driver = FakeDriver()
    r = post(gateway(driver), JOB | {"url": "HTTPS://Example.ORG/#x"})
    assert r.status_code == 200 and r.json()["content"] == "hi"
    assert driver.jobs[0]["url"] == "https://example.org/"  # normalized again on the gateway


def test_signatures_are_required_and_not_replayable():
    driver = FakeDriver()
    client = gateway(driver)
    assert client.post(JOB_PATH, json=JOB).status_code == 401
    assert post(client, JOB, secret="wrong-" + "x" * 40).status_code == 401
    body = json.dumps(JOB).encode()
    old = str(int(time.time()) - 120)
    headers = {
        "X-Relay-Timestamp": old,
        "X-Relay-Nonce": "n" * 20,
        "X-Relay-Signature": sign(SECRET, old, "n" * 20, "POST", JOB_PATH, body),
    }
    assert client.post(JOB_PATH, content=body, headers=headers).status_code == 401
    hdrs = GatewayClient("http://gateway:8095", SECRET, 5).signed_headers(body)
    assert client.post(JOB_PATH, content=body, headers=hdrs).status_code == 200
    assert client.post(JOB_PATH, content=body, headers=hdrs).status_code == 401  # replay
    assert client.post(JOB_PATH + "?x=1", content=body, headers=hdrs).status_code == 401
    assert client.post(JOB_PATH, content=b"x" * 9000, headers=hdrs).status_code in (401, 413)
    assert len(driver.jobs) == 1


@pytest.mark.parametrize(
    "job",
    [
        JOB | {"url": "http://10.0.0.1/"},
        JOB | {"url": "file:///etc/passwd"},
        JOB | {"url": "https://own.example/"},
        JOB | {"format": "pdf"},
        JOB | {"max_chars": 10**7},
        JOB | {"wait_ms": -1},
        JOB | {"headers": {"Cookie": "x"}},
        {k: v for k, v in JOB.items() if k != "format"},
        [JOB],
    ],
)
def test_jobs_are_checked_again(job):
    driver = FakeDriver()
    r = post(gateway(driver, own_hosts=("own.example",)), job)
    assert r.status_code == 400 and r.json() == {"outcome": "refused"}
    assert driver.jobs == []


def test_busy_timeout_and_failures(monkeypatch):
    async def busy():
        app = create_gateway_app(SECRET, FakeDriver(delay=0.2), concurrency=1)
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:

            def call(i):
                body = json.dumps(JOB | {"job_id": f"job_{i}"}).encode()
                hdrs = GatewayClient("http://gw", SECRET, 5).signed_headers(body)
                return c.post(JOB_PATH, content=body, headers=hdrs)

            return await asyncio.gather(call(1), call(2))

    statuses = sorted(r.status_code for r in asyncio.run(busy()))
    assert statuses == [200, 503]

    # A driver that ignores its deadline is cut off by the gateway.
    monkeypatch.setattr("agent_helper.browse_gateway.CUTOFF_MARGIN_SECONDS", 0.0)
    assert post(gateway(FakeDriver(delay=2), timeout=0.1), JOB).json() == {"outcome": "timeout"}
    assert post(gateway(FakeDriver(RuntimeError("boom"))), JOB).json() == {"outcome": "failed"}


def test_short_secret_refused():
    with pytest.raises(SystemExit):
        create_gateway_app("short", FakeDriver())


def test_valid_job_and_readable_types():
    assert valid_job(dict(JOB)) == JOB
    with pytest.raises(ValueError):
        valid_job(JOB | {"max_chars": True})
    assert _readable("text/html; charset=utf-8") and _readable("application/json") and _readable("text/csv")
    assert not _readable("application/pdf") and not _readable("application/zip") and not _readable("")
