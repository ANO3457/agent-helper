# 0026: Automatic answers to connectivity checks

- Status: accepted
- Date: 2026-10-13
- Related: [0012](0012-operator-notifications.md), [0023](0023-anonymous-usage-counts.md)

## Context

Agents and their developers test a contact point before they rely on it: they send "ping", or ask the
service to "reply with exactly OK". Such a request waited for the human operator like any other, so the
check failed (no answer for hours) and the operator was alerted for nothing. The operator asked that
pings are always answered automatically.

## Decision

1. **Two narrow shapes are answered at once and closed**, through every entry point (HTTP, MCP
   `describe_need`, A2A `SendMessage`), because they all create requests the same way:
   - the whole message (at most 80 characters) is a ping or a short health-check phrase from a fixed
     list ("ping", "health check", "are you there", ...). The answer is "pong" plus one sentence on what
     the service is and how to ask for real help, with a link to `/llms.txt`;
   - the whole message asks to reply with exactly one short word ("Reply with exactly OK", "Antworte genau
     mit READY"): letters, digits, `-` or `_`, at most 16 characters. The answer is that word and nothing
     else, so liveness checks that compare the answer pass.
2. **Nothing else is echoed or answered automatically.** Longer text, extra words, other characters or
   anything that merely contains "ping" is an ordinary request for the operator.
3. **The operator is not alerted** for these (no `request.created` event, no push). They are counted in
   the usage statistics as `autoreply:ping` and `autoreply:echo`, and remain visible as closed requests
   in the console. `AUTO_REPLY_CHECKS=false` turns the behaviour off.
4. The request is closed with `closed_by = auto`, which A2A shows as `TASK_STATE_COMPLETED`. An agent
   that writes to it again over HTTP reopens it like any closed request, and then the operator is alerted
   as usual.

## Consequences

- Agents that probe the service get the answer their check expects within one call.
- A real request that happens to be a single word like "test" is answered automatically too; the answer
  tells the agent how to ask for real help.
