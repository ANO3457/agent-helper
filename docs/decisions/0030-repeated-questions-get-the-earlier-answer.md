# 0030: Repeated questions get the earlier answer

- Status: accepted
- Date: 2026-10-19
- Related: [0003](0003-free-text-requests.md), [0012](0012-operator-notifications.md),
  [0026](0026-automatic-answers-to-connectivity-checks.md)

## Context

Agents often ask the same question again: a new instance of the same agent, a retry after a restart, or
different agents with the same need. Each repeat waited for the operator, who then typed the same answer
again. The operator asked that a question that was answered before is answered at once with the answer
sent before.

## Decision

1. **Every new request gets a question key** from its first message, through every entry point (HTTP,
   MCP `describe_need`, A2A `SendMessage`). The text is NFKC-normalised, lower-cased (case-folded), runs of
   whitespace become one space, and punctuation, symbols and spaces at both ends are removed. The key is
   the SHA-256 of that text. Only an exact match counts; anything that differs in a word, a letter or a
   comma inside the text is a different question.
2. **A template is an operator answer that came right after the first message** of an earlier request with
   the same key; automatic messages in between are skipped, so a correction of a repeated answer counts.
   A reply to a later agent message answers more than the question, so it does not count. The latest such
   answer is used.
3. **Only answers a person wrote count.** Automatic answers (connectivity checks, earlier repeats) are never
   templates. Neither are holding replies that left the request `open` ("looking into it"), nor referrals
   to another agent (the other agent never saw the new request). Replies from before this change count as
   `answered`; referrals from before it are recognised by their fixed text.
4. **The new request gets the template's text unchanged** as an operator message marked `automatic: true`
   (visible to the agent, in the admin API and in the console, which links the request it came from). Its
   status is the one the operator chose with the template: `answered`, or `closed` (then `closed_by = auto`,
   which A2A shows as completed). An agent that writes again reopens the request and the operator is
   alerted as usual (`request.message`).
5. **The operator is told**, but not as a new request: the event `request.auto_answered` (on by default in
   `NOTIFY_EVENTS`) names the new request and the request whose answer was used. The repeat is also counted
   as `autoreply:repeat` in the usage statistics. `AUTO_REPLY_REPEATS=false` turns the behaviour off.
6. Connectivity checks (0026) are matched first; a ping is never treated as a repeat.

## Consequences

- An agent that asks a known question gets its answer within one call, and the operator types it once.
- **An operator answer can reach other agents.** Whoever sends exactly the same first message gets it. An
  answer meant for one agent only (about its own account, task or data) should go to a follow-up message or
  be sent with status `open`, or the behaviour turned off. The exact match keeps accidental reuse unlikely;
  a deliberate guess still needs the exact question.
- An answer that became outdated is repeated until the operator replies again: an operator reply that
  directly follows an automatic answer (no agent message in between) is a correction and becomes the new
  template. With the behaviour turned off, new requests still get their key, so turning it on later works
  for them too.
- A deployment that sets `NOTIFY_EVENTS` explicitly must add `request.auto_answered` to hear about repeats.
