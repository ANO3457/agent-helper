# 0031: MCP over stdio for local clients and registry checks

- Status: accepted
- Date: 2026-10-20
- Related: [0028](0028-more-entry-points-agents-try.md)

## Context

The MCP endpoint speaks Streamable HTTP only. Some MCP clients, and the automated checks of MCP
registries, start a server as a local process and talk to it over stdin and stdout. Such a check builds
the repository, starts the server behind a stdio proxy and asks it for its tools; an HTTP-only server
exits before it answers, and the listing fails its checks.

## Decision

1. **`python -m agent_helper.stdio`** (also installed as `agent-helper-stdio`) reads one JSON-RPC message
   per line from stdin and writes each answer as one line to stdout. Logs go to stderr only.
2. **Each line goes through the same app** as a POST to `/mcp`, in process and with the middleware, so the
   tools, the protocol versions, the limits and the error answers are exactly those of the HTTP endpoint.
   Notifications get no line back, as over HTTP they get 202 and no body.
3. **The headers that modern MCP requires over HTTP** (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`)
   do not exist on stdio; they are derived from the message itself.
4. **It is a separate local instance** with its own `DATA_DIR`, database and instance key. It does not
   forward anything to another instance, and nothing reaches the operator of a public instance through it.

## Consequences

- A registry can start and inspect the server without credentials or network access.
- A stdio client that wants the public instance still connects to its `/mcp` URL over HTTP; the stdio mode
  only helps someone who runs their own instance.
- The local process is a single client, counted and rate limited as one address.
