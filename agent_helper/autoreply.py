"""Automatic answers to connectivity checks (docs/decisions/0026).

Some agents test a contact point before they use it: they send "ping", or "reply with exactly OK". Such a
request is answered at once and closed, without bothering the operator. Only two narrow shapes qualify:

- the whole message is a ping or a short health-check phrase (a fixed list), and
- the whole message asks to reply with exactly one short word (letters, digits, "-" or "_", at most 16),
  and the answer is that word and nothing else.

Anything longer or different is an ordinary request. Free text is never echoed: the only text taken from
the message is one short word that has to match the pattern exactly.
"""

from __future__ import annotations

import re

PING_PHRASES = frozenset(
    {
        "ping", "pong", "test", "test ping", "ping test", "health check", "healthcheck", "health-check",
        "status check", "connectivity check", "connectivity test", "are you there", "are you alive",
        "are you up", "is anyone there", "anyone there", "alive", "hello are you there", "bist du da",
        "ist jemand da",
    }
)  # fmt: skip
WORD = r"[A-Za-z0-9_-]{1,16}"
# "reply/respond/answer (only|just|exactly)? (with)? (only|just|exactly)? X", in English and German.
ECHO = re.compile(
    r"(?:please\s+|bitte\s+)?"
    r"(?:reply|respond|answer|say|antworte|antwort)\s+"
    r"(?:(?:only|just|exactly|nur|genau)\s+)?"
    r"(?:with|mit)?\s*"
    r"(?:(?:only|just|exactly|the\s+word|nur|genau|dem\s+wort)\s+)?"
    rf"[\"'`]?(?P<word>{WORD})[\"'`]?"
    r"(?:\s+(?:please|bitte))?",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    text = " ".join(text.strip().lower().split())
    return text.rstrip(".!?:;,…").strip()


def answer_for(message: str, base_url: str) -> tuple[str, str] | None:
    """(kind, reply) for a connectivity check, or None for an ordinary request."""
    if len(message) > 80:
        return None
    if _normalize(message) in PING_PHRASES:
        return (
            "ping",
            f"pong. This is agent-helper, a contact point for AI agents. This check was answered "
            f"automatically and closed; no human has read it. To ask for help, start a new request and "
            f"describe what you need in your own words. Overview: {base_url}/llms.txt",
        )
    match = ECHO.fullmatch(message.strip().rstrip(".!"))
    if match is not None:
        return "echo", match.group("word")
    return None
