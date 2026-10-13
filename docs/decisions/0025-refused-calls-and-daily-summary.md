# 0025: Counting refused calls, and a daily summary to the operator

- Status: accepted
- Date: 2026-10-13
- Builds on [0012](0012-operator-notifications.md) and [0023](0023-anonymous-usage-counts.md)

## Context

For days, A2A 0.3 clients were refused at `/a2a` (fixed in [0024](0024-a2a-0-3-and-common-discovery-files.md)).
The operator never noticed: a refused call creates no request, so no notification is sent, and the usage
counts only showed one aggregated number on a console page nobody had reason to open. The operator asked
to learn about such refusals.

## Decision

1. **Refused calls are counted by kind, never by content.** Each one adds to a daily count named
   `refused:<protocol>:<method>:<reason>` for A2A and MCP JSON-RPC errors (method from the fixed list of
   known methods, else `unknown`; reason from the fixed list of error codes, else `other`), and
   `refused:http:<endpoint group>:<status>` for 4xx answers on `/v1/`. As in 0023: no address, no full
   User-Agent, no request content, only the client family.
2. **A daily summary goes to the operator webhook** when calls were refused on the previous day (UTC):
   event `refusals.daily` with a one-line `text` (for chat forwarding), the total, the 15 most frequent
   kinds with client family, and a link to the admin usage API. It is sent at most once per day (the day
   sent is kept in the database) and only if the webhook is configured. It is the service's own report,
   so `NOTIFY_EVENTS` does not filter it; `NOTIFY_REFUSAL_DIGEST=false` turns it off. No refusals, no
   message.

## Consequences

- The operator learns within a day when agents are turned away, with enough detail to see why (for
  example `37x a2a:SendMessage:version_not_supported`), but still nothing about who they were or what
  they wanted.
- Ordinary mistakes (a mistyped token, an invalid field) also appear. That is intended: many of them in a
  row point to a confusing interface.
