# Architecture decision records

ADRs record decision history. They are not current policy. Current policy lives in
[AGENTS.md](../../AGENTS.md) and the documents it links.

| Number | Title | Status | What it decided |
|---|---|---|---|
| 0001 | [Keep sync and `_async` tools separate](0001-keep-sync-and-async-tools-separate.md) | Accepted | Keep separate MCP tools for synchronous calls and background jobs. |
| 0002 | [Omit null `meta` members on delivery](0002-omit-null-meta-on-the-wire.md) | Accepted | Omit null `meta` members from delivered success envelopes only. |
| 0003 | [Fetch schemas without the tool inventory](0003-fetch-schemas-without-the-inventory.md) | Accepted | Add `detail="contracts"` to omit `tool_details` from capability results. |
| 0004 | [Stay on MCP 2025-11-25](0004-mcp-2026-07-28-migration.md) | Superseded by [0005](0005-fastmcp-4-and-mcp-sdk-v2.md) | Keep MCP 2025-11-25 and record the migration plan for MCP 2026-07-28. |
| 0005 | [FastMCP 4 / MCP SDK v2](0005-fastmcp-4-and-mcp-sdk-v2.md) | Accepted | Upgrade to FastMCP 4, serve both protocol eras, and retire the roots probe. |

A superseded ADR keeps its file. Change its `Status` line to link forward to its replacement.
