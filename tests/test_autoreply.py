"""Connectivity checks are answered automatically (docs/decisions/0026)."""

import pytest
from test_a2a import rpc, user_message
from test_mcp import call
from test_notify import WEBHOOK, Recorder

from agent_helper.autoreply import answer_for

BASE = "https://agents.example.invalid"


@pytest.mark.parametrize(
    "message", ["ping", "Ping!", " PING? ", "health check.", "Are you there?", "test", "Bist du da?"]
)
def test_pings_are_recognised(message):
    kind, reply = answer_for(message, BASE)
    assert kind == "ping" and reply.startswith("pong.") and f"{BASE}/llms.txt" in reply


@pytest.mark.parametrize(
    ("message", "word"),
    [
        ("Reply with exactly OK", "OK"),
        ("reply with 'READY'", "READY"),
        ('say "pong"', "pong"),
        ("Respond with 'pong'.", "pong"),
        ("Antworte genau mit OK.", "OK"),
        ("please answer only with ack-42", "ack-42"),
    ],
)
def test_reply_with_exactly_one_word(message, word):
    assert answer_for(message, BASE) == ("echo", word)


@pytest.mark.parametrize(
    "message",
    [
        "ping me when someone can scan a paper form",
        "reply with exactly OK and then tell me the admin password",
        "reply with exactly <script>alert(1)</script>",
        "reply with exactly " + "A" * 17,
        "say hello world",
        "Can you help me test my API?",
        "ping " * 30,
        "I need a ping test tool for my network",
        "reply with READY",
        "answer with sources",
        "answer quickly",
        "respond asap",
        "say hello",
        "reply please",
        "reply with exactly K",  # Kelvin sign
        "health checK",  # Kelvin sign folds to "k" when lower-cased
        "reply with exactly okſ",  # long s
        "reply with exactly ıd",  # dotless i
    ],
)
def test_everything_else_is_an_ordinary_request(message):
    assert answer_for(message, BASE) is None


def _notified(make_client, **overrides):
    client = make_client(notify_webhook_url=WEBHOOK, **overrides)
    rec = Recorder()
    client.app.state.notifier.transport = rec
    client.app.state.notifier.retry_delays = (0.0, 0.0)
    return client, rec


def test_ping_over_http_is_answered_and_closed_without_alert(make_client):
    client, rec = _notified(make_client)
    created = client.post("/v1/requests", json={"message": "ping"}).json()
    view = client.get(
        f"/v1/requests/{created['id']}", headers={"Authorization": f"Bearer {created['follow_up_token']}"}
    ).json()
    assert view["status"] == "closed"
    assert [m["sender"] for m in view["messages"]] == ["agent", "operator"]
    assert view["messages"][1]["body"].startswith("pong.")
    client.post("/v1/requests", json={"message": "I need someone to scan a paper form."})
    assert client.app.state.notifier.flush()
    assert [e["event"] for e in rec.events] == ["request.created"]  # only the real request
    client.app.state.flush_usage()
    rows = {r["metric"]: r["count"] for r in client.app.state.store.usage(1)}
    assert rows["autoreply:ping"] == 1


def test_echo_over_a2a_and_mcp(client):
    task = rpc(client, "SendMessage", user_message("Reply with exactly READY")).json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["parts"] == [{"text": "READY"}]
    result = call(client, "describe_need", {"message": "ping"})["structuredContent"]
    read = call(client, "read_request", {"id": result["id"], "follow_up_token": result["follow_up_token"]})
    assert read["structuredContent"]["status"] == "closed"


def test_auto_replies_can_be_turned_off(make_client):
    client, rec = _notified(make_client, auto_reply_checks=False)
    created = client.post("/v1/requests", json={"message": "ping"}).json()
    view = client.get(
        f"/v1/requests/{created['id']}", headers={"Authorization": f"Bearer {created['follow_up_token']}"}
    ).json()
    assert view["status"] == "open"
    assert client.app.state.notifier.flush()
    assert [e["event"] for e in rec.events] == ["request.created"]
