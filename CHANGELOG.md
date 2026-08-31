# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- `pontonier` 0.5.0 -> 0.7.0. Two upstream minors, both compatible with this bridge:
  `CONTRACT_API_VERSION` stays 1 and the 0.7.0 addition (`RunRequest.instructions_append`)
  is a defaulted field this bridge does not set. The one entry that reaches an agent is
  0.6.0's: `redaction.exc_summary` now strips Unicode `Cc` code points before redacting,
  so exception text carrying a control character produces different message text. Verified
  by running it — an `ESC`/`NUL`-bearing exception comes back stripped. This bridge feeds
  `exc_summary` into `error.message` prose in three places, which is human-readable prose
  only, so per Versioning it moves no fingerprint and is not breaking; the built manifest
  is byte-identical. The change is a hardening: a control character wedged into a secret
  previously defeated the redactor's patterns outright.

### Added

- `kimi-code 0.39.1` to `SUPPORTED_VERSIONS`, after re-running the full probe set from
  `docs/UPGRADING-KIMI.md` against the binary. Captures and results:
  `docs/kimi-help/0.39.1/` (`M0-FINDINGS.md`). No guarantee moved — the read-only
  `--agent-file` allowlist, the confidentiality limit, the silent-effort-ignore, the
  worktree's non-containment, the argv ceiling, and the `stream-json` line shapes all
  hold, and the agent-visible manifest is byte-identical, so `FINGERPRINT` is unchanged.

### Fixed

- A second captured invalid-model message,
  `model X does not resolve to a configured provider`, now classifies as `invalid_model`.
  kimi emits it when `config.toml`'s `default_model` names an alias with no
  `[models."..."]` section, failing while it resolves the default and so reaching the
  classifier even on a run that passed a valid `--model`. It previously matched no
  signature and surfaced as a generic error, pointing operators at an upgrade bug rather
  than at their own config. The pattern is anchored on kimi's `failed to run prompt:`
  prefix: its tail is ordinary English, and `classify_failure` tests invalid_model first
  over the model's own output, so an unanchored clause could mask genuine contract drift.
- An unresolvable `default_model` no longer blames the caller's `model` argument. Both
  causes still share the `invalid_model` code, but the configured-default case now names
  `config.toml` and drops `details.field = "model"` — previously it advised omitting
  `model` to fall back on `default_model`, the one action that cannot help when
  `default_model` is itself the broken thing, leaving a caller circling a repair loop.

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
