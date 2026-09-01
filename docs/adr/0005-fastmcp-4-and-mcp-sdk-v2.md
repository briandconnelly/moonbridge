# ADR 0005: Upgrade to FastMCP 4 / MCP SDK v2 and serve both protocol eras

**Status:** Accepted (2026-08-31). Supersedes [ADR 0004](0004-mcp-2026-07-28-migration.md).

## Context

ADR 0004 stayed on MCP 2025-11-25 and named its own revisit trigger: "**FastMCP/the `mcp` SDK
shipping 2026-07-28 support**, not a calendar date — re-run the same verification (installed-package
grep + the official spec pages) when that happens." That trigger has fired. FastMCP 4.0.0 shipped on
MCP Python SDK v2 (`mcp` 2.1.1), and the installed stack now implements the modern wire model that
ADR 0004 recorded as absent.

The upgrade was not optional. `pyproject.toml` declared `fastmcp>=3.4` with no upper bound, and
`.mcp.json` installs this plugin with `uvx --from git+<remote>@v<version>`, which resolves
dependencies fresh and **does not read `uv.lock`**. So every user's install picked up fastmcp 4.0.0
the day it was published, while the repo's own lock still pinned 3.4.7 and the gate stayed green.
The server failed at import for everyone:

```
$ uvx --from git+https://github.com/briandconnelly/moonbridge.git@v0.2.0 moonbridge-mcp
ImportError: cannot import name 'McpError' from 'mcp'. Did you mean: 'MCPError'?
```

That is the second, independent lesson here, and it is recorded in Consequences: an unbounded
dependency range plus a lockfile CI trusts is a gate that cannot fail on the thing that broke.

## Decision

**Upgrade to `fastmcp>=4,<5`, and serve both protocol eras.** FastMCP 4 negotiates per connection:
the handshake era (2025-11-25, via `initialize`) and the sessionless modern era (2026-07-28, via
per-request metadata and `server/discover`). This server serves both — that is FastMCP's behavior,
not a feature this repo implemented — and the agent-visible surface now says so.

The pin gains an upper bound. `fastmcp>=3.4` is what let a major release reach users unannounced;
`<5` converts the next one from a production break into a resolution failure at upgrade time.

### Roots: the probe is gone, and the value set says so

MCP SDK v2 removed `ctx.list_roots()` from `Context`. Verified rather than taken from the upgrade
notes: `hasattr(Context, "list_roots")` is `False`, and it is `False` on **both** eras — the removal
is not scoped to the sessionless one. `roots/list` was a server-to-client request, and the modern
protocol has no server-to-client request direction at all, so the pushed form has nowhere to go.

ADR 0004 pre-authorized exactly this: it recorded roots as an advisory fallback behind
`workspace_root`, and planned to "drop it only when/if the underlying MCP SDK removes roots
support." So `_roots_from_ctx` is now a constant returning `([], "unsupported")`.

`RootsSource` **gains** `"unsupported"` rather than narrowing to it. The three existing values
(`client`, `not_negotiated`, `probe_failed`) are retained but never emitted. Narrowing a published
value set is a breaking change under AGENTS.md → Versioning, and it would break a client validating
a stored envelope against the published enum for no gain; adding a value is not breaking. The cost
is 206 bytes on `tools/list`, since the enum is duplicated into every tool's `outputSchema` — paid
deliberately, and recorded in the wire-size budget.

Two surfaces become unreachable as a consequence, and are left advertised rather than removed:
`workspace_source == "roots"` and the `workspace_outside_roots` error code (with its
`candidate_roots` detail). Both can only arise from a non-empty roots list, which no longer occurs.
Removing them would be a second breaking change beyond what this upgrade requires, and the guard
pattern could make them reachable again. They are documented as inert; see Consequences.

### `-32002` becomes era-dependent

Serving the modern era means honoring its error-code renumbering. MCP 2026-07-28 replaced `-32002`
(resource not found) with `-32602` and states that implementations of that revision **MUST NOT**
emit the old code. The resource-read middleware therefore reads the negotiated
`protocol_version` off the request context and picks the code per era, failing safe to `-32002`
when the version cannot be read. A single constant would violate the spec on one era whichever
value were chosen. `resource_error_carrier` now documents the split and tells clients to branch on
the symbolic `error.data.code` instead.

### `protocol_revision` moves, and gains a companion

`protocol_revision` moves `2025-11-25` -> `2026-07-28` and keeps its documented meaning: the newest
revision served, not the per-session negotiated value. Because "the target" alone can no longer tell
a client whether the handshake era is still reachable, `protocol_eras_served` is added alongside it.
The manifest guards both eras under a new `protocol_eras` section and a matching
`FINGERPRINT_COVERS` token, captured from live handshakes rather than hardcoded, so an SDK that
silently stops serving an era moves the snapshot.

## Verified facts

Every claim below was produced by running the probe, not by reading the upgrade guide:

- `hasattr(Context, "list_roots")` is `False` under fastmcp 4.0.0 on `mode="legacy"` and
  `mode="auto"`. Pinned by `test_context_really_has_no_list_roots`.
- `McpError(ErrorData(...))` raises `TypeError: MCPError.__init__() missing 1 required positional
  argument: 'message'`; `McpError(code=..., message=..., data=...)` works and `err.error.code` /
  `err.error.data` still read back.
- `Client(mcp)` defaults to `mode="auto"` and negotiates `2026-07-28`, where
  `client.initialize_result` is `None`. `mode="legacy"` negotiates `2025-11-25` and returns an
  `InitializeResult`. The manifest pins `mode="legacy"` for that reason.
- `extensions` is a **declared field** on `ServerCapabilities` under SDK v2; under v1 it arrived
  only as an undeclared extra. The `model_extra`-only lookup this repo used therefore found nothing
  and nulled the whole key, suppressing every extension — caught by
  `test_ui_extension_filter_preserves_other_extensions`, which was confirmed to fail against the
  pre-fix code and pass after it.
- Without the capability seam the SDK advertises `io.modelcontextprotocol/ui` on the modern era
  (and `prompts` on both). The seam is still load-bearing; #424's goal holds on both eras.
- SDK v2 does not carry `extensions` into the handshake-era `initialize` result at all — a sentinel
  injected at the seam does not reach the legacy wire. That is why the extension test asserts on the
  modern era.
- `mcp.types.LATEST_PROTOCOL_VERSION` is `2026-07-28`; `mcp.shared.version` is gone and the
  constants moved to `mcp_types.version`, which splits `HANDSHAKE_PROTOCOL_VERSIONS` from
  `MODERN_PROTOCOL_VERSIONS`. The drift guard reads the split rather than a single constant.

## Consequences

- **The real fix reaches users only on release.** `.mcp.json` pins a git tag, so every install stays
  broken until a new tag is pushed. Follow [docs/RELEASING.md](../RELEASING.md); the unpushed-tag
  hazard it documents is the acute one here, because the currently pinned tag does not import.
- **Unbounded dependency ranges are now treated as a gate hole, not a style preference.** CI resolved
  from `uv.lock` and could not observe what `uvx` resolves. The upper bound is the immediate fix; the
  durable one is a CI job that installs the way users do. Not implemented here — recorded as the
  known gap.
- `FINGERPRINT` moves `schema-5` -> `schema-6`. Not breaking: every change is additive
  (`protocol_eras_served`, the `unsupported` enum value, reworded descriptions) or a value change to
  a field whose documented meaning is unchanged (`protocol_revision`).
- `RESULT_FORMAT` moves `8` -> `9`. A format-8 reader's closed `RootsSource` Literal rejects a record
  carrying `"unsupported"`, which this release stamps on every record; without the bump such a record
  would be misreported as corruption instead of a cross-release payload.
- The advertised `initialize` capabilities lost `experimental: {}` — an SDK v2 change to a capability
  this server never implemented.
- `workspace_source == "roots"`, `workspace_outside_roots`, and `candidate_roots` are advertised but
  unreachable. Tests that cover them now stub `_roots_from_ctx` and say so. Retiring them is a
  separate, breaking decision; revisit it if the guard pattern is ever adopted.
- FastMCP deprecation warnings are errors in pytest (`filterwarnings`), implementing step 12 of the
  upstream upgrade checklist: the camelCase compatibility bridge is scheduled for removal, so a
  surviving camelCase read is a future hard failure that would otherwise pass CI silently.
- Not adopted: the guard pattern (`InputRequiredResult`) for roots or elicitation, and the tasks
  extension. This server elicits nothing, samples nothing, and runs background work through its own
  `kimi_job_*` lifecycle rather than `task=True`.
