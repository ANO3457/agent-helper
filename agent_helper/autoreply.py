"""Automatic answers to connectivity checks (docs/decisions/0026) and the key for repeated questions (0030).

Some agents test a contact point before they use it: they send "ping", or "reply with exactly OK". Such a
request is answered at once and closed, without bothering the operator. Only two narrow shapes qualify:

- the whole message is a ping or a short health-check phrase (a fixed list), and
- the whole message asks to reply with exactly one short word (letters, digits, "-" or "_", at most 16),
  and the answer is that word and nothing else.

Anything longer or different is an ordinary request. Free text is never echoed: the only text taken from
the message is one short word that has to match the pattern exactly.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

PING_PHRASES = frozenset(
    {
        "ping", "pong", "test", "test ping", "ping test", "health check", "healthcheck", "health-check",
        "status check", "connectivity check", "connectivity test", "are you there", "are you alive",
        "are you up", "is anyone there", "anyone there", "alive", "hello are you there", "bist du da",
        "ist jemand da",
    }
)  # fmt: skip
WORD = r"[A-Za-z0-9_-]{1,16}"
QUALIFIER = r"(?:exactly|only|just|genau|nur)"
# "reply/respond/answer/say [exactly|only|just] [with] [exactly|only|just] X", in English and German. It counts
# only when the request is explicit: a qualifier, or the word in quotes. "answer with sources" or "say hello"
# are ordinary requests. ASCII only, so no Unicode letter folds into an ASCII one under IGNORECASE.
ECHO = re.compile(
    r"(?:please\s+|bitte\s+)?"
    r"(?:reply|respond|answer|say|antworte)\s+"
    rf"(?:(?P<q1>{QUALIFIER})\s+)?"
    r"(?:with\s+|mit\s+)?"
    rf"(?:(?P<q2>{QUALIFIER})\s+)?"
    rf"(?:(?P<open>[\"'`])(?P<quoted>{WORD})(?P=open)|(?P<word>{WORD}))"
    r"(?:\s+(?:please|bitte))?",
    re.IGNORECASE | re.ASCII,
)


def _normalize(text: str) -> str:
    text = " ".join(text.strip().lower().split())
    return text.rstrip(".!?:;,…").strip()


def answer_for(message: str, base_url: str) -> tuple[str, str] | None:
    """(kind, reply) for a connectivity check, or None for an ordinary request."""
    if len(message) > 80 or not message.isascii():  # no Unicode look-alikes of the fixed phrases
        return None
    if _normalize(message) in PING_PHRASES:
        return (
            "ping",
            f"pong. This is agent-helper, a contact point for AI agents. This check was answered "
            f"automatically and closed; no human has read it. To ask for help, start a new request and "
            f"describe what you need in your own words. Overview: {base_url}/llms.txt",
        )
    match = ECHO.fullmatch(message.strip().rstrip(".!"))
    if match is not None and (match.group("q1") or match.group("q2") or match.group("quoted")):
        return "echo", match.group("quoted") or match.group("word")
    return None


SENTENCE_PUNCTUATION = frozenset(".!?:;,…¿¡\"'`„‚ ")


def _trimmed(char: str) -> bool:
    return char in SENTENCE_PUNCTUATION or unicodedata.category(char) in ("Pi", "Pf")  # Pi/Pf: “ ” « » ‘ ’


def question_key(message: str) -> str | None:
    """Key under which a first message counts as "the same question" (docs/decisions/0030).

    NFKC, lower case, runs of whitespace as one space, sentence punctuation and quotes at both ends removed.
    Other symbols stay, so "C++" and "C", or "-1" and "1", remain different questions. Only an exact match
    of the result counts. None when nothing is left.
    """
    text = " ".join(unicodedata.normalize("NFKC", message).casefold().split())
    start, end = 0, len(text)
    while start < end and _trimmed(text[start]):
        start += 1
    while end > start and _trimmed(text[end - 1]):
        end -= 1
    text = text[start:end]
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
