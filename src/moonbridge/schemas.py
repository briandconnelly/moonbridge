"""Pydantic models for the normalized tool result contract."""

from __future__ import annotations

import copy
import math
from typing import Annotated, Any, Literal
from uuid import uuid4

from pontonier.core.jobs import DEFAULT_POLL_AFTER_MS
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from moonbridge import __version__

# The agent-visible surface the FINGERPRINT covers, as granular machine-readable
# identifiers. This tuple is the single source of truth: CapabilitiesResult.fingerprint_covers
# derives from it (audit F6, #178), so a client can reason about the fingerprint's
# cache-invalidation scope programmatically instead of reading this comment. Bump FINGERPRINT
# whenever the CONTRACT within one of these categories changes. Keep the tokens stable — they
# are themselves part of the surface (a rename is a covered change under `capabilities_payload`).
#
# Coverage is over contract semantics, NOT release identity: the per-category comments below
# carry the carve-outs, and _FINGERPRINT_COVERS_DESC discloses them to clients (#337).
FINGERPRINT_COVERS: tuple[str, ...] = (
    # Tool surface (manifest `tools` section)
    "tool_names",
    "tool_input_schemas",
    "tool_output_schemas",
    "tool_descriptions",
    "tool_annotations",
    "error_codes",  # the per-tool ErrorCode set
    "value_enums",  # the tier/sandbox/isolation/scope value sets
    # Resource + prompt surface (manifest `resources`/`resource_templates`/`prompts`)
    "resource_metadata",  # resource-record metadata (the resources/list wire fields)
    "resource_templates",  # resource-template wire shapes
    "prompts",  # prompt wire shapes (name/description/arguments)
    # Server-level surface
    # serverInfo/protocolVersion/advertised capabilities/instructions — minus the
    # release-variable `serverInfo.version` (#337). Handshake-era only: `initialize` does
    # not exist on the sessionless era (ADR 0005), which `protocol_eras` covers instead.
    "initialize_response",
    # the set of MCP protocol revisions the server negotiates (ADR 0005). Dropping an era
    # changes what a client can connect with, so it is contract, not release identity.
    "protocol_eras",
    "error_envelope_schema",  # kimi://error-envelope content
    "result_meta_schema",  # kimi://result-meta content
    "capabilities_result_schema",  # kimi://capabilities-result content (#242)
    "status_result_schema",  # kimi://status-result content (#242)
    "parameter_contracts",  # kimi://params content (#333) — compressed param summaries + full text
    # the kimi_capabilities result body — minus its release-identity fields
    # `version`/`server_version` (#337)
    "capabilities_payload",
    "capability_guarantees",  # the semantic promises within it (annotations/tiers/error contracts)
)

# The carve-out above, stated where a CLIENT can read it: without this the promise lived only
# in the comments here, and the advertised guarantee was broader than the implemented one
# (#337). Deliberately NOT an exhaustive list of everything the guard normalizes away — the
# snapshot also strips framework-owned `_meta` and sorts set-like arrays, both non-contractual
# — so it promises a scoped semantic (contract identity, not release identity) that stays true.
# Registered in _KEPT_DESCRIPTIONS so it survives into the advertised outputSchema.
_FINGERPRINT_COVERS_DESC = (
    "A contract-semantic change in any listed category changes the fingerprint; nothing "
    "outside them does. Release identity is excluded: serverInfo.version, version, "
    "server_version change every release WITHOUT moving the fingerprint."
)

# Bump this whenever the agent-visible surface changes — see FINGERPRINT_COVERS above for the
# exact categories. Clients cache by it. The committed manifest snapshot
# (tests/fixtures/manifest_snapshot.json, guarded by tests/test_manifest.py) fails CI on any
# covered change, so such a change can't land unreviewed; its failure message directs you to bump
# this and regenerate the fixture in the same commit. It is an acknowledgment guard — it surfaces
# the drift, it does not mechanically force the integer bump (the snapshot and this string are
# independently editable).
FINGERPRINT = "moonbridge/0.1/schema-6"

# The persisted result-format version, stamped into each job record's generic metadata
# (`extra.result_format`) at spawn so replay can tell a cross-release payload from a corrupt
# one (#305): on a validation failure, a stored value differing from this constant means the
# record was written under a DIFFERENT persisted format (job_result_incompatible, permanent);
# an equal or missing/unusable value means corruption (internal_error). Independent of
# FINGERPRINT: that tracks the agent-visible contract and moves on wording-only changes,
# while this tracks only the bytes replay must re-read. Bump it on any change to the
# persisted result.json shape an older reader's closed schema could reject — a field
# added/renamed/retyped on the result models/Meta/error envelope, a new Literal/enum value,
# or a serializer-mode change (e.g. exclude_none). The committed format snapshot
# (tests/fixtures/result_format_snapshot.json, guarded by tests/test_result_format.py) fails
# CI without a bump on drift in either the model schemas or the rendered writer output (its
# `serialized` view pins the writers' serializer modes); only a mode change the representative
# envelopes don't exercise escapes it and relies on this rule plus review.
# ErrorInfo's `resource_uri`/`request_id` (F6, #185) moved the `schemas` view of the snapshot
# but deliberately did NOT bump this: both fields are populated only by the resource-read
# middleware, which never writes result.json, so every persisted path keeps serializing them
# as absent (`exclude_none`) — the `serialized` view is byte-identical before/after. A bump
# here would have made every already-stored job result unreadable
# (`server.py`'s replay check treats any other value as `job_result_incompatible`) for zero
# compatibility gain. Regenerate the fixture to acknowledge the schema-view drift; only bump
# the integer when the `serialized` view itself would actually move.
# F8 (#roots_source): bumped 6->7. Meta.roots_source is new, extra="forbid", and dump_success
# has no exclude_none, so every already-persisted success envelope's replay would need to
# tolerate an unknown key an older reader's closed schema would reject — the exact case this
# field exists to catch. Verified (not assumed): the `serialized` view's consult/review/delegate
# success entries each gained "roots_source": null; the `error` entry did not move (serialize_error
# uses exclude_none, and the representative error envelope leaves this field unpopulated, so it
# serializes absent there) — the same asymmetry #185's ErrorInfo fields relied on to NOT
# bump, except here the success side (which DOES retain nulls) moved, so this one must bump.
# #393 later made the worker POPULATE roots_source, so a real stored error (the worker's crash
# sink builds its meta from the same spec) can now carry the key where it previously never did.
# That does NOT move this integer: the field is already known at format 7, so a format-7 reader
# accepts it — only an UNKNOWN key would trip a closed schema. The representative envelope above
# still leaves it null, so the `serialized` view is unchanged and the snapshot stays green.
# #433 bumped 7->8: `Coverage` gained `redaction: RedactionSummary | None = None`. The field
# itself defaults to None (see its own comment on `Coverage.redaction`) so an OLDER stored
# Coverage without the key still replays — that is exactly why the default is required, not
# optional. What moves the integer is the REPRESENTATIVE `review_success` envelope in
# result_format_snapshot.py, which the null-fixtures convention (#400) requires to populate
# every producible optional somewhere so its future deletion is visible: it now carries a
# non-null, non-empty RedactionSummary, so the `serialized` view's `review_success` entry
# gains the key WITH a populated value — a real shape addition to what a replaying reader
# must be able to parse, not merely a schema-only change.
# ADR 0005 bumped 8->9: `RootsSource` gained the value "unsupported", and this release
# stamps it on EVERY new record (the roots probe is gone, so it is now the only value the
# server emits). The rule above names "a new Literal/enum value" explicitly, and this is
# the case it was written for: a format-8 reader's closed Literal
# ["client","not_negotiated","probe_failed"] REJECTS a record carrying "unsupported", so
# without a bump such a record would be misreported as corruption (internal_error) instead
# of a cross-release payload (job_result_incompatible, permanent). Widening runs the safe
# way round in the other direction — this release's reader accepts every format-8 value,
# so already-stored records still replay.
#
# Note the `serialized` view does NOT move here, unlike #433: the representative envelopes
# leave `roots_source` null, so only the `schemas` view drifts. That asymmetry did NOT
# bump for #185, and the difference is that #185's fields could never be persisted at all,
# while this value is persisted on every record — the schema view is what a replaying
# reader validates against, and it is the thing that changed.
RESULT_FORMAT: int = 9


# The release that produced this envelope. Beside `fingerprint` on every result surface:
# fingerprint = contract identity ("which surface does this conform to?", a cache key),
# server_version = release identity ("which build produced this run?", provenance).
#
# default_factory, NOT a literal default, for two independent reasons:
#   1. A literal default leaks its value into the published JSON schemas
#      (RESULT_META_SCHEMA, ERROR_ENVELOPE_SCHEMA), which are snapshotted and guarded —
#      it would trip the manifest guard on EVERY release.
#   2. It cannot be forgotten at a construction site, unlike explicit population.
#
# `| None` is load-bearing: a job payload persisted by an older build carries no version,
# and on replay it must stay ABSENT rather than be stamped with the replaying server's
# version (see server.py::_finished_job_envelope).
def _server_version_field() -> Any:
    return Field(
        default_factory=lambda: __version__,
        description=(
            "moonbridge package version attributed to this result. A replayed done-job "
            "result preserves the version of the run that produced it; every freshly built "
            "envelope reports the responding server. Omitted entirely when a stored payload "
            "predates this field — never backfilled, and never sent as null."
        ),
    )


# Default poll/backoff interval (ms) shared by job handles and the job_running
# error's retry_after_ms, so the "when to retry" hint stays consistent in one place.
# Sourced from _core (the lower layer that owns the value) so a live job record's
# poll_after_ms and this constant can never drift.
JOB_POLL_AFTER_MS = DEFAULT_POLL_AFTER_MS

# Reasoning-effort description constants (#309), shared between the model fields that
# carry them and _KEPT_DESCRIPTIONS (which lets them survive schema-noise stripping so
# the null/[] semantics are agent-visible in the advertised outputSchemas).
_DEFAULT_EFFORT_DESC = (
    "This model's default reasoning effort as advertised by Kimi's on-disk cache; "
    "advisory only. null = the cache advertised nothing usable (the static fallback "
    "never carries effort data)."
)
_SUPPORTED_EFFORTS_DESC = (
    "Effort tokens this model advertises in Kimi's on-disk cache; advisory only — "
    "the backend's accepted set varies by account and an unlisted effort may still "
    "work. null = the cache advertised nothing usable; [] = an explicitly empty "
    "advertised set."
)
_DRY_RUN_MODEL_DESC = (
    "Model override the previewed paid call would send (per-call param or server "
    "default); null = Kimi would resolve it itself. Unvalidated by the preview."
)
_DRY_RUN_EFFORT_DESC = (
    "Reasoning-effort override the previewed paid call would send (per-call param or "
    "server default); null = Kimi would resolve it itself. Unvalidated by the preview."
)
# #342: a hint, not a refusal — every other preview field is unaffected. null unless
# the previewed call would actually run the model AND its size or effort risks the
# deadline (a call that runs no model can't overrun one; on kimi_dry_run that's
# would_call_model=False). Shared verbatim between DryRunResult and
# DelegateDryRunResult so the two previews can't drift (#342). Round-3 Kimi-review
# correction: the text must name the PREVIEWED PAID tool's own `_async` counterpart
# (kimi_review_changes_async / kimi_delegate_async) — NOT this dry-run tool's own
# name, which has no `_async` variant; an agent following a generic "this tool's
# _async variant" phrase from kimi_dry_run's own payload would attempt the
# nonexistent kimi_dry_run_async. kimi_consult has no dry-run preview at all — this
# field doesn't exist there — so its description below carries the one-clause pointer
# instead.
_DEADLINE_ADVISORY_DESC = (
    "Non-null when the previewed call would actually run the model AND its prompt "
    "size or reasoning effort risks the synchronous deadline; null otherwise "
    "(including whenever it would not call the model at all). Names the previewed "
    "PAID tool's own `_async` counterpart verbatim (e.g. kimi_review_changes_async "
    "for a kimi_dry_run preview) — never this dry-run tool's own name, which has no "
    "`_async` variant. A hint, not a refusal. kimi_consult has no dry-run preview; "
    "prefer kimi_consult_async for a high-effort or broad repo-grounded consult."
)

Severity = Literal["critical", "high", "medium", "low", "nit"]
Verdict = Literal["pass", "concerns", "fail", "unknown"]
Confidence = Literal["low", "medium", "high"]
# Intent tier: read-only consult, write-in-worktree propose (diff returned, not
# applied), or write-in-place apply (explicit opt-in).
Tier = Literal["consult", "propose", "apply"]
Sandbox = Literal["read-only", "workspace-write", "danger-full-access"]
# Isolation levels. kimi has no --ignore-user-config equivalent — dropping its config would
# also drop the provider credentials the run authenticates with — so the only lever is which
# skill directories load:
#   inherit       -> kimi's own discovery (user + project skills, plus extra_skill_dirs)
#   ignore-skills -> --skills-dir <empty>, replacing the discovered user/project dirs
# kimi's BUILT-IN skills always load under both (verified on 0.35.0); see
# cli_contract.SKILLS_ISOLATION_NOTE.
Isolation = Literal["inherit", "ignore-skills"]
ReviewScope = Literal["working_tree", "branch", "commit"]

# How a working_tree review treats untracked (never-committed) files. `explicit_only`
# preserves #74 (include only untracked files named in `paths`); `include` sends every
# non-ignored untracked file's contents to OpenAI (opt-in egress); `exclude` never
# includes them. Inert for branch/commit scopes.
Untracked = Literal["explicit_only", "include", "exclude"]
Detail = Literal["summary", "full"]
# kimi_capabilities-ONLY detail domain (#339). Deliberately a separate Literal rather than
# a third member of `Detail`: that type is shared by consult/review/delegate and the two job
# result-readers, where "contracts" has no meaning, so extending it would widen five
# unrelated tools' accepted input. `contracts` drops `tool_details` — the one heavy field
# already absent from both published schemas' `required` (it carries a default_factory), so
# omitting it needs no schema change — letting a resource-blind client fetch a schema, or
# just revalidate `fingerprint`, without re-paying for the inventory.
CapabilitiesDetail = Literal["summary", "full", "contracts"]
# Which state the MCP-roots probe saw (F8, #contract-checklist §1/§2). Defined once here
# and imported into server.py so the two modules cannot drift on the value set.
#
# "unsupported" is the ONLY value this server emits as of the FastMCP 4 upgrade (ADR 0005):
# the MCP SDK v2 removed the server-to-client `roots/list` request on every protocol era,
# so there is no probe left to run. Pass `workspace_root` — the durable, first-choice path
# on every tool that resolves a working directory, and the migration target the 2026-07-28
# spec itself names for the deprecated roots feature (SEP-2577).
#
# The other three values are RETAINED, not emitted. Removing them would narrow a value set
# a client may already branch on (breaking, per AGENTS.md → Versioning); keeping them costs
# nothing and lets a client that stored an older envelope still validate it:
# "client" — the client advertised roots and list_roots() returned (possibly empty);
# "not_negotiated" — this client never advertised the roots capability;
# "probe_failed" — roots were advertised but the call errored that turn.
RootsSource = Literal["client", "not_negotiated", "probe_failed", "unsupported"]
# Lifecycle states for a background job. Terminal: done|failed|cancelled|timeout.
# (TTL-expired records are deleted and reported as job_not_found, not a state.)
JobState = Literal["running", "done", "failed", "cancelled", "timeout"]
# Per-tool maturity, advertised as discovery metadata in kimi_capabilities. NOT the
# consult/propose/apply intent `Tier`. None means the tool inherits the server-wide
# `stability` ("alpha"); a value flags a tool that differs from that norm. It reaches the
# wire as an explicit null, never as an absent key — see _normalize_tool_details (#399).
ToolStability = Literal["stable", "preview", "experimental"]

# G1 (audit-2 gate follow-up): this inheritance rule previously lived only in
# kimi_capabilities' `returns` prose, which detail="full" now gates behind an extra
# param — so a summary-only or resource-blind client couldn't reach it. Putting it on the
# field itself fixes that at the source. Its only wire route is CAPABILITIES_RESULT_SCHEMA
# (kimi://capabilities-result / include_schemas=["capabilities-result"]) — a raw
# TypeAdapter(...).json_schema() that is never passed through _strip_schema_noise, so it
# reaches the wire regardless of _KEPT_DESCRIPTIONS. It is registered there anyway; see
# that registration for why.
_TOOL_STABILITY_DESC = (
    "This tool's per-tool maturity override, advisory only. null means it inherits the "
    "top-level `stability` (the server-wide tier); a value here flags a tool more "
    "experimental than that norm."
)


def workspace_warning_for(source: str | None, cwd: str) -> str | None:
    """Warning when the workspace was resolved from the server's own cwd.

    The MCP server process launches from its install directory, so a cwd-resolved
    workspace silently targets the wrong repo. Surfacing this (rather than failing)
    lets agents notice and pass workspace_root without breaking existing callers."""
    if source == "cwd":
        return (
            f"workspace resolved from the server's own cwd ({cwd}); pass "
            "workspace_root (or configure an MCP root) to be sure the task "
            "targets the intended repository"
        )
    return None


def apply_detail(envelope: dict, detail: str) -> dict:
    """Trim a SUCCESS envelope to the requested detail level (#56).

    `detail="full"` returns the envelope unchanged — the full raw model output and
    complete metadata, for diagnostics. `detail="summary"` (the default) omits the
    often-large, duplicative raw model text (`raw_response.text`); the structured
    fields (`summary`/`findings`/`verdict`/`diff`/…) stay authoritative and the
    parser shape is unchanged (`raw_response` remains present with its `text` nulled,
    and `session_id`/`model` are still echoed there and in `meta`). Error envelopes
    (`ok` != True) are returned unchanged. Mutates and returns the same dict."""
    if detail == "full" or envelope.get("ok") is not True:
        return envelope
    raw = envelope.get("raw_response")
    if isinstance(raw, dict):
        raw["text"] = None
    return envelope


# The result envelopes `slim_meta` applies to, keyed by their `tool` discriminator.
# Scoping STRUCTURALLY (not by prose) keeps every other ok:true payload out: JobStarted
# is `ok: Literal[True]` and carries a full null-laden Meta, and JobStatus/JobSummary
# carry `result_ok`, a required nullable documented "always present, never omitted".
_SLIMMED_TOOLS = frozenset({"kimi_consult", "kimi_review_changes", "kimi_delegate"})


def slim_meta(envelope: dict) -> dict:
    """Drop `meta`'s null-valued keys from a DELIVERED success envelope (#334).

    On the wire, ~40% of a success envelope was null `meta` fields. Absence here means
    exactly what null meant — "not applicable / not reported" — so a client reads the
    same fact from a missing key. Only `meta` sheds keys: every top-level field
    (notably `diff`, whose null a no-changes delegate emits and whose key the
    `next_steps` prose tells the caller to read) and all of `raw_response` are
    delivered verbatim, so no payload-bearing key can vanish and `apply_detail`'s
    guarantee holds unchanged.

    Deliberately keyed on `is None`, never falsiness: `truncated=False` and
    `elapsed_ms=0` are meaningful values. Empty collections stay too — an empty
    `Coverage.omission_reasons` is the machine-checkable half of that model's
    `status == "partial" iff omission_reasons` invariant, and `findings: []` must stay
    iterable.

    WIRE-ONLY. `dump_success` still persists the full envelope, so RESULT_FORMAT does
    not move and stored results stay readable; this runs at the single delivery
    chokepoint, which both the sync and replay paths share. Call it AFTER
    `apply_detail` — before it, `apply_detail`'s `raw["text"] = None` would run last
    anyway, but `meta` would be re-read from an already-slimmed dict for no reason and
    the ordering would stop being obvious. Mutates and returns the same dict."""
    if envelope.get("ok") is not True or envelope.get("tool") not in _SLIMMED_TOOLS:
        return envelope
    meta = envelope.get("meta")
    if isinstance(meta, dict):
        for key in [k for k, v in meta.items() if v is None]:
            del meta[key]
    return envelope


ErrorCode = Literal[
    # Setup / auth
    "kimi_not_found",
    "kimi_auth_required",
    # The auth probe returned no verdict — `kimi.login_status()` yields None when the
    # `kimi login status` child never reported (it timed out, or the binary was gone) —
    # so the session state is UNKNOWN rather than known-absent. Distinct from
    # kimi_auth_required so an unanswered probe never tells an authenticated caller to
    # log in, and so the condition is marked temporary (#252).
    "kimi_auth_indeterminate",
    "unexpanded_env_placeholder",
    # Configuration
    "unsupported_tier",
    "unsupported_sandbox",
    "unsupported_isolation",
    "unsupported_detail",
    "invalid_scope",
    "invalid_base",
    "invalid_commit",
    "invalid_paths",
    # Tool-argument validation failed, from either of two paths. (1) At the MCP call-tool
    # boundary — unknown/extra arg, missing required arg, wrong type, or out-of-enum
    # Literal value — re-emitted from the Pydantic ValidationError that FastMCP raises
    # BEFORE the handler runs, so the failure carries the structured envelope instead of
    # raw validator prose (#136). (2) In-handler, where an argument is well-typed but
    # semantically unusable and no schema rule can express it: a transcript_path that
    # fails validation, or a blank `question`/`task` that would spend on an empty ask
    # (#411). Both paths emit the same envelope shape.
    "invalid_arguments",
    "invalid_workspace_root",
    "workspace_outside_roots",
    "input_too_large",
    # Git / worktree
    "not_a_git_repo",
    "git_unavailable",
    "worktree_error",
    "context_too_large",
    # Resource read (JSON-RPC error carrier, not a tool result). Emitted only on a
    # resources/read of an unknown/disabled URI — the §6 envelope now travels in the
    # JSON-RPC error.data instead of leaving it null (audit F9, #181). Never reachable
    # from a tool call.
    "resource_not_found",
    # Runtime
    "timeout",
    "nonzero_exit",
    "invalid_json",
    "schema_violation",
    "internal_error",
    # The installed `kimi` rejected a flag/value this plugin sends — its CLI
    # contract drifted and the plugin likely needs an update.
    "cli_contract_changed",
    # The reasoning_effort this run requested through the plugin's first-class
    # controls (the per-call parameter or MOONBRIDGE_REASONING_EFFORT) was
    # rejected — a caller/operator value to correct, NOT a plugin contract drift.
    # Two emitters: the plugin's pre-spend shape bounds (an argv-hostile value —
    # oversized or control characters — refused before any subprocess, zero spend),
    # and the Kimi backend's request-level rejection (only when an effort override
    # was actually sent and the failure carries the backend's markers; a rejection of
    # the config key itself stays cli_contract_changed) (#309).
    "invalid_reasoning_effort",
    "invalid_model",
    "empty_response",
    # kimi rejected an operator-supplied MOONBRIDGE_EXTRA_ARGS passthrough entry
    # (an unaccepted option / config key / profile). Operator config to fix, NOT a
    # plugin contract drift — kept distinct from cli_contract_changed so the fail-loud
    # contract signal is not muddied by user-owned passthrough (#231).
    "extra_args_rejected",
    # NOTE: the three `transfer_*` codes were removed here. They were inherited from the
    # Codex plugin, which has a session-transfer tool; kimi has no equivalent, so no
    # handler could ever emit them while their repair hints told agents to "retry
    # kimi_transfer" — a tool this server does not expose. Restore them WITH a
    # kimi_transfer tool, never before.
    # kimi hit a usage/rate limit (ChatGPT window or API-key 429). Transient and
    # retryable; the error carries retry_after_ms as the suggested backoff.
    "kimi_rate_limited",
    # Background-job lifecycle errors:
    "job_not_found",
    "job_running",
    "job_cancelled",
    "job_timeout",
    "job_failed",
    # a done job's stored result was written under a different persisted result format
    # (see RESULT_FORMAT) — another release produced it and this one cannot read it.
    # Permanent for this record (never retryable); start a new job. Same-format
    # malformed payloads remain internal_error (#305).
    "job_result_incompatible",
    # Idempotency (client-supplied idempotency_key on spend-committing tools):
    # the same key was reused with different effective arguments (a duplicate would be
    # a mismatched result, so it is refused rather than silently returning the other run).
    "idempotency_conflict",
    # a prior run for this key+arguments completed but its result is no longer available
    # (consumed via kimi_job_consume_result, or count-cap evicted) within the dedup
    # window; refused rather than silently starting a new paid run.
    "idempotency_result_unavailable",
    # a concurrent reservation for this key is still being published; transient — retry.
    "idempotency_in_progress",
]


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None


def _finite_float(v: object) -> float | None:
    """`float(v)` for a real (non-bool) finite int/float, else None. Catches the
    OverflowError a huge int (e.g. a 400-digit `usedPercent`) raises on `float()`, so an
    untrusted wire value degrades to None instead of escaping as an exception (#321 review)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        fv = float(v)
    except (OverflowError, ValueError):  # pragma: no cover - float(int) only overflows
        return None
    return fv if math.isfinite(fv) else None


class RateLimitWindowSnapshot(BaseModel):
    """RESERVED raw per-window quota (one of the primary/secondary windows); nothing on
    this server produces one — see `RateLimit`. Parsed tolerantly; unknown fields ignored.

    Field validators (mode="before") enforce numeric bounds on all three fields in one place:
    - used_percent: a finite float in [0, 100]; out-of-range/non-finite/huge → None (treated
      as absent — never clamped to a valid-looking value).
    - resets_at / window_minutes: a finite numeric; non-finite/huge → None.
    """

    model_config = ConfigDict(extra="ignore")
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None  # epoch seconds

    @field_validator("used_percent", mode="before")
    @classmethod
    def _validate_used_percent(cls, v: object) -> float | None:
        fv = _finite_float(v)
        if fv is None or fv < 0.0 or fv > 100.0:
            return None
        return fv

    @field_validator("resets_at", mode="before")
    @classmethod
    def _validate_resets_at(cls, v: object) -> int | None:
        fv = _finite_float(v)
        return int(fv) if fv is not None else None

    @field_validator("window_minutes", mode="before")
    @classmethod
    def _validate_window_minutes(cls, v: object) -> int | None:
        fv = _finite_float(v)
        return int(fv) if fv is not None else None


class RateLimitSnapshot(BaseModel):
    """RESERVED raw quota block; nothing on this server produces one — see `RateLimit`.

    Its shape follows the Codex plugin's `account/rateLimits/read`, which kimi has no
    equivalent of. Windows are classified by duration: `primary` is the shorter/rolling
    window, `secondary` the longer one. Either may be None."""

    model_config = ConfigDict(extra="ignore")
    plan_type: str | None = None
    rate_limit_reached_type: str | None = None
    # Backend-enforced spend control (kimi 0.145+, #359). Tri-state: True/False are the
    # backend's answer; None means it did not report one (a CLI that omits the key, or an
    # explicit null — upstream documents both as "unavailable"). Sanitized at the wire boundary
    # by the parser, so a non-bool never reaches this field. (Reserved; see RateLimit.)
    spend_control_reached: bool | None = None
    primary: RateLimitWindowSnapshot | None = None  # shorter/rolling window (historically 5h)
    secondary: RateLimitWindowSnapshot | None = None  # longer window (historically weekly)


RateLimitStatus = Literal["available", "limited", "exhausted", "unknown", "unavailable", "blocked"]


class RateLimitWindow(BaseModel):
    """One quota window, interpreted for an agent. used_percent/remaining_percent are
    current-ish (as of `as_of`) for an open window and are NULLED when reset_passed is
    true (the window rolled over since capture, so its captured usage is obsolete and
    its post-reset usage is unobserved). One source of truth: a present percentage
    always means current-ish, never stale."""

    model_config = ConfigDict(extra="forbid")
    used_percent: float | None = None
    remaining_percent: float | None = None  # max(0, 100 - used_percent); None if reset_passed
    window_minutes: int | None = None
    # RFC3339 UTC (F6, schema-19); null when the captured epoch is absent or not
    # datetime-representable — conversion never raises (tolerant parsing).
    resets_at: str | None = None
    seconds_until_reset: int | None = None  # clamped ≥ 0; 0 when reset_passed; None if no resets_at
    reset_passed: bool = False


# Provenance, deliberately NOT in the docstring below: that text ships to clients as a
# schema description, and where this shape came from informs a maintainer, not a caller.
# It came from the Codex plugin this server was ported from, whose `kimi app-server` had
# `account/rateLimits/read`. kimi has no equivalent, which is why nothing populates it.
class RateLimit(BaseModel):
    """Agent-facing rate-limit quota. On this server it is always `unavailable`.

    RULES. Do not plan spend around this block. `status: unavailable` is the only value you
    will observe; treat every other state, and every quota-detail field below, as a reserved
    shape rather than behavior to branch on.

    Why: kimi exposes no quota-read channel and its provider is user-configured, so there is
    nothing authoritative to report. `kimi_status` returns `unavailable` and `Meta.rate_limit`
    stays null; that is not a failure. The only quota signal this server can give is the
    `kimi_rate_limited` error on a throttled call, which carries `retry_after_ms`.

    Reserved shape: the fields below are kept so a future kimi that exposes quota can populate
    them without a breaking schema change. `primary` is the shorter/rolling window, `secondary`
    the longer one; `available` would attest only that reported windows are healthy, and
    `blocked` would report a backend SPEND control (`spend_control_reached`) that no quota
    reset clears."""

    model_config = ConfigDict(extra="forbid")
    status: RateLimitStatus
    # 'app_server_live' = a fresh kimi_status read; 'plugin_cache' = the persisted snapshot;
    # 'current_run' = observed during a paid run (legacy; not populated on current CLIs).
    source: Literal["current_run", "plugin_cache", "app_server_live"] = "plugin_cache"
    as_of: str | None = None  # ISO-8601 capture time; None when no snapshot
    age_seconds: int | None = None
    is_stale: bool = False  # older than the configured warn threshold (advisory)
    plan_type: str | None = None  # captured metadata, NOT a verified current plan
    home_unverified: bool = False  # cached KIMI_CODE_HOME differs from the current environment
    # Backend-reported SPEND control (#359). true -> status 'blocked' (waiting will not clear
    # it); false -> the backend says spending is permitted; null -> it did not report the state
    # (a CLI predating kimi 0.145, or an explicit upstream null). null is NOT "false".
    spend_control_reached: bool | None = None
    limiting_window: Literal["primary", "secondary"] | None = None
    primary: RateLimitWindow | None = None  # shorter/rolling window (historically 5-hour)
    secondary: RateLimitWindow | None = None  # longer window (historically weekly)
    note: str | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Severity
    title: str
    file: str | None = None
    line: int | None = None
    line_end: int | None = None  # end line when the finding spans a range (line = start)
    evidence: str
    risk: str
    recommendation: str


class RawResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    session_id: str | None = None
    model: str | None = None


class ContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0


class Workspace(BaseModel):
    """Compact workspace-resolution context for job-lifecycle success responses.

    Mirrors the `cwd`/`workspace_source`/`workspace_warning` fields the full `Meta`
    envelope carries, so a successful `kimi_job_status`/`kimi_job_list` call shows
    which repository the lookup targeted (and warns when it fell back to the server's
    own cwd) — making wrong-workspace polling diagnosable rather than a silent empty
    list or `job_not_found` (#54)."""

    model_config = ConfigDict(extra="forbid")
    cwd: str
    workspace_source: str | None = None  # how cwd was resolved: param|roots|cwd
    workspace_warning: str | None = None  # set when cwd was resolved from server cwd


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str
    workspace_source: str | None = None  # how cwd was resolved: param|roots|cwd
    workspace_warning: str | None = None  # set when cwd was resolved from server cwd
    # F8. The `which run` clause is published (not just commented) because #393 made the
    # field reach two kinds of envelope that answer it differently — a stored result
    # replays the producer's value while a handle reports the attaching call — and a
    # client diffing those two would otherwise read a contradiction. Mirrors how `tier`
    # below states its own originating-run rule.
    roots_source: RootsSource | None = Field(
        default=None,
        description=(
            "What the MCP-roots probe saw. ALWAYS 'unsupported' on this server: the MCP SDK "
            "removed the server-to-client roots/list request on every protocol era, so no "
            "probe runs and client roots can no longer supply a working directory — pass "
            "workspace_root, or the call falls back to the server cwd. The other three values "
            "are HISTORICAL: retained so an envelope stored by an earlier release still "
            "validates, never emitted now — 'client' (the client advertised the roots "
            "capability and the probe returned, possibly an empty list), 'not_negotiated' "
            "(this client never advertised the capability), 'probe_failed' (roots were "
            "advertised but the call errored that turn). It reports the PROBE, "
            "not where the workspace came from — workspace_source answers that, and is now "
            "'param' (you passed workspace_root) or 'cwd' (you did not); its 'roots' value is "
            "likewise unreachable. WHEN PRESENT, which run it "
            "describes depends on the envelope: a DELIVERED kimi_consult / "
            "kimi_review_changes / kimi_delegate result — whether returned synchronously "
            "or fetched later via kimi_job_result / kimi_job_consume_result — reports the "
            "ORIGINATING run, like tier; a *_async job handle (including an idempotency "
            "replay handle), a dry-run preview, and an error a kimi_job_* call GENERATES "
            "instead of delivering a stored result (job_not_found, a still-running or "
            "unreadable job) each report the CURRENT call. So a "
            "replay handle and the result later fetched for that same job may legitimately "
            "differ. Absence does NOT imply any of those: it means only that the value was "
            "not reported — the roots state was never recorded for that run, or the failure "
            "happened where it could not be (an unexpected server-side internal_error is "
            "built without one). Never infer a run's age or identity from a missing key."
        ),
    )
    tier: Tier = Field(
        description=(
            "Kimi intent tier of the run this envelope describes — consult (read-only, no "
            "writes), propose (writes only inside a throwaway worktree), or apply. For a call "
            "that runs Kimi it is that call's own tier; for a retrieved background-job result "
            "(kimi_job_result/consume) it is the ORIGINATING run's tier (a completed delegate "
            "reads 'propose'); for a kimi_delegate_dry_run preview it is the previewed run's "
            "tier. Job-lifecycle calls (kimi_job_*) run no Kimi, so their GENERATED error "
            "envelopes report 'consult'. This is orthogonal to the MCP readOnlyHint annotation, "
            "which describes whether the call mutates this server's own job state (so "
            "kimi_job_cancel/consume are readOnlyHint:false yet tier 'consult'). On a lifecycle "
            "error envelope, meta.job_kind carries the inspected job's own kind/posture."
        )
    )
    sandbox: Sandbox = Field(
        description=(
            "Sandbox the run this envelope describes uses: read-only, workspace-write (worktree, "
            "no network egress), or danger-full-access. It tracks `tier` across the same cases — "
            "the call's own run, a retrieved job's originating run, or a dry-run's previewed run; "
            "lifecycle-generated errors report 'read-only'. Like tier, this describes Kimi "
            "execution posture, not whether the call mutates this server's job state (see "
            "readOnlyHint)."
        )
    )
    isolation: Isolation
    model: str | None = Field(
        default=None,
        description=(
            "The model slug requested through the plugin's first-class controls for the Kimi "
            "run this envelope describes — the per-call `model` parameter or the server's "
            "MOONBRIDGE_MODEL default. On success envelopes it is also mirrored to "
            "raw_response.model. It is override provenance, not backend attestation: ABSENT "
            "OR null means model selection happened outside those controls (Kimi's own "
            "default, the operator's config.toml, or an opaque --profile), and the plugin "
            "cannot know which model that resolved to. The generic `-c model=…` extra-args "
            "spelling is refused at parse time so a passthrough cannot contradict this field "
            "(#310). A retrieved background-job result carries the ORIGINATING run's value. An "
            "error envelope GENERATED before a Kimi run was prepared — argument validation, "
            "job-lifecycle (kimi_job_*) calls — does not attest to the rejected call's model: "
            "it reports the server-default resolution, or absent/null when no default is set "
            "(see meta.job_kind for an inspected background job's kind)."
        ),
    )
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "The reasoning effort requested through the plugin's first-class controls for "
            "the Kimi run this envelope describes — the per-call `reasoning_effort` "
            "parameter or the server's MOONBRIDGE_REASONING_EFFORT default, sent to "
            "kimi as a `model_reasoning_effort` config override. Like meta.model it is "
            "override provenance, not backend attestation: ABSENT OR null means effort "
            "resolution happened outside those controls (Kimi's own default, the operator's "
            "config.toml, or an opaque --profile), and the plugin cannot know what it "
            "resolved to. The generic `-c model_reasoning_effort=…` extra-args spelling is "
            "refused at parse time so a passthrough cannot contradict this field (#309). A "
            "retrieved background-job result carries the ORIGINATING run's value; an error "
            "envelope GENERATED before a Kimi run was prepared reports the server-default "
            "resolution, or absent/null when no default is set — the same cases meta.model "
            "documents."
        ),
    )
    scope: str | None = None  # review scope: working_tree|branch|commit
    base: str | None = None
    commit: str | None = None
    paths: list[str] | None = None
    timeout_seconds: int = Field(
        description=(
            "Deadline in seconds for the run this envelope describes; which deadline "
            "depends on the envelope. A synchronous call that runs Kimi (kimi_consult, "
            "kimi_review_changes, kimi_delegate) reports that call's own resolved "
            "deadline, post-clamp (10-600s; a value outside that range is coerced to the "
            "nearest bound, not rejected). A background job — a *_async call's job-start "
            "handle, or a later kimi_job_result/kimi_job_consume_result fetch of that "
            "job's ORIGINATING run — reports the job's own deadline instead: the "
            "job-lifecycle ceiling (config.job_max_seconds(), default 1800, clamped "
            "60-7200), since a background job runs to that ceiling, not the sync clamp. "
            "A kimi_job_* call's own handler-generated lifecycle error (job_not_found "
            "and its siblings) reports that same ceiling, present even when no job was "
            "resolved (job_not_found, where meta.job_kind is omitted from the envelope). "
            "Call-boundary argument validation (invalid_arguments) and an unexpected "
            "internal_error — on any tool, not only kimi_job_* — instead report the "
            "server's configured sync deadline, still post-clamp; NEITHER is tied to any "
            "run or resolved job."
        )
    )
    elapsed_ms: int
    command_exit_code: int | None = None
    session_id: str | None = None  # Kimi session id, when one was emitted
    truncated: bool = False
    truncation_hint: str | None = None
    # Optional `kimi` flags this server dropped because the installed CLI did not
    # advertise them in --help (e.g. ["--model"]). Empty in the common case;
    # informational — guarantee-bearing flags are never dropped, only depth ones.
    compat_warnings: list[str] = Field(default_factory=list)
    # Advisory security posture warnings detected before launching Kimi.
    security_warnings: list[str] = Field(default_factory=list)
    redacted_paths: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    # A run's rate-limit snapshot. `null` on kimi 0.144+: quota no longer rides the exec
    # stream (#321), so it is read live by kimi_status (account/rateLimits/read), not per run.
    rate_limit: RateLimit | None = None
    context_summary: ContextSummary | None = None
    job_id: str | None = None  # set on background-job results; None for sync calls
    # Kind of the background job a lifecycle error envelope refers to (e.g.
    # "kimi_delegate"), set only when kimi_job_* resolved an existing record. It
    # carries the inspected job's own posture — a propose-tier delegate vs a read-only
    # consult/review — without overloading tier/sandbox, which describe the Kimi run the
    # envelope is about (for a lifecycle error, the read-only lifecycle call itself). None
    # for not-found/pre-lookup errors and for every non-lifecycle call.
    job_kind: str | None = None
    # Set to True ONLY on a response that replayed an existing run because the caller
    # passed an idempotency_key matching an in-flight/completed run — a signal that no
    # new Kimi spend occurred. Null (like the other optional meta fields) otherwise;
    # it is patched onto the outgoing envelope for a replay and is never persisted into
    # a job's result.json, so a later ordinary read of that job never looks replayed.
    idempotency_replayed: Literal[True] | None = None
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


# Whether the model was actually consulted. `not_run` means the tool short-circuited
# before any Kimi call (e.g. nothing reviewable was gathered), so `verdict` reflects no
# model judgment. Separated from `verdict` so a caller can tell "reviewed and clean" from
# "never reviewed" (#319).
ReviewStatus = Literal["completed", "not_run"]

# Whether everything in the requested git/path scope was put in front of the model AND the
# gather could treat it as one consistent snapshot. `complete`: no omission and no concurrent
# modification was detected. `partial`: some in-scope content was not reviewed, OR (working_tree
# only) the working tree was modified while the diff was being gathered, so the pieces may not
# reflect a single snapshot — see `omission_reasons`. The concurrent-modification signal is
# BEST-EFFORT (see the `tree_changed_during_gather` reason), so `complete` is not an absolute
# guarantee that no concurrent edit occurred. The gather issues its git commands as separate
# processes, but every ref they read is resolved to an immutable object ID once up front (#355):
# `branch`/`commit` diff pinned objects, so a concurrent commit/reset/checkout cannot make their
# summary and diff disagree, and they run no snapshot check. working_tree pins HEAD too and
# additionally brackets the gather with a token: it catches a HEAD move and a change to a path's
# worktree-vs-HEAD status, but not a diff-content change that leaves the status code unchanged
# (the same limitation class as a content-only re-edit or an A→B→A round trip) (#336).
CoverageStatus = Literal["complete", "partial"]

# Why in-scope content went unreviewed. A closed set:
#   untracked_omitted          — untracked files exist in scope but were not gathered (see the
#                                `untracked` input policy); their contents were never sent.
#   tree_changed_during_gather — (working_tree only) the working tree was modified while the
#                                diff was being gathered — its changed-file set or a file's
#                                git status changed between the start and end of the gather —
#                                so the summary/diff/untracked pieces may not describe one
#                                consistent snapshot. A consistency caveat, not a claim that
#                                specific content was omitted. Best-effort: it trips on file
#                                additions/removals and status changes (including a concurrent
#                                `git add`), but NOT on a content-only re-edit of an
#                                already-modified file — so its ABSENCE is not proof the tree
#                                held still (#336).
#   truncated                  — the gathered diff hit the byte cap and was cut off.
#   redacted                   — secret redaction hid content from the model. EITHER a
#                                secret-looking file's whole hunk was dropped (the model saw
#                                a marker instead of its content) OR a secret-looking value
#                                inside an otherwise-sent file was masked inline.
#                                `coverage.redaction` (when present) distinguishes the two;
#                                `meta.redacted_paths` names the files either way regardless
#                                (#421, #433).
CoverageOmissionReason = Literal[
    "untracked_omitted", "tree_changed_during_gather", "truncated", "redacted"
]


class RedactionSummary(BaseModel):
    """Structured breakdown of the `redacted` omission reason (#433): which files had
    their hunk dropped whole vs which were sent with inline values masked, scoped to
    content that actually reached the model. Both path lists are encounter order and
    mutually exclusive (withholding is dominant: a later mask on an already-withheld
    path counts nowhere — see `DiffRedactor`). `withheld_paths` union `masked_paths`
    can be a STRICT SUBSET of `meta.redacted_paths`: that flat union describes the
    whole gathered diff with no notion of what a byte cap later dropped, and stays the
    single backward-compatible list it always was (#433 review C1/C2)."""

    model_config = ConfigDict(extra="forbid")
    withheld_paths: list[str] = Field(
        default_factory=list,
        description="Files whose hunk was dropped whole (path looked secret-bearing, "
        "e.g. `.env`). Encounter order.",
    )
    masked_paths: list[str] = Field(
        default_factory=list,
        description="Files sent with >=1 inline secret-looking value replaced by a "
        "marker. Encounter order.",
    )
    inline_masks: int = Field(
        default=0,
        ge=0,
        description="Emitted replacement markers, not raw pattern matches — "
        "overlapping candidates merge into one marker first. >= len(masked_paths); "
        "zero iff masked_paths is empty.",
    )

    @model_validator(mode="after")
    def _check_invariants(self) -> RedactionSummary:
        # The docstring promises withheld_paths/masked_paths are mutually exclusive
        # (withholding dominates — see `DiffRedactor`), but this model is wire-contract
        # constructible from arbitrary external JSON, not only from DiffRedactor's own
        # construction path, so the promise must be enforced here too (#433 Copilot
        # review of #470, comment 2).
        overlap = set(self.withheld_paths) & set(self.masked_paths)
        if overlap:
            raise ValueError(f"withheld_paths and masked_paths must be disjoint: {sorted(overlap)}")
        # Every real inline_masks increment (DiffRedactor.commit_pending's "mask"
        # branch) also adds its path to masked_paths (deduped) — so the two can never
        # disagree: a non-empty masked_paths needs at least one mask per listed path,
        # and an empty masked_paths can never carry a nonzero count (#433 review C4).
        if self.masked_paths:
            if self.inline_masks < len(self.masked_paths):
                raise ValueError(
                    "inline_masks must be >= len(masked_paths) when masked_paths is non-empty"
                )
        elif self.inline_masks:
            raise ValueError("inline_masks must be 0 when masked_paths is empty")
        return self


class Coverage(BaseModel):
    """What the review actually covered — the disclosure that makes `verdict`
    interpretable (#319). Untracked counts are pathspec-scoped and null outside
    working_tree scope; `detected == included + omitted` where applicable.
    `redaction`, when non-null, breaks down the `redacted` omission reason — but the
    reason can fire with `redaction` still null (a legacy-shaped producer that only
    sets the flat path union, or a disclosure dropped entirely by byte-cap
    truncation; every Coverage persisted before this field existed is one such case).
    The enforced direction is the other one: `redaction` is never populated without
    the `redacted` reason also present."""

    model_config = ConfigDict(extra="forbid")
    status: CoverageStatus
    untracked_files_detected: int | None = None
    untracked_files_included: int | None = None
    untracked_files_omitted: int | None = None
    # `= None` is load-bearing (#433): without an explicit default pydantic v2 makes
    # this field REQUIRED, and every Coverage persisted before this field existed —
    # replayed from a stored result.json — would then fail model_validate.
    redaction: RedactionSummary | None = None
    omission_reasons: list[CoverageOmissionReason] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_invariants(self) -> Coverage:
        # Enforce the invariants the docstring advertises, so no construction path (now or
        # future) can emit an internally inconsistent disclosure — e.g. a false `complete`
        # with clamped counts (#322 F3/F5).
        if (self.status == "partial") != bool(self.omission_reasons):
            raise ValueError("coverage.status must be 'partial' iff omission_reasons is non-empty")
        detected = self.untracked_files_detected
        included = self.untracked_files_included
        omitted = self.untracked_files_omitted
        if (
            detected is not None
            and included is not None
            and omitted is not None
            and detected != included + omitted
        ):
            raise ValueError("untracked_files_detected must equal included + omitted")
        # #433 review F1: `redaction` and the `redacted` omission reason are two views
        # of the SAME fact (build_coverage derives both from one predicate — see its own
        # comment), and the model must reject the case a producer disagreeing about
        # them would otherwise be able to construct: a populated disclosure for a
        # reason that never fired.
        if self.redaction is not None and "redacted" not in self.omission_reasons:
            raise ValueError("coverage.redaction requires 'redacted' in omission_reasons")
        return self


class _SuccessBase(BaseModel):
    """Fields shared by every success envelope. `verdict`/`confidence` live only on
    the review result — they are a review judgment, meaningless for Q&A (consult) or
    a worktree diff (delegate), so those tools must not carry them (#31)."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    raw_response: RawResponse = Field(default_factory=RawResponse)
    meta: Meta


class ConsultResult(_SuccessBase):
    """kimi_consult: a read-only answer/second opinion. No verdict/confidence/diff."""

    tool: Literal["kimi_consult"] = "kimi_consult"


class ReviewResult(_SuccessBase):
    """kimi_review_changes: a structured review. The only verdict-bearing result.

    `verdict` is the *safe overall conclusion*, not the raw model verdict: when coverage
    is `partial`, a model `pass` is surfaced as `unknown` (a pass over partly-reviewed
    code is not a pass), while concrete `fail`/`concerns` are retained. `review_status`
    and `coverage` are top-level — not in `meta` — because they are load-bearing for
    interpreting `verdict` (#319)."""

    tool: Literal["kimi_review_changes"] = "kimi_review_changes"
    verdict: Verdict = "unknown"
    confidence: Confidence = "medium"
    # Required (no default): review_status and coverage are load-bearing for interpreting
    # verdict, so every construction must set them — a defaulted "completed"/"True" could
    # let a future path silently report the safe-looking positive (#322 F5).
    review_status: ReviewStatus
    coverage: Coverage


class DelegateResult(_SuccessBase):
    """kimi_delegate(_async): a proposed change. Carries the unified `diff` (confined
    to a temp worktree and NOT applied to the live tree); no verdict/confidence."""

    tool: Literal["kimi_delegate"] = "kimi_delegate"
    diff: str | None = None


def dump_success(result: _SuccessBase) -> dict:
    """THE serializer for success envelopes bound for result.json (and the sync wire).
    Success envelopes retain null optionals as explicit keys, unlike error envelopes
    (serialize_error, exclude_none) — that asymmetry is part of the persisted result
    format, pinned by tests/fixtures/result_format_snapshot.json. Changing the mode
    here is a persisted-format change: the snapshot guard fires, and RESULT_FORMAT
    must bump (#305)."""
    return result.model_dump(mode="json")


class InvalidArgument(BaseModel):
    """One field-level argument-validation failure (#136). Machine-actionable detail
    behind the `invalid_arguments` error code — one entry per Pydantic error at the
    call-tool boundary, or per rejected argument for an in-handler validation the
    schema cannot express (e.g. a blank `question`/`task`, #411)."""

    model_config = ConfigDict(extra="forbid")
    field: str  # the offending argument name (accessor path for nested locations)
    reason: str  # the validator's human-readable message (bounded)
    allowed_values: list[str] | None = None  # enum options when the field is a Literal
    # The rejected value is deliberately NOT echoed: a Literal/string param accepts
    # arbitrary input, so a value could be a secret, and best-effort redaction cannot
    # reliably catch a plain one. The caller already holds what it sent; field + reason +
    # allowed_values drive the repair without copying input into the result (#136).


RepairStep = Literal[
    "retry_after_delay",
    "correct_arguments",
    "use_allowed_value",
    "reduce_input",
    "use_workspace_in_roots",
    "poll_job_status",
    "list_jobs",
    "list_resources",
    "start_new_job",
    "authenticate",
    "install_kimi",
    "install_git",
    "init_git_repo",
    "update_plugin",
    "correct_config",
    "inspect_and_retry",
    "retry_then_report",
    "use_new_idempotency_key",
]


class Repair(BaseModel):
    """Machine-actionable recovery guidance (agent-friendly-mcp §6). `next_step` is a
    STABLE SYMBOLIC label an agent branches on (not prose); `alternative` carries the
    human-readable fallback. `tool`/`arguments` name a tool to call to recover."""

    model_config = ConfigDict(extra="forbid")
    next_step: RepairStep
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    alternative: str | None = None


class ErrorDetail(BaseModel):
    """§6 details{field, value, reason}. `value` is deliberately omitted: a Literal/string
    param accepts arbitrary input that could be a secret, and best-effort redaction cannot
    reliably catch a plain one; the caller already holds what it sent. Documented divergence.

    `field` and `fields` are mutually exclusive — at most one is set, never both: `field`
    names a single offending input; `fields` names a set of inputs whose *combination* is
    invalid — e.g. a combined-size limit where no single input is at fault on its own
    (#174/F2). Neither is required: a detail may instead carry only `reason`/`allowed_values`
    (e.g. an enum failure with no single named field). When `fields` is set it is non-empty
    and its entries are unique — both advertised in the published schema (`minItems: 1`,
    `uniqueItems: true`), not merely runtime-enforced."""

    model_config = ConfigDict(extra="forbid")
    field: str | None = None
    fields: (
        Annotated[list[str], Field(min_length=1, json_schema_extra={"uniqueItems": True})] | None
    ) = None
    reason: str | None = None
    allowed_values: list[str] | None = None

    @model_validator(mode="after")
    def _one_of_field_or_fields(self) -> ErrorDetail:
        if self.field is not None and self.fields is not None:
            raise ValueError("ErrorDetail: set at most one of field/fields, never both")
        if self.fields is not None and len(set(self.fields)) != len(self.fields):
            raise ValueError("ErrorDetail.fields must not contain duplicates")
        return self


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    # §6: both always present in the canonical schema. `temporary` was `retryable`.
    temporary: bool = Field(...)
    retry_after_ms: int | None = Field(..., ge=0)
    repair: Repair | None = None  # omitted only when no corrective path exists
    details: ErrorDetail | None = None
    # Multi-field validation carrier (#136); details mirrors the first entry.
    invalid_arguments: list[InvalidArgument] | None = None
    # Documented top-level extensions (unchanged):
    limit_bytes: int | None = None
    actual_bytes: int | None = None
    candidate_roots: list[str] | None = None
    # §6 correlation, populated only on the JSON-RPC (resource) carrier: the tool carrier
    # already carries request_id on `meta`, and duplicating it there would be two homes for
    # one fact. exclude_none strips both on the tool path.
    resource_uri: str | None = None
    request_id: str | None = None
    # NOTE: `app_server_stderr_tail` was removed here alongside the `transfer_*` codes. It
    # existed only for a kimi_transfer failure diagnostic; with no such tool and no code
    # path setting it, it was an advertised field that could never be populated.

    @model_validator(mode="after")
    def _retry_after_only_when_temporary(self) -> ErrorInfo:
        if not self.temporary and self.retry_after_ms is not None:
            raise ValueError("retry_after_ms must be None when temporary is False")
        return self


class ErrorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    error: ErrorInfo
    meta: Meta


class ResolvedDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Tier
    sandbox: Sandbox
    isolation: Isolation
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int
    timeout_bounds: list[int]  # [min, max] clamp range for timeout_seconds


class RawDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str
    sandbox: str
    isolation: str
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    kimi_found: bool
    kimi_version: str | None = None
    # Readiness probes (all free — no model call):
    kimi_authenticated: bool | None = None  # None = could not determine
    auth_detail: str | None = None  # non-identifying method phrase (ChatGPT / API key)
    version_supported: bool | None = None
    version_warning: str | None = None  # advisory; never blocks
    flags_warning: str | None = None  # a guarantee-bearing flag missing from --help
    ready: bool = False  # found AND authenticated
    readiness_detail: str
    # Operator passthrough (MOONBRIDGE_EXTRA_ARGS, #231). Presence + option count +
    # validity ONLY — never the raw tokens/values (a `-c` value can hold a secret).
    # extra_args_valid is False when the knob is set but fails parse/allowlist, so every
    # paid call is guaranteed to preflight-fail even though `ready` may be True.
    extra_args_configured: bool = False
    extra_args_count: int = 0
    extra_args_valid: bool = True
    raw_defaults: RawDefaults
    resolved_defaults: ResolvedDefaults
    # Always present, but never a live reading: kimi exposes no quota-read channel (there is
    # no equivalent of Codex's app-server `account/rateLimits/read`), and the provider is
    # user-configured, so the server has nothing authoritative to report. Fixed at
    # 'unavailable' rather than guessed — see the `unavailable` status on RateLimit.
    rate_limit: RateLimit = Field(default_factory=lambda: RateLimit(status="unavailable"))
    caveat: str
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


class AsyncLifecycle(BaseModel):
    """Structured discovery metadata for an *_async tool that runs as a background job
    via this server's *custom* job lifecycle rather than native MCP tasks/progress (#94).

    Lets a client that looks specifically for native MCP tasks or `notifications/progress`
    infer their absence structurally (not just from description prose), and discover the
    exact poll/result/consume/cancel/list tools and the JobStatus fields to branch on.

    Activity signal: the server exposes a polled (disk-persisted, poll-read) event-activity
    signal via `activity_support`/`event_count_field`/`last_event_field`/`event_age_field`.
    This is SEPARATE from `progress_support` — progress_support denotes native MCP
    notifications/progress on THIS async/poll path, which has none (the sync await path
    streams throttled notifications/progress instead), and stays "none"."""

    model_config = ConfigDict(extra="forbid")
    # No native MCP task object and no notifications/progress streaming — the run is
    # polled via the kimi_job_* tools below. These are fixed for this server.
    native_task_support: Literal[False] = False
    progress_support: Literal["none"] = "none"
    lifecycle: Literal["kimi_job_*"] = "kimi_job_*"
    # The job-lifecycle tools to drive the run after the *_async call returns a job_id.
    poll_tool: str  # kimi_job_status
    result_tool: str  # kimi_job_result
    consume_tool: str  # kimi_job_consume_result
    cancel_tool: str  # kimi_job_cancel
    list_tool: str  # kimi_job_list
    # JobStatus fields a client branches on while polling.
    status_field: str  # "status" — the lifecycle state
    result_ready_field: str  # "result_available" — true once the result can be fetched
    # "result_ok" — the done record's producer-declared outcome (true/false/null),
    # so a poll or list can flag a stored failure without fetching the result.
    result_ok_field: str
    poll_after_field: str  # "poll_after_ms" — backoff to honor before the next poll
    # Polled event-activity (#139). SEPARATE from progress_support: this is not
    # native notifications/progress, it is a disk-persisted, poll-read activity
    # signal. progress_support stays "none" so the native-progress meaning is intact.
    activity_support: Literal["kimi_events"] = "kimi_events"
    event_count_field: str  # "events_seen"
    last_event_field: str  # "last_event_at"
    event_age_field: str  # "event_age_ms"


class ToolCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cost: Literal["free", "active"]
    # Per-tool maturity (advisory). None ⇒ inherits the server-wide `stability`; set
    # only when a tool is more experimental than that norm (e.g. the async/job surface).
    # Optional here, but delivered on every inventory entry: `kimi_capabilities` forces
    # the key back after `exclude_none` strips it, so None ships as null (#399).
    stability: ToolStability | None = Field(default=None, description=_TOOL_STABILITY_DESC)
    # Optional in the schema (not just at the Python default): `kimi_capabilities`'
    # `detail="summary"` (the default) strips use_when/returns from every entry before
    # the response ships, so the published schema must accept an entry without them —
    # only `detail="full"` carries them. Always populated wherever this model is
    # constructed; the None branch exists for the wire, not for a real omission here.
    use_when: str | None = None
    required_params: list[str] = Field(default_factory=list)
    key_optional_params: list[str] = Field(default_factory=list)
    returns: str | None = None
    # Error codes this tool may return. Advisory, not exhaustive: a guide for
    # branching/recovery, not a closed contract. Typed as ErrorCode so the schema
    # advertises the valid code set and entries are checked statically.
    error_codes: list[ErrorCode] = Field(default_factory=list)
    # Set only on the *_async tools: how to drive their background-job lifecycle, and
    # that the server uses that custom lifecycle instead of native MCP tasks/progress
    # (#94). None ⇒ a synchronous/lifecycle tool that needs no such metadata.
    async_lifecycle: AsyncLifecycle | None = None


class CapabilitiesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    name: str
    version: str
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()
    # What a change to the fingerprint may signal, as granular machine-readable identifiers
    # (audit F6, #178). A client can key its cache-invalidation reasoning off these categories
    # rather than treating every fingerprint change as opaque. A change to the CONTRACT within
    # one of these categories bumps `fingerprint`; nothing outside them affects it, and the
    # release-identity fields named in _FINGERPRINT_COVERS_DESC are carved out of the covered
    # categories that contain them (#337). Derived from the authoritative FINGERPRINT_COVERS
    # tuple so the disclosure can't drift from the source of truth.
    fingerprint_covers: list[str] = Field(
        default_factory=lambda: list(FINGERPRINT_COVERS),
        description=_FINGERPRINT_COVERS_DESC,
    )
    transport: str
    stability: str
    active_tools: list[str]
    free_tools: list[str]
    tool_details: list[ToolCapability] = Field(default_factory=list)
    tiers: list[str]
    sandboxes: list[str]
    scope: list[str]  # what this server is for
    negative_scope: list[str]  # what it deliberately does NOT do
    prerequisites: list[str]
    deprecation_policy: str
    # Where a TOOL failure travels, stated before the first failure so a client need not
    # infer it from the outputSchema union (#175/F3). Scoped to tool calls deliberately:
    # resource-read failures use the JSON-RPC error carrier, not a tool result.
    tool_error_carrier: str = (
        "tool result with isError: true; the error envelope is in structuredContent, "
        "and content[0].text mirrors it as JSON"
    )
    # The MCP protocol revision this server's wire shapes are written against — a
    # declared TARGET, not the per-session negotiated value (#423 Kimi review): the
    # installed SDK negotiates per connection, so a given session's negotiated revision
    # can be earlier than this field (see ADR 0005 and
    # test_protocol_revision_matches_installed_sdk_target, which pins the target to the
    # installed SDK's own newest known revision). Kept as a plain `str`, not a `Literal`,
    # because a future revision bump changes the value without narrowing the type.
    #
    # Moved 2025-11-25 -> 2026-07-28 with the FastMCP 4 / MCP SDK v2 upgrade, which serves
    # BOTH eras from one server (ADR 0005). The value tracks the newest revision served;
    # `protocol_eras_served` below carries the full set, because "the target" alone can no
    # longer tell a client whether the handshake era is still reachable.
    protocol_revision: str = Field(
        default="2026-07-28",
        description=(
            "The newest MCP protocol revision this server serves (the target its wire "
            "shapes are written against). The installed SDK negotiates per connection "
            "and this server serves multiple eras — see `protocol_eras_served` for the "
            "full set — so a given session's negotiated revision can be earlier than "
            "this field. This field states the target, not the per-session negotiation. "
            "The migration that moved this from 2025-11-25 is recorded in ADR 0005 "
            "(docs/adr/0005-fastmcp-4-and-mcp-sdk-v2.md)."
        ),
    )
    # Additive companion to `protocol_revision` (ADR 0005). Before FastMCP 4 this server
    # spoke one era, so a single target string said everything; now it serves the
    # sessionless modern era AND the initialize handshake, and a client that must know
    # whether the handshake is reachable (because it depends on session-era behavior)
    # could otherwise only find out by attempting a connection.
    protocol_eras_served: list[str] = Field(
        default_factory=lambda: ["2025-11-25", "2026-07-28"],
        description=(
            "Every MCP protocol revision this server negotiates, oldest first: the "
            "handshake era (session via `initialize`) and the sessionless modern era "
            "(per-request metadata). Roots are unavailable on both — pass "
            "`workspace_root` (see `meta.roots_source`)."
        ),
    )
    # The server's documented reading of its own `readOnlyHint` tool annotation (#426):
    # a judgment call, not a restatement. The long-form rationale — and the annotation
    # presets it governs (_FREE_READ/_ACTIVE_PROPOSE/_ACTIVE_ASYNC) — live in server.py
    # (~server.py:181-204, esp. the observable-job-state paragraph on _ACTIVE_ASYNC);
    # this field is the compact, agent-facing statement of that reading, kept free of
    # the mechanics the comments already own. Verified against the live annotations by
    # test_sync_active_tools_are_not_read_only / test_dry_run_tools_are_read_only
    # (alongside the existing test_async_launchers_are_not_read_only).
    annotations_reading: str = Field(
        default=(
            "readOnlyHint tracks whether a call changes observable state that outlives "
            "the response — a job record, committed spend — rather than file I/O. "
            "That's why kimi_consult, kimi_review_changes, and kimi_delegate (and "
            "their _async variants) are readOnlyHint: false even though consult and "
            "review never write files, while kimi_dry_run and kimi_delegate_dry_run, "
            "which create no job record, stay readOnlyHint: true."
        ),
        description=(
            "The server's documented reading of its `readOnlyHint` tool annotation: "
            "what counts as read-only for tools that call Kimi. Stated here because "
            "it's a judgment call a literal reader of the MCP spec could reach "
            "differently, not a restatement of server.py's annotation-preset mechanics."
        ),
    )
    # Where a RESOURCE-read failure travels (audit F9, #181). A resources/read of an
    # unknown/disabled URI returns a JSON-RPC error whose `error.data` now carries the
    # §6 ErrorInfo shape (code/message/temporary/retry_after_ms/repair — the `error`
    # object of kimi://error-envelope), no longer a bare null. `error.code` is the MCP
    # numeric not-found code for the negotiated era (-32002 handshake / -32602 modern, see
    # server._resource_not_found_code) or -32603 (read failure); the symbolic string
    # code lives in `error.data.code` (e.g. resource_not_found). resource_uri/request_id
    # (audit F6, #185) add correlation: the tool carrier already has request_id on
    # `meta`, so those two are populated only here, never on the tool path.
    resource_error_carrier: str = (
        "JSON-RPC error; the §6 ErrorInfo envelope (code/message/temporary/"
        "retry_after_ms/repair/resource_uri/request_id) is in error.data. error.code is "
        "the MCP numeric for not-found — which is ERA-DEPENDENT: -32002 on the handshake "
        "era, -32602 on 2026-07-28, which forbids the old code — or -32603 (read "
        "failure) on both. Branch on error.data.code ('resource_not_found'), not the "
        "numeric, to stay era-agnostic. Note: this "
        "server keeps `code`/`message` rather than the machine_code/human_message "
        "spelling some §6 profiles use — nesting inside `data` already avoids shadowing "
        "the native JSON-RPC keys."
    )
    error_envelope_resource: str = "kimi://error-envelope"
    result_meta_resource: str = "kimi://result-meta"
    # Opt-in tool-reachable fallback: the full 'error-envelope', 'result-meta',
    # 'capabilities-result', and/or 'status-result' schema, and/or the 'parameter-contracts'
    # document (a contract doc, not a JSON Schema), returned only when
    # kimi_capabilities(include_schemas=[...]) requests them, so a resource-blind client can
    # still reach the contracts from tools/list alone (#179, #173). Omitted (exclude_none)
    # from the default payload to keep it small.
    schemas: dict[str, Any] | None = None


ModelCatalogSource = Literal["cache", "static", "none"]


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    display_name: str | None = None
    # Advisory reasoning-effort discovery for the optional `reasoning_effort` param
    # (#309), read from Kimi's on-disk cache. NOT authoritative: the backend's
    # accepted set varies by model and account, and an unlisted effort may still work.
    # These two descriptions (and the dry-run echo descriptions) are in
    # _KEPT_DESCRIPTIONS so the null/[] semantics survive schema-noise stripping and
    # reach the advertised outputSchema.
    default_reasoning_effort: str | None = Field(default=None, description=_DEFAULT_EFFORT_DESC)
    supported_reasoning_efforts: list[str] | None = Field(
        default=None, description=_SUPPORTED_EFFORTS_DESC
    )


class ModelCatalogResult(BaseModel):
    """Advisory list of Kimi model slugs for the optional `model` param.

    Discovery only: `source` says where it came from and `advisory` states it is not
    authoritative (Kimi validates the real slug at exec time). Returned by the
    kimi_models tool and the kimi://models resource; deliberately NOT embedded in
    kimi_capabilities, whose payload is fingerprint-cacheable and must stay stable.
    """

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    source: ModelCatalogSource
    models: list[ModelInfo] = Field(default_factory=list)
    # From the on-disk cache; None for the static fallback / none.
    fetched_at: str | None = None
    cache_client_version: str | None = None
    advisory: str
    # Set only when source == "none" (no cache and no static fallback).
    unavailable_reason: str | None = None
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


ThreadIdSource = Literal["import_notification", "ledger"]


class JobStarted(BaseModel):
    """Returned by the *_async tools: a handle to poll, not a result."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    kind: str  # the tool the job runs, e.g. kimi_delegate
    status: JobState = "running"
    started_at: str  # ISO-8601 UTC
    deadline_seconds: int  # wall-clock cap after which a poll reaps the job
    poll_after_ms: int = JOB_POLL_AFTER_MS  # initial poll delay; grows per poll (see JobStatus)
    # Results are retained `ttl_seconds` AFTER the job completes — the retention
    # window, not a countdown from now. `expires_at` is therefore null until the job
    # finishes; kimi_job_status populates it once a terminal state is reached.
    ttl_seconds: int
    expires_at: str | None = None
    meta: Meta
    # §3 dispatched-vs-applied: the job is dispatched but its outcome is unconfirmed, so the
    # success carrier names the verification surface with literally callable arguments. Reuses
    # the `Repair` shape verbatim — one next-step vocabulary, not two (§6).
    follow_up: Repair
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


class JobStatus(BaseModel):
    """Returned by kimi_job_status: lifecycle state without the full result."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    kind: str
    status: JobState
    started_at: str
    elapsed_ms: int
    deadline_seconds: int
    # Suggested delay before the NEXT poll. For a running job this grows with
    # elapsed runtime (bounded) so successive polls back off instead of tight-
    # looping at the flat base; honor it rather than polling on a fixed interval.
    poll_after_ms: int = JOB_POLL_AFTER_MS
    # Results are retained `ttl_seconds` after the job COMPLETES. `expires_at` is null
    # while running (no completion time yet) and is set once the job is terminal.
    ttl_seconds: int
    expires_at: str | None = None
    result_available: bool = False  # true once status == done
    # The finished job's producer-declared outcome, so a poll can tell a stored
    # FAILURE from a success without fetching the result: true = stored ok:true,
    # false = a stored error envelope, null = not yet known (running), no stored
    # envelope, an unclassifiable payload, or a record finalized before this field.
    # It reports the outcome recorded when the result was written; it does NOT
    # guarantee this reader can still parse the payload — a cross-release record may
    # report an outcome yet fail kimi_job_result with job_result_incompatible, so
    # fetch for the structured error. Always present (null-meaningful), never omitted.
    result_ok: bool | None
    detail: str | None = None  # short human hint (e.g. failure reason)
    # Non-empty when a cancelled/timed-out job's throwaway worktree could not be
    # removed; each entry names the leaked path and reason.
    cleanup_warnings: list[str] = Field(default_factory=list)
    # Advisory polled event-activity (#139). Derived from Kimi's --json stream;
    # silence is NOT proof of a stall and nothing auto-cancels on these. They show
    # RECENT output, complementing elapsed_ms (total runtime).
    events_seen: int = 0  # monotonic count of Kimi events observed
    last_event_at: str | None = None  # ISO-8601 of the most recent event, or None
    event_age_ms: int | None = None  # now - last_event (to completion if terminal)
    workspace: Workspace  # the resolved workspace this status was looked up in (#54)
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


class DryRunResult(BaseModel):
    """Free preview of what a run WOULD do — no Kimi call, no spend."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    tool: Literal["kimi_dry_run"] = "kimi_dry_run"
    cwd: str
    workspace_source: str | None = None
    workspace_warning: str | None = None
    roots_source: RootsSource | None = None  # F8: see Meta.roots_source
    tier: Tier
    sandbox: Sandbox
    isolation: Isolation
    # Same override-provenance semantics as meta.model/meta.reasoning_effort (#309);
    # descriptions kept through stripping via _KEPT_DESCRIPTIONS.
    model: str | None = Field(default=None, description=_DRY_RUN_MODEL_DESC)
    reasoning_effort: str | None = Field(default=None, description=_DRY_RUN_EFFORT_DESC)
    scope: str | None = None
    base: str | None = None
    commit: str | None = None
    paths: list[str] = Field(default_factory=list)
    context_summary: ContextSummary | None = None
    # Whether the previewed review would actually call the model. When False the paid
    # call short-circuits on an empty diff, so `prompt_bytes` is 0 — matching what the
    # real call would send (#320).
    would_call_model: bool  # required: a defaulted True could mask a no-model preview (#322 F5)
    prompt_bytes: int  # full UTF-8 size of the prompt that would be sent (0 if no call)
    coverage: Coverage  # same coverage disclosure the paid review returns (#319/#320)
    max_input_bytes: int
    truncated: bool = False
    truncation_hint: str | None = None
    redacted_paths_count: int = 0
    redacted_paths: list[str] = Field(default_factory=list)
    security_warnings: list[str] = Field(default_factory=list)
    deadline_advisory: str | None = Field(default=None, description=_DEADLINE_ADVISORY_DESC)
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


class WorktreePlan(BaseModel):
    """The baseline a `kimi_delegate` run would seed from, previewed read-only with
    no worktree created. Counts are advisory — uncommitted tracked changes are
    reported but their replay into the worktree is not validated by the preview."""

    model_config = ConfigDict(extra="forbid")
    head_commit: str  # the HEAD commit the worktree detaches at
    head_subject: str | None = None  # short subject of HEAD, if readable
    tracked_files: int  # entries in the HEAD tree (blobs + submodule gitlinks)
    tracked_bytes: int  # approximate total size (blob sizes; gitlinks count as 0)
    uncommitted_tracked_files: int  # tracked files changed vs HEAD (would be replayed)
    untracked_files: int  # untracked files (delegate never copies these)
    note: str | None = None  # plain-language caveats about the previewed baseline


class DelegateDryRunResult(BaseModel):
    """Free preview of what a `kimi_delegate`/`kimi_delegate_async` run WOULD do —
    no Kimi call, no spend, and no worktree created. `tier`/`sandbox` describe the
    previewed propose run, not this read-only preview."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    tool: Literal["kimi_delegate_dry_run"] = "kimi_delegate_dry_run"
    cwd: str
    workspace_source: str | None = None
    workspace_warning: str | None = None
    roots_source: RootsSource | None = None  # F8: see Meta.roots_source
    tier: Tier = "propose"
    sandbox: Sandbox = "workspace-write"
    isolation: Isolation
    # Same override-provenance semantics as DryRunResult's model/reasoning_effort;
    # shared description constants keep the two previews from drifting.
    model: str | None = Field(default=None, description=_DRY_RUN_MODEL_DESC)
    reasoning_effort: str | None = Field(default=None, description=_DRY_RUN_EFFORT_DESC)
    prompt_bytes: int  # full UTF-8 size of the delegate prompt that would be sent
    max_input_bytes: int  # the task byte limit the real run enforces
    worktree_plan: WorktreePlan
    deadline_advisory: str | None = Field(default=None, description=_DEADLINE_ADVISORY_DESC)
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    kind: str
    status: JobState
    started_at: str
    elapsed_ms: int
    result_available: bool = False
    # Same producer-declared outcome as JobStatus.result_ok — lets a list-level
    # triage spot a stored failure without a per-job fetch. Always present.
    result_ok: bool | None
    expires_at: str | None = None


class JobListResult(BaseModel):
    """Returned by kimi_job_list: the workspace's known jobs, newest first."""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    jobs: list[JobSummary] = Field(default_factory=list)
    workspace: Workspace  # the resolved workspace these jobs were listed from (#54)
    # §8 explicit truncation: `truncated` says rows beyond `limit` exist and were dropped —
    # this is a cap, not a page, so there is no cursor to continue with. Always present;
    # the hint is omitted when nothing was cut.
    truncated: bool = False
    truncation_hint: str | None = None
    fingerprint: str = FINGERPRINT
    server_version: str | None = _server_version_field()


_OPAQUE_ERROR_BRANCH = {
    "type": "object",
    "required": ["ok", "error", "meta"],
    "properties": {
        "ok": {"const": False},
        "error": {
            "type": "object",
            "description": "Populated error envelope; full schema at resource kimi://error-envelope",
        },
        "meta": {"type": "object"},
    },
}
_ERROR_POINTER_DESC = _OPAQUE_ERROR_BRANCH["properties"]["error"]["description"]

# The full Meta model is inlined per success branch (~3.5KB each) and dominates the
# tools/list wire response (audit F1, #173). Success branches instead advertise a compact
# opaque meta stub — the server still EMITS the full Meta, so a strict client validating
# structuredContent against this schema still passes ({"type":"object"} accepts any
# object). The full contract is published once at the kimi://result-meta resource.
_RESULT_META_POINTER_DESC = (
    "Result metadata (cwd, tier, sandbox, model, timeout, usage, rate_limit, and more); "
    "full schema at resource kimi://result-meta"
)
_OPAQUE_META = {"type": "object", "description": _RESULT_META_POINTER_DESC}
_META_REF = {"$ref": "#/$defs/Meta"}

# kimi_capabilities/kimi_status embed nested payload fields (tool_details, rate_limit,
# raw_defaults, resolved_defaults) whose $def closures dominate those two tools'
# advertised outputSchemas (#242). Opaquing them to compact stubs — the same treatment
# Meta gets (F1/#173) — applies to named top-level fields via published_schema's
# opaque_fields parameter. The full shapes are published once at kimi://capabilities-result
# and kimi://status-result.
_TOOL_DETAILS_POINTER_DESC = (
    "Per-tool capability records; full schema at resource kimi://capabilities-result"
)
_RATE_LIMIT_POINTER_DESC = "Kimi rate-limit snapshot; full schema at resource kimi://status-result"
_RAW_DEFAULTS_POINTER_DESC = "Raw configured defaults; full schema at resource kimi://status-result"
_RESOLVED_DEFAULTS_POINTER_DESC = (
    "Resolved effective defaults; full schema at resource kimi://status-result"
)
_OPAQUE_TOOL_DETAILS = {"type": "array", "description": _TOOL_DETAILS_POINTER_DESC}
_OPAQUE_RATE_LIMIT = {"type": "object", "description": _RATE_LIMIT_POINTER_DESC}
_OPAQUE_RAW_DEFAULTS = {"type": "object", "description": _RAW_DEFAULTS_POINTER_DESC}
_OPAQUE_RESOLVED_DEFAULTS = {"type": "object", "description": _RESOLVED_DEFAULTS_POINTER_DESC}

# Descriptions that survive _strip_schema_noise: the intentional resource pointers,
# the reasoning-effort field semantics (#309) whose null/[] distinctions an agent
# cannot recover from the bare types, and the fingerprint coverage carve-out (#337),
# which a client cannot recover from a bare array-of-string either.
_KEPT_DESCRIPTIONS = frozenset(
    {
        _ERROR_POINTER_DESC,
        _RESULT_META_POINTER_DESC,
        _TOOL_DETAILS_POINTER_DESC,
        _RATE_LIMIT_POINTER_DESC,
        _RAW_DEFAULTS_POINTER_DESC,
        _RESOLVED_DEFAULTS_POINTER_DESC,
        _DEFAULT_EFFORT_DESC,
        _SUPPORTED_EFFORTS_DESC,
        _DRY_RUN_MODEL_DESC,
        _DRY_RUN_EFFORT_DESC,
        _DEADLINE_ADVISORY_DESC,
        _FINGERPRINT_COVERS_DESC,
        # _TOOL_STABILITY_DESC: verified empirically (removing this entry leaves the full
        # suite green) that no current test needs this registration — its only wire route,
        # CAPABILITIES_RESULT_SCHEMA, is a raw TypeAdapter(...).json_schema() that
        # `published_schema()` never runs through `_strip_schema_noise` (see the comment at
        # the field definition above). `ToolCapability` never appears in the tool's actual
        # outputSchema (CAPABILITIES_SCHEMA) either — `_prune_defs` drops its $def, orphaned
        # by the `tool_details` opaquing, *before* `_strip_schema_noise` would run, so that
        # route can't exercise this set. Kept here anyway for consistency with every other
        # `Field(description=...)` in this module, and as a safety net if a future change
        # ever routes this field through a schema that IS stripped.
        _TOOL_STABILITY_DESC,
    }
)


# Keys whose VALUES are sub-schema maps (property name → sub-schema).
# The keys of these maps are NAMES (e.g. a field called "title"), NOT JSON-Schema
# annotation keywords, so they must never be stripped.
_SUBSCHEMA_MAPS = frozenset(
    ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
)


def _strip_schema_noise(node: object) -> object:
    """Recursively drop generated `title`/`description`/`default`, keeping only the one
    intentional error-pointer description.

    Context-aware: keys that appear inside a *subschema map* (``properties``,
    ``$defs``, etc.) are property/definition NAMES, not JSON-Schema annotation
    keywords.  A Pydantic model field named ``title`` or ``default`` must not be
    removed from the map — only the object-level annotations should be stripped.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("title", "default"):
                continue
            if k == "description" and v not in _KEPT_DESCRIPTIONS:
                continue
            if k in _SUBSCHEMA_MAPS and isinstance(v, dict):
                # Preserve the map keys (they are names, not annotations);
                # recurse only into each sub-schema value.
                out[k] = {name: _strip_schema_noise(sub) for name, sub in v.items()}
            else:
                out[k] = _strip_schema_noise(v)
        return out
    if isinstance(node, list):
        return [_strip_schema_noise(v) for v in node]
    return node


def _opaque_meta_refs(node: object) -> object:
    """Replace every ``{"$ref": "#/$defs/Meta"}`` with the opaque meta stub.

    Meta is only ever referenced as a success envelope's ``meta`` field, so swapping the
    ref anywhere it appears (top-level branches and inside other $defs, covering future
    multi-model unions) collapses the ~3.5KB inlined object to a compact pointer (F1)."""
    if isinstance(node, dict):
        if node == _META_REF:
            return dict(_OPAQUE_META)
        return {k: _opaque_meta_refs(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_opaque_meta_refs(v) for v in node]
    return node


def _local_def_names(node: object) -> set[str]:
    """Names of every ``#/$defs/<name>`` ref reachable in ``node`` (non-recursive over defs)."""
    names: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            names.add(ref.split("/")[-1])
        for v in node.values():
            names |= _local_def_names(v)
    elif isinstance(node, list):
        for v in node:
            names |= _local_def_names(v)
    return names


def _prune_defs(doc: dict) -> dict:  # type: ignore[type-arg]
    """Drop ``$defs`` entries no longer reachable from the document body.

    Reachability is seeded from the doc EXCLUDING ``$defs`` (otherwise every definition
    would be trivially reachable by its own presence in the map), then closed transitively
    over refs between definitions. After the Meta ref is opaqued, its closure
    (RateLimit/RateLimitWindow/Usage/ContextSummary) is orphaned here — unless a
    definition is independently referenced elsewhere (e.g. StatusResult → RateLimit,
    DryRunResult → ContextSummary), in which case per-schema reachability keeps it."""
    defs = doc.get("$defs")
    if not defs:
        return doc
    body = {k: v for k, v in doc.items() if k != "$defs"}
    reachable: set[str] = set()
    frontier = _local_def_names(body)
    while frontier:
        name = frontier.pop()
        if name in reachable or name not in defs:
            continue
        reachable.add(name)
        frontier |= _local_def_names(defs[name])
    doc["$defs"] = {k: v for k, v in defs.items() if k in reachable}
    return doc


# The JSON Schema draft every schema in this server is authored against. Pydantic v2
# (which builds the tool schemas) targets this dialect; we advertise it so both input
# AND output schemas are self-describing — a client knows which draft to validate
# against (audit N4, #185). Single source of truth: the input-schema middleware, every
# published output schema, and the two resource schemas all reference this constant.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def published_schema(
    *success_models: type[BaseModel],
    opaque_fields: dict[str, dict[str, Any]] | None = None,
) -> dict:  # type: ignore[type-arg]
    """Build a tool's advertised outputSchema: the success branch(es) plus ONE fully
    opaque error branch. The opaque branch references no $def, so $defs is exactly the
    success closure (no ErrorInfo, no dangling refs). Generated noise is stripped.

    ``opaque_fields`` maps a top-level success-branch property name to a compact opaque
    stub (e.g. ``{"type": "array", "description": "…kimi://capabilities-result"}``).
    Each named property is replaced before $defs pruning, so the closure it referenced is
    orphaned and dropped — the same shrink the Meta ref gets, applied to named fields (#242).
    """
    if len(success_models) == 1:
        adapter: TypeAdapter = TypeAdapter(success_models[0])  # type: ignore[type-arg]
    else:
        union = success_models[0]
        for m in success_models[1:]:
            union = union | m  # type: ignore[operator]
        adapter = TypeAdapter(union)  # type: ignore[type-arg]
    raw = adapter.json_schema(ref_template="#/$defs/{model}")
    if "anyOf" in raw:
        branches = list(raw["anyOf"])
    else:
        branches = [{k: v for k, v in raw.items() if k != "$defs"}]
    doc = {
        # Advertise the dialect so the output schema is self-describing, symmetric with
        # the stamped input schemas (audit N4, #185). Kept through the transforms below,
        # none of which touch ``$schema``.
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "true = success result, false = error result"},
        },
        "required": ["ok"],
        "anyOf": [*branches, _OPAQUE_ERROR_BRANCH],
        "$defs": raw.get("$defs", {}),
    }
    if opaque_fields:
        for branch in branches:  # success branches only; the error branch is not in `branches`
            props = branch.get("properties")
            if not props:
                continue
            for field, stub in opaque_fields.items():
                if field in props:
                    props[field] = dict(stub)
    # Collapse the inlined Meta object to an opaque pointer, then drop the $defs it (and
    # its now-unreferenced closure) leaves behind (audit F1, #173; #242 for named fields).
    doc = _opaque_meta_refs(doc)
    assert isinstance(doc, dict)
    doc = _prune_defs(doc)
    result = _strip_schema_noise(doc)
    assert isinstance(result, dict)
    return result


# Advertised output schemas (convention: a discriminated ok:true|false union). Each
# active tool advertises its own success shape so verdict/confidence appear only where
# they are meaningful (review), not as perpetually-null fields on consult/delegate (#31).
# The error branch is a single fully-opaque branch (no $defs pollution, no dangling
# refs); the full error contract lives at resource kimi://error-envelope.
CONSULT_RESULT_SCHEMA = published_schema(ConsultResult)
REVIEW_RESULT_SCHEMA = published_schema(ReviewResult)
DELEGATE_RESULT_SCHEMA = published_schema(DelegateResult)
# kimi_job_result/_consume_result return exactly the envelope the originating tool
# produced. Advertising the full three-model union re-embedded ~14.6KB of $defs on BOTH
# tools (audit F1); instead the success branch is opaque and points at the originating
# tool's advertised outputSchema, which the client has already loaded. Payloads are
# validated against the real model server-side (_validate_job_success) before return.
_OPAQUE_JOB_SUCCESS_BRANCH = {
    "type": "object",
    "required": ["ok", "tool"],
    "properties": {
        "ok": {"const": True},
        "tool": {
            "enum": ["kimi_consult", "kimi_review_changes", "kimi_delegate"],
            "description": (
                "Originating tool; the payload matches that tool's advertised "
                "outputSchema success branch — branch on this field."
            ),
        },
    },
}
JOB_RESULT_SCHEMA = {
    "$schema": JSON_SCHEMA_DIALECT,
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "true = success result, false = error result"},
    },
    "required": ["ok"],
    "anyOf": [_OPAQUE_JOB_SUCCESS_BRANCH, _OPAQUE_ERROR_BRANCH],
    "$defs": {},
}
# These three tools return their success model on the happy path, but an invalid
# argument is re-emitted as an ErrorResult at the call-tool boundary (#136), so each
# advertises a success|error union — otherwise that envelope would violate the
# declared output schema for strict MCP clients.
STATUS_SCHEMA = published_schema(
    StatusResult,
    opaque_fields={
        "rate_limit": _OPAQUE_RATE_LIMIT,
        "raw_defaults": _OPAQUE_RAW_DEFAULTS,
        "resolved_defaults": _OPAQUE_RESOLVED_DEFAULTS,
    },
)
CAPABILITIES_SCHEMA = published_schema(
    CapabilitiesResult, opaque_fields={"tool_details": _OPAQUE_TOOL_DETAILS}
)
MODEL_CATALOG_SCHEMA = published_schema(ModelCatalogResult)
# kimi_delegate_async returns only a job handle (or an error) — the eventual delegate
# result is fetched separately via kimi_job_result (DELEGATE_RESULT_SCHEMA).
JOB_STARTED_SCHEMA = published_schema(JobStarted)
JOB_STATUS_SCHEMA = published_schema(JobStatus)
DRY_RUN_SCHEMA = published_schema(DryRunResult)
DELEGATE_DRY_RUN_SCHEMA = published_schema(DelegateDryRunResult)
JOB_LIST_SCHEMA = published_schema(JobListResult)


def _harden_error_envelope_schema(schema: dict) -> dict:  # type: ignore[type-arg]
    """Post-process the raw Pydantic-generated ErrorResult schema to encode invariants
    that Pydantic models enforce at runtime but that JSON Schema cannot express without
    explicit work.

    1. Ensures top-level ``required`` includes ``"ok"`` — Pydantic omits it because
       ``ok`` carries a ``default`` of ``False``.
    2. Encodes the model invariant ``temporary == False ⇒ retry_after_ms is None``
       inside the ``ErrorInfo`` $def via a JSON Schema ``if/then`` conditional.

    Operates on a deep copy to avoid mutating Pydantic's cached output in place.
    """

    s = copy.deepcopy(schema)
    s["$schema"] = JSON_SCHEMA_DIALECT
    # 1. Require ok at the root.
    required = s.setdefault("required", [])
    if "ok" not in required:
        required.append("ok")
    # 2. Encode the temporary=False ⇒ retry_after_ms=null invariant in ErrorInfo.
    error_def = s["$defs"]["ErrorInfo"]
    error_def["if"] = {"properties": {"temporary": {"const": False}}, "required": ["temporary"]}
    error_def["then"] = {"properties": {"retry_after_ms": {"const": None}}}
    return s


# The full error envelope, published once (resource kimi://error-envelope). Root is the
# outer ErrorResult (ok/error/meta) with all $defs — the canonical, discoverable contract.
ERROR_ENVELOPE_SCHEMA = _harden_error_envelope_schema(
    TypeAdapter(ErrorResult).json_schema(ref_template="#/$defs/{model}")
)

# The full result-metadata contract, published once (resource kimi://result-meta). Every
# success envelope carries the opaque `meta` pointer above instead of inlining this ~3.5KB
# object per tool; this is the canonical, discoverable full shape (audit F1, #173).
RESULT_META_SCHEMA = TypeAdapter(Meta).json_schema(ref_template="#/$defs/{model}")
RESULT_META_SCHEMA["$schema"] = JSON_SCHEMA_DIALECT
# The delivered-shape rule, stated where a CLIENT reads the meta contract (#334). Without
# it the omission is only observable by diffing two responses, and the FINGERPRINT bump
# has nothing discoverable to point at. Scoped deliberately to the three RESULT envelopes:
# JobStarted and JobStatus also carry meta/nullable fields and do NOT slim, so an
# unscoped promise would contradict the first async handle an agent sees.
RESULT_META_SCHEMA["description"] = (
    # Rules-then-context (agent-friendly-mcp §3/§4): the binding constraint is its own
    # sentence up front, phrased against observable behavior; mechanics and scope follow.
    "Result metadata for one Kimi run. "
    "Read every optional member with a null-safe accessor; never test for key presence. "
    "An absent key and an explicit null mean the same thing here: not applicable, or not "
    "reported for this run. "
    "Context: on a delivered kimi_consult / kimi_review_changes / kimi_delegate success "
    "envelope, optional members whose value is null are omitted entirely. The six required "
    "members (cwd, tier, sandbox, isolation, timeout_seconds, elapsed_ms) are always "
    "present, and empty arrays stay empty arrays rather than being dropped. Everything "
    "outside meta is delivered verbatim, so top-level fields (including kimi_delegate's "
    "`diff`) and `raw_response` keep their keys even when null. The *_async tools' job "
    "handle and kimi_job_status are not slimmed and still send their nulls."
)

# The full capabilities/status result schemas, published once at kimi://capabilities-result
# and kimi://status-result. The wire outputSchemas opaque tool_details / rate_limit /
# raw_defaults / resolved_defaults and point here for the full shape (#242) — the same
# opaque-pointer treatment Meta gets (F1/#173).
CAPABILITIES_RESULT_SCHEMA = TypeAdapter(CapabilitiesResult).json_schema(
    ref_template="#/$defs/{model}"
)
CAPABILITIES_RESULT_SCHEMA["$schema"] = JSON_SCHEMA_DIALECT
STATUS_RESULT_SCHEMA = TypeAdapter(StatusResult).json_schema(ref_template="#/$defs/{model}")
STATUS_RESULT_SCHEMA["$schema"] = JSON_SCHEMA_DIALECT

# JSON Schema enforced on Kimi's final response for structured findings (passed via
# `kimi -p` with an output schema). It mirrors the agent-visible result fields we
# parse in normalize.py. Kimi returns the final message as JSON conforming to this.
#
# OpenAI strict structured outputs require EVERY property to appear in `required`
# and EVERY object to set additionalProperties:false. "Optional" fields are modeled
# as nullable types (e.g. file/line) that are still listed in `required`.
_FINDINGS_ARRAY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "nit"]},
            "title": {"type": "string"},
            "file": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"]},
            "line_end": {"type": ["integer", "null"]},
            "evidence": {"type": "string"},
            "risk": {"type": "string"},
            "recommendation": {"type": "string"},
        },
        "required": [
            "severity",
            "title",
            "file",
            "line",
            "line_end",
            "evidence",
            "risk",
            "recommendation",
        ],
    },
}
_STR_ARRAY_SCHEMA = {"type": "array", "items": {"type": "string"}}

# Review output: a verdict-bearing structured review. Used by kimi_review_changes.
FINDINGS_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "concerns", "fail", "unknown"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "findings": _FINDINGS_ARRAY_SCHEMA,
        "questions": _STR_ARRAY_SCHEMA,
        "assumptions": _STR_ARRAY_SCHEMA,
        "next_steps": _STR_ARRAY_SCHEMA,
    },
    "required": [
        "summary",
        "verdict",
        "confidence",
        "findings",
        "questions",
        "assumptions",
        "next_steps",
    ],
}

# Consult output: a read-only answer. Same shape MINUS verdict/confidence — Kimi is
# never asked to invent a verdict for plain Q&A (#31). Used by kimi_consult.
CONSULT_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "findings": _FINDINGS_ARRAY_SCHEMA,
        "questions": _STR_ARRAY_SCHEMA,
        "assumptions": _STR_ARRAY_SCHEMA,
        "next_steps": _STR_ARRAY_SCHEMA,
    },
    "required": ["summary", "findings", "questions", "assumptions", "next_steps"],
}
