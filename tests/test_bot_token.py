"""A narrow token for the operator's chat bot (docs/decisions/0027)."""

import logging

import pytest

BOT = "bot-token-" + "b" * 40


@pytest.fixture
def bot_client(make_client):
    return make_client(operator_bot_token=BOT)


def bot(token=BOT):
    return {"Authorization": f"Bearer {token}"}


def test_bot_can_list_read_and_answer(bot_client, caplog):
    created = bot_client.post("/v1/requests", json={"message": "Please scan a paper form."}).json()
    listed = bot_client.get("/admin/v1/requests?status=open", headers=bot())
    assert listed.status_code == 200 and [r["id"] for r in listed.json()] == [created["id"]]
    one = bot_client.get(f"/admin/v1/requests/{created['id']}", headers=bot()).json()
    assert one["messages"][0]["body"] == "Please scan a paper form."
    with caplog.at_level(logging.INFO, logger="agent_helper.audit"):
        r = bot_client.post(
            f"/admin/v1/requests/{created['id']}/replies",
            json={"message": "Done.", "status": "answered"},
            headers=bot(),
        )
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert f"operator reply to {created['id']} (answered) via telegram" in caplog.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/admin/v1/requests/req_x/referrals", {"to": "nova", "note": "x"}),
        ("GET", "/admin/v1/reports", None),
        ("POST", "/admin/v1/board", {"content": "x"}),
        ("POST", "/admin/v1/board/1/purge", {"reason": "x", "confirm": "PURGE 1"}),
        ("GET", "/admin/v1/usage", None),
        ("GET", "/admin/v1/push", None),
        ("POST", "/admin/v1/notifications/test", None),
        ("GET", "/admin/v1/directory", None),
    ],
)
def test_bot_token_cannot_do_anything_else(bot_client, method, path, body):
    r = bot_client.request(method, path, json=body, headers=bot())
    assert r.status_code == 403


def test_wrong_or_short_tokens(make_client, admin_headers):
    client = make_client(operator_bot_token=BOT)
    assert client.get("/admin/v1/requests", headers=bot("wrong-" + "x" * 40)).status_code == 401
    assert client.get("/admin/v1/requests").status_code == 401
    # The admin secret still does everything.
    assert client.get("/admin/v1/reports", headers=admin_headers).status_code == 200
    too_short = "too-short"
    short = make_client(operator_bot_token=too_short)
    assert short.get("/admin/v1/requests", headers=bot(too_short)).status_code == 401


def test_without_a_bot_token_nothing_changes(client, admin_headers):
    assert client.get("/admin/v1/requests", headers=bot()).status_code == 401
    assert client.get("/admin/v1/requests", headers=admin_headers).status_code == 200


def test_bot_token_works_only_where_the_admin_api_is(make_client):
    client = make_client(operator_bot_token=BOT, admin_port=8081)
    # The test client reaches the app on the public port: /admin does not exist there.
    assert client.get("/admin/v1/requests", headers=bot()).status_code == 404
