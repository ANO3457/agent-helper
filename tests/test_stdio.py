import asyncio
import json

from agent_helper.app import create_app
from agent_helper.config import Settings
from agent_helper.stdio import serve

MODERN = "2026-07-28"


def run(tmp_path, *messages):
    settings = Settings(public_base_url="http://testserver", data_dir=tmp_path)
    lines = [m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages]
    lines.append(b"")
    out: list[bytes] = []
    asyncio.run(serve(create_app(settings), "testserver", lambda: lines.pop(0), out.append))
    assert all(chunk.endswith(b"\n") and chunk.count(b"\n") == 1 for chunk in out)
    return [json.loads(chunk) for chunk in out]


def test_legacy_handshake_and_tool_list(tmp_path):
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    replies = run(
        tmp_path,
        init,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        b"\n",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert [r["id"] for r in replies] == [1, 2]  # the notification and the blank line get no answer
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[0]["result"]["serverInfo"]["name"] == "agent-helper"
    assert "find_help" in {t["name"] for t in replies[1]["result"]["tools"]}


def test_modern_call_gets_its_headers_from_the_message(tmp_path):
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    call = {
        "jsonrpc": "2.0",
        "id": "a",
        "method": "tools/call",
        "params": {"name": "find_help", "arguments": {"need": "a place to run a long job"}, "_meta": meta},
    }
    (reply,) = run(tmp_path, call)
    assert reply["id"] == "a"
    assert reply["result"]["isError"] is False


def test_unparseable_line_gets_a_parse_error(tmp_path):
    (reply,) = run(tmp_path, b"not json\n")
    assert reply["error"]["code"] == -32700
