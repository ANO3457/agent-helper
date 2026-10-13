# 0024: A2A 0.3 clients, and answers for often-probed discovery files

- Status: accepted
- Date: 2026-10-13
- Related: [0011](0011-discovery-channels.md), [0019](0019-a2a-adapter.md),
  [0021](0021-finding-help-in-one-call.md), [0023](0023-anonymous-usage-counts.md)

## Context

The anonymous usage counts (0023) showed that A2A clients speaking protocol version 0.3 reach `/a2a`
and are refused: the adapter (0019) served only A2A 1.0, and a request without the `A2A-Version` header
means 0.3. The counts also showed requests for files this service did not have, such as
`/.well-known/agent.json`, `mcp.json`, `ai-plugin.json`, `agents.json`/`agents.txt` and
`/.well-known/oauth-protected-resource`. Each of these is a client that was looking for us and left.

## Decision

1. **A2A 0.3 is served at the same endpoint.** A request without `A2A-Version` (or with `0.3`/`0.3.0`)
   that uses the 0.3 method names is translated to the 1.0 handlers: `message/send` → `SendMessage`,
   `tasks/get` → `GetTask`, `tasks/cancel` → `CancelTask`. Messages come in with `role: "user"` and parts
   `{"kind": "text", "text": ...}`; answers go back in 0.3 form: the task itself as the result (also for
   `message/send`), `kind: "task"`/`"message"`, lower-case states (`submitted`, `input-required`,
   `completed`, `canceled`) and roles (`user`, `agent`). Everything else is unchanged: the same rate
   limits, the follow-up token as a Bearer header, text parts only, the same error codes (`message/stream`,
   `tasks/resubscribe` and the push-config methods are refused as unsupported).
2. **The versions do not mix.** 1.0 method names still need `A2A-Version: 1.0`; 0.3 names under a 1.0
   header are unknown methods. A client therefore always gets one consistent dialect.
3. **Agent cards.** The 1.0 card at `/.well-known/agent-card.json` stays the primary one and stays 1.0;
   it now lists a second interface entry with `protocolVersion: "0.3"` for the same URL. The legacy path
   `/.well-known/agent.json`, where 0.3 clients look, serves the same agent in the 0.3 card format.
4. **Often-probed files answer or point to the real ones,** all public and read-only:
   - `/.well-known/mcp.json` and `/mcp.json`: the MCP server card (0021).
   - `/.well-known/ai-plugin.json`: the old plugin manifest, pointing at `/openapi.json`, `auth: none`.
   - `/agents.json` and `/.well-known/agents.json`: a small index of where the real descriptions are. It
     does not claim to implement any particular agents.json format.
   - `/agents.txt` and `/.well-known/llms.txt`: the same text as `/llms.txt`.
   - `/.well-known/oauth-protected-resource` (also with a path suffix), `oauth-authorization-server` and
     `openid-configuration` stay **404**, because a 404 is how OAuth-aware MCP clients learn that no
     authorization is needed; the body says so in words.
   The usage counts record these as answered (`compat:...`) instead of probes.

## Consequences

- 0.3 clients can use the service without changes on their side; the 1.0 path is untouched.
- A second dialect is more code to keep correct; it is a thin translation layer over the 1.0 handlers,
  covered by its own tests, and can be removed once 0.3 traffic disappears from the counts.
