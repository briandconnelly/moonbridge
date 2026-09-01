# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-09-01

Upgrading is mandatory for anyone on 0.2.0: that release cannot import (see the first entry under
Fixed). This release is flagged breaking for three behavior changes that no schema diff surfaces —
they are marked **Breaking** below. `FINGERPRINT` moves `schema-5` -> `schema-6`; `RESULT_FORMAT`
moves `8` -> `9`.

### Added

- `kimi-code 0.39.1` to `SUPPORTED_VERSIONS`, after re-running the full probe set from
  `docs/UPGRADING-KIMI.md` against the binary (captures and results in `docs/kimi-help/0.39.1/`,
  `M0-FINDINGS.md`). No guarantee moved — the read-only `--agent-file` allowlist, the
  confidentiality limit, the silent-effort-ignore, the worktree's non-containment, the argv
  ceiling, and the `stream-json` line shapes all hold.
- `protocol_eras_served` on the capabilities surface: every protocol revision this server
  negotiates, read from the installed SDK. `protocol_revision` alone names only the newest
  revision served, so it can no longer tell a client whether the handshake era is reachable.
- `unsupported` as a `RootsSource` value (see the roots removal under Changed). It was added
  rather than replacing `client`/`not_negotiated`/`probe_failed`, so a stored envelope still
  validates against the published value set.
- A `protocol_eras` section in the manifest, with a matching `FINGERPRINT_COVERS` token,
  capturing each era's negotiated revision, advertised capabilities, and instructions from live
  connections. Capabilities are covered per era because the two genuinely differ (`listChanged`
  is true on the handshake era, false on the modern one).

### Changed

- **FastMCP 3.4 -> 4.0, MCP SDK 1.29 -> 2.1** (ADR 0005, superseding ADR 0004, whose own revisit
  trigger this was). The server now serves both protocol eras — the 2025-11-25 handshake and the
  sessionless 2026-07-28 — because FastMCP 4 negotiates per connection. Agent-visible
  consequences:
  - **Breaking:** a call that omits `workspace_root` now resolves somewhere else. MCP SDK v2
    removed `ctx.list_roots()` on every era (verified, not inferred: `hasattr(Context,
    "list_roots")` is False under both `mode="legacy"` and `mode="auto"`), so no roots probe runs
    and the client's roots can no longer supply a workspace. Such a call falls back to the
    server's own launch directory with `meta.workspace_warning`. A caller that relied on MCP roots
    to aim work at a repository will silently target the wrong directory — **pass
    `workspace_root`**. This is a behavior change, not a schema change, so no schema guard catches
    it for you.
  - **Breaking:** `meta.roots_source` is now always `unsupported`, a value no existing client has
    seen. Code branching on `client`/`not_negotiated`/`probe_failed` falls through every arm.
  - **Breaking:** an explicit `workspace_root` outside the client's advertised roots used to be
    refused at **zero spend** (`workspace_outside_roots`, carrying `candidate_roots`). With no
    roots to compare against, that refusal can no longer fire, so the same input now **runs and
    spends**. If you relied on it as an aim-check, you have lost it — validate the path yourself.
  - **Breaking:** resource-read not-found is era-dependent. `-32002` on the handshake era, but
    `-32602` on 2026-07-28, which forbids the old code — and a default FastMCP 4 client negotiates
    that era, so a client matching the numeric `-32002` stops matching. Branch on the symbolic
    `error.data.code` (`"resource_not_found"`) instead. `resource_error_carrier` documents the
    split.
  - `workspace_source == "roots"`, the `workspace_outside_roots` error code, and its
    `candidate_roots` detail are still advertised but unreachable, for the same reason. Retiring
    them would be breaking and is deliberately deferred.
  - `protocol_revision` moves `2025-11-25` -> `2026-07-28`, the newest revision served.
  - The advertised `initialize` capabilities lost `experimental: {}` — a field removal, and so
    breaking by the letter of the rules, though it announced a capability this server never
    implemented and no client could usefully have branched on.
  - Tool and capability descriptions no longer tell agents their MCP roots can select the working
    directory; they now say roots are unavailable and that `workspace_root` is what aims a call.
  - The `pydantic` floor moves `>=2` -> `>=2.12`, which MCP SDK v2 requires. A project pinning an
    older pydantic gets an unsatisfiable resolution rather than a silent downgrade.
- `pontonier` 0.5.0 -> 0.7.0. `CONTRACT_API_VERSION` stays 1. The one change that reaches an
  agent: `redaction.exc_summary` now strips Unicode `Cc` code points before redacting, so
  exception text carrying a control character produces different message text where this bridge
  feeds `exc_summary` into `error.message`. That is human-readable prose only, so it moves no
  fingerprint and is not breaking. It is a hardening — a control character wedged into a secret
  previously defeated the redactor's patterns outright.
- `FINGERPRINT` moves `schema-5` -> `schema-6` and `RESULT_FORMAT` moves `8` -> `9`. Every
  schema-level change above is itself additive or a value change to a field whose documented
  meaning is unchanged; the release is flagged breaking for the behavioral changes marked above.
  `RESULT_FORMAT` moves because a format-8 reader's closed `RootsSource` Literal rejects a record
  carrying `unsupported`, which this release stamps on every record — without the bump such a
  record would be misreported as corruption rather than a cross-release payload.

### Fixed

- **Every 0.2.0 install is broken; only upgrading fixes it.** `pyproject.toml` declared
  `fastmcp>=3.4` with no upper bound, and `.mcp.json` installs this plugin with
  `uvx --from git+<remote>@v<version>`, which resolves dependencies fresh and does not read
  `uv.lock`. When fastmcp 4.0.0 published, every user's install picked it up and the server failed
  at import — `ImportError: cannot import name 'McpError' from 'mcp'` — while CI stayed green
  against the locked fastmcp 3.4.7. The dependency is now `fastmcp>=4,<5`; the upper bound turns
  the next major from a production break into a resolution failure at upgrade time. Because
  `.mcp.json` pins a git tag, the fix reaches users only when they move to this release's tag.
- The `initialize` capability seam read `extensions` off `model_extra`, where MCP SDK v2 makes it
  a declared field. The lookup found nothing and nulled the whole key, which would have suppressed
  every extension the server legitimately advertises rather than only the unimplemented
  `io.modelcontextprotocol/ui` one.
- `classify_failure` now tests failure signatures in pontonier's shared precedence order
  (drift -> auth -> rate limit -> invalid model), which the library documents as the part every
  bridge must agree on. It previously tested `invalid_model` first and `auth` ahead of drift, so a
  run whose output carried both a contract-drift message and an invalid-model or auth message
  classified as `invalid_model`/`kimi_auth_required` here and `cli_contract_changed` under the
  shared classifier — masking the one signal that must stay loud. Verified against the 0.35.0 and
  0.39.1 captures: no captured kimi message changes classification. No error code, field, or
  documented meaning changes.
- An unresolvable `default_model` in `config.toml` is now diagnosed as itself. kimi emits
  `model X does not resolve to a configured provider` when `default_model` names an alias with no
  `[models."..."]` section, failing while it resolves the default and so reaching the classifier
  even on a run that passed a valid `--model`. That message previously matched no signature and
  surfaced as a generic error, pointing operators at an upgrade bug rather than at their own
  config; it now classifies as `invalid_model`. The repair hint for this cause names `config.toml`
  and drops `details.field = "model"` — the caller-supplied-`model` case is unchanged, but
  advising the configured-default case to omit `model` and fall back on `default_model` steered
  callers into a repair loop, since `default_model` is the broken thing.

## [0.2.0] - 2026-08-18

### Added

- `cli_contract.PONTONIER_CONTRACT` — the declarative CLI contract in the shared
  `BackendContract` shape, derivation-pinned against the legacy constants
  (including the verified silent-effort-ignore fact and field-scoped catalog
  authority: aliases authoritative, effort metadata advisory).
- `backend.KimiBackend` — this bridge's adapter on the frozen pontonier
  `AgentBackend` protocol (`contract_api_version = 1`), with an argv-differential
  test against the production command builder and a fail-open catalog-relative
  effort guard matching the server's stance. Every model-bearing run is staged
  through its `prepare()`, so that method cannot drift from production behavior.
  Its other methods (`validate_request`, `finalize`, `classify_failure`,
  `list_models`, `auth_probe`) have no production callers yet and are held to the
  pontonier conformance suite rather than to parity with the routes that do run;
  the two gaps this leaves are documented at their definitions.
- The surface-honesty phrase scan now runs through `pontonier.testing`; the
  moonbridge-specific quota/transfer/repair-hint honesty checks are unchanged.


- Codex plugin and marketplace manifests, with Codex installation instructions and a shared,
  host-neutral Kimi collaboration skill.
- `docs/RELEASING.md`, the ordered release runbook, and `docs/adr/README.md`, an ADR index that
  marks the records as decision history rather than current policy.
- A "Further reading" section in `README.md` that routes to every other document.

### Changed

- Every model-bearing run now goes through the pontonier `AgentBackend`
  adapter: `kimi.run_kimi_exec` stages via `KimiBackend.prepare()` — the
  out-of-workspace handshake dir, argv pointer, effort environment, the
  prompt-appended schema instruction, and help-gate drops (surfaced on the new
  `PreparedRun.dropped_flags`) — and keeps only the execution step (timeouts,
  byte caps, event streaming, orphan-sweep marker). The schema-instruction
  wording now has ONE source (`backend.schema_instruction`); the byte-parity
  test that pinned the old duplication is retired in favor of a behavioral pin
  that the instruction reaches the staged prompt file. Wire snapshots are
  byte-identical; argv is unchanged for every tier.


- Generic core machinery (`jobs`, `worktree`, `gitdiff`, `redaction`, `runtime`,
  `gitproc`, `streamcap`, `idempotency`, `workspace`, `jsoncache`) now comes from
  the shared [pontonier](https://github.com/briandconnelly/pontonier) library —
  this repo's `_core/` was its extraction source of record, so the code is the
  same (the redaction newline fix, worktree hardening, and orphan sweep all
  originated here). This bridge's worktree knobs (`moonbridge-worktree-` prefix,
  `moonbridge@local` baseline identity, the `.moonbridge` handshake-dir diff
  exclusion) are pinned in `config.WORKTREE_CONFIG`, so externally visible
  behavior is unchanged; all wire snapshots are byte-identical.


- `isolation` joins the shared parameter-contract registry: its 295-character inline
  description is now a 225-character summary, with AGENTS.md-always-loads, the skill-name
  egress path, and `extra_skill_dirs` served from `kimi://params`. It rides 8 tools, so this
  is ~560 bytes off `tools/list`. This reverses a 2026-07-26 decision that measured
  registration as a 88-byte *loss*; that measurement was correct for the summary it tested
  (the full text plus a pointer) and wrong as a permanent property of the parameter.
- The wire-size budget drops 87,500 → 83,000, banking both this and the vestige removal
  (which took 3,815 bytes out of every tool's error enum and branch). Measured
  `tools/list` is 82,866 bytes, down from 87,281.
- `test_summary_is_materially_shorter_than_what_it_replaced` becomes
  `test_registration_pays_for_itself_in_wire_bytes`: it asserted a 25%-shorter proxy, which
  misjudges a short description with wide fan-out. It now asserts measured bytes saved.
- **Breaking:** `kimi_status` no longer advertises a live quota read. Its `rate_limit` block
  always reports `unavailable` — kimi exposes no quota-read channel — so the description that
  taught agents to plan spend around `available`/`limited`/`exhausted`/`blocked` described states
  the server cannot produce. It now states the single reachable state and names
  `kimi_rate_limited` as the only real throttling signal. The `RateLimit` schema keeps its
  reserved fields, now documented as reserved.
- Tool descriptions no longer say `kimi exec` or "read-only sandbox". kimi has no `exec`
  subcommand and no sandbox; what constrains a read-only run is the generated agent profile.
  `cli_contract.FORBIDDEN_SURFACE_PHRASES` now owns that ban and
  `tests/test_surface_honesty.py` enforces it against the built manifest.
- Binding rules in the rewritten prose are now structurally separated from their rationale,
  after a `separating-context-from-constraints` audit found four R1 defects. The `RateLimit`
  schema description carried two rules across two paragraphs with no rule section — they now
  sit under a `RULES.` heading, with the rationale under `Why:` and the never-emitted fields
  under `Reserved shape:`. `kimi_status`'s spend rule gained a `Spend:` label, matching the
  `PAID —` / `Data egress:` blocks used everywhere else on this server. The `RateLimit`
  Codex-provenance aside moved to an internal comment: it informs a maintainer, not a caller,
  and it was shipping to clients as part of a schema description.
- Four guards added by the changes above were hardened after an independent Codex review found
  they overstated what they enforced. `test_status_result_rate_limit_is_structurally_unavailable`
  asserted only the schema default factory, so `kimi_status()` could return `available` with the
  test green; it is now a behavioral test across four readiness states. The repair-hint sweep's
  hand-maintained exclusion list held a phantom (`kimi_failed`, never an `ErrorCode`) and let
  prose like "retry kimi_rate_limited" pass; exclusions now derive from `ErrorCode`, `repair.tool`
  is validated with no exemption, and call-phrases bind tighter than the code-name exemption.
  The registration-saving test did character arithmetic against stale historical constants; it
  now measures serialized UTF-8 bytes of `full` against `summary`, so no baseline can go stale.
  Fan-out exemptions pin the byte cost they were granted at, so an exempt parameter cannot grow
  without forcing a re-measurement. Each guard now has a mutation test proving it fails on its
  target defect.
- The `RateLimit` `RULES.` block said to treat "every field and state below" as unobservable,
  contradicting the preceding sentence that `status: unavailable` is what you always get. It now
  names that state as the single observable one.
- `FINGERPRINT` is `moonbridge/0.1/schema-5`. `RESULT_FORMAT` is unchanged: only the `schemas`
  view of the result-format snapshot moved, the `serialized` view is byte-identical, and no
  stored job record can contain a removed code or the never-populated field.

### Removed

- Codex-port residue that outlived the fork, none of it wire-visible: the inert
  `DISABLE_FEATURE_FLAG`/`REMOTE_PLUGIN_FEATURE` names, `run_kimi_exec`'s
  accepted-but-unused `add_dirs`/`skip_git_repo_check`/`ephemeral` parameters,
  the dead `TransferMeta`/`TransferResult`/`TRANSFER_SCHEMA` models (no
  `kimi_transfer` tool exists), the uncalled
  `cli_contract.is_reasoning_effort_rejection` predicate together with the
  `REASONING_EFFORT_REJECTION_MARKERS` and `REASONING_EFFORT_TOKEN_PATTERN` it
  read (M0-6 established kimi never rejects an unrecognized effort, so the
  markers described a classifier branch that cannot exist), and stale docstrings
  describing an in-worktree handshake and an `--output-last-message` answer
  channel that kimi does not have (the handshake lives outside the workspace;
  the answer comes from the event stream / answer file).


- **Breaking:** the `transfer_unsupported`, `transfer_failed`, and `transfer_incomplete` error
  codes, and the `app_server_stderr_tail` field on the error envelope. All four were inherited
  from the Codex plugin this server was ported from: kimi has no session-transfer equivalent, so
  no handler could emit them, while their repair hints told agents to retry a `kimi_transfer`
  tool that does not exist (and to install `@openai/kimi`). Restore them only alongside a real
  `kimi_transfer` tool.

### Fixed

- Multi-line private-key blocks (PEM/PKCS8/OpenSSH/PGP) in gathered diffs and
  returned prose are now redacted statefully (via pontonier 0.4.0): the
  BEGIN/END markers stay visible, every body line between them is dropped, and
  an unterminated block fails closed. Previously only the BEGIN marker was
  masked while the entire base64 body was sent.
- A model's declared `minimal` and `xhigh` reasoning efforts are no longer
  refused pre-spend. The model-catalog parser gated `supportEfforts` and
  `defaultEffort` on a fixed `low|medium|high|max` vocabulary, so those two
  levels were silently dropped from what an alias declares. The truncated set is
  still non-empty, so the pre-spend guard did not fail open: it refused
  `reasoning_effort="minimal"` as "not one this model declares" — for a level the
  model does declare — and its repair hint steered agents to the truncated list.
  `kimi_models` and `kimi://models` under-reported the same set. The parser now
  gates on SHAPE, not vocabulary, so any level a model declares survives,
  including ones kimi adds later; malformed tokens are still dropped. Pre-existing
  (not introduced by the pontonier migration).
- Redaction now covers five more credential formats, inherited with pontonier
  0.5.0: fine-grained GitHub tokens (`github_pat_`), GitLab PATs (`glpat-`),
  Anthropic keys (`sk-ant-`), npm tokens (`npm_`), and PyPI tokens (`pypi-`).
  Previously only `AKIA`, `ghp_`, and `xoxb` were masked, so the other five
  reached callers verbatim in gathered diffs and returned prose. This widens what
  is masked, so a literal string in those shapes (a docs example, a test fixture)
  is now replaced in returned text.
- `CONTRIBUTING.md` pointed at an `AGENTS.md` section that does not exist and described an
  issue-claim protocol that `AGENTS.md` says does not exist.
- The allowed commit scopes had no documented home, so the rule requiring
  `scripts/check_commit_message.py` and the docs to mirror each other named no target.
- The release procedure was stated differently in `AGENTS.md` and the pull-request template; both
  now link to the runbook.

## [0.1.0] - 2026-08-12

First release.

### Added

- Claude Code plugin and MCP server that call the Kimi Code (`kimi`) CLI: `kimi_consult` for a
  read-only second opinion, `kimi_review_changes` for a structured review of changes gathered from
  git, and `kimi_delegate` for a coding task that returns a reviewable diff rather than editing the
  working tree. Each has an `_async` job variant, with `kimi_job_*` tools to poll, collect, and
  cancel.
- `runspace.run_isolated`: every tier runs in a throwaway git worktree, with consult and review
  additionally constrained by a generated read-only agent profile (no shell, no write tools).
- Orphan sweep in `_core/runtime.py`: kimi's shell tool spawns each command in its own process
  group, so a process-group kill leaves survivors. Swept by the run's unique worktree path on
  timeout, cancellation, and normal completion.
- Local pre-spend validation of `reasoning_effort` against the model alias's declared efforts,
  because kimi silently ignores an unrecognized effort rather than rejecting it.
- `model` takes an alias from the user's `config.toml`. The catalog is read live from
  `kimi provider list --json`, and the provider `apiKey` in that payload never reaches an envelope.
- `MOONBRIDGE_EXTRA_ARGS` refuses every value: kimi exposes no safe passthrough and reuses
  `-p`/`-c` for prompt and continue.
- `.mcp.json` installs the server from the public GitHub remote at a pinned release tag
  (`uvx --from git+…@v0.1.0`), so installing needs no clone. `tests/test_packaging.py` asserts the
  pin tracks the version in `pyproject.toml`.
