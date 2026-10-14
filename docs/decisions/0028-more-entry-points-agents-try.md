# 0028: More entry points that agents and tools try

- Status: accepted
- Date: 2026-10-14
- Builds on [0019](0019-a2a-adapter.md), [0021](0021-finding-help-in-one-call.md),
  [0024](0024-a2a-0-3-and-common-discovery-files.md), [0025](0025-refused-calls-and-daily-summary.md)

## Context

A week of usage counts showed many calls the service did not answer usefully: `GET`/`HEAD`/`OPTIONS` on
`/mcp` (405), the A2A card next to the endpoint (`/a2a/.well-known/agent-card.json`), the A2A HTTP+JSON
binding (`POST /a2a/v1/message:send`, also appended to the card's URL), `GET /a2a` and `/a2a/health`,
OAuth discovery paths with an `/mcp` suffix, `/favicon.ico`, payment manifests, and the MCP endpoint
under other URLs (`/sse`, `/mcp/sse`, `/api/mcp`, `/mcp/mcp`, `/a2a/mcp`, or appended to a discovery
file's URL). MCP clients also called `prompts/list` and `resources/list` and got errors.

## Decision

All of these are fixed routes; there are no wildcards, and each is counted under a fixed usage name.

1. **HEAD answers like GET without a body** for every route (RFC 9110).
2. **MCP endpoint.** `POST` is unchanged. `GET` with `Accept: text/event-stream` still answers 405, as the
   Streamable HTTP transport requires of a server without a standalone stream; any other `GET` gets a
   short JSON description (how to call it, versions, server card, docs). `OPTIONS` answers 204 with
   `Allow` (no CORS is opened by it), `DELETE` 405 (no sessions). The alias URLs listed above behave
   exactly like `/mcp`. `prompts/list`, `resources/list` and `resources/templates/list` return empty
   lists instead of "method not found".
3. **A2A.** `GET /a2a` describes the endpoint, `/a2a/health` answers `{"status": "ok"}`,
   `/a2a/.well-known/agent-card.json` serves the 1.0 card. The **HTTP+JSON binding** is served at
   `POST /a2a/v1/message:send` (also at `/.well-known/agent-card.json/v1/message:send`, where some clients
   append it), `GET /a2a/v1/tasks/{id}` and `POST /a2a/v1/tasks/{id}:cancel`, through the same handlers,
   limits and Bearer token as JSON-RPC. With `A2A-Version: 1.0` the shapes are 1.0; without it, 0.3
   (0024). Errors carry the HTTP status and the A2A error name. Both cards list the binding.
4. **OAuth discovery** (`oauth-protected-resource`, `oauth-authorization-server`, `openid-configuration`,
   each also with `/mcp`) stays 404 with a body saying that no authorization is needed. RFC 9728
   metadata describes a *protected* resource; publishing it without an authorization server would invite
   clients to start an OAuth flow that cannot succeed.
5. **Small files.** `/favicon.ico` answers 204. Payment manifests (`/.well-known/x402`, `x402.json`,
   `payment-manifest.json`, `mpp.json`) say the service is free and takes no payments. `/mcp/server-card`,
   `/mcp/server-card.json` and `/.well-known/mcp` serve the MCP server card; `ard.json`,
   `agent-directory.json` and `ai-catalog.json` under `/.well-known/` serve the index of the real
   descriptions (no standard format exists for them that this service could fill honestly).
   `/.well-known/glama.json` (Glama's ownership check for listed MCP servers) is served only when the
   operator sets `GLAMA_MAINTAINER_EMAIL`; the address is live configuration, not part of this repository.

## Consequences

- More clients get a working answer or a clear pointer on their first try.
- Each alias is one more URL to keep working; they are listed in one place (`discovery.py`) and covered
  by tests, and the usage counts show which ones are actually used.
