"""Central construction and serialization of the error envelope.

One place owns the §6 symbolic-repair mapping and the null-stripping policy so every
error path emits the identical, machine-actionable shape. Nothing in `_core` imports
this module (it depends on the parent package's `schemas`)."""

from __future__ import annotations

from typing import Any

from moonbridge.schemas import (
    ErrorCode,
    ErrorDetail,
    ErrorInfo,
    ErrorResult,
    InvalidArgument,
    Repair,
    RepairStep,
)


class _KeepTableTool:
    """Sentinel for `make_error`'s `repair_tool`: leave the per-code table's tool in place.

    Distinct from `None`, which explicitly CLEARS the tool (the emitted repair carries no
    `tool`). A plain `None` default could not express "clear" — it was indistinguishable
    from "not overridden" — so a call site whose failure the table's tool does not fit
    (e.g. a local config-shape refusal that must not steer to kimi_models) uses this
    sentinel default and passes `repair_tool=None` to drop the tool."""

    __slots__ = ()


_KEEP_TABLE_TOOL = _KeepTableTool()

# code -> (next_step, repair_tool, temporary, default alternative prose)
_REPAIR_BY_CODE: dict[str, tuple[RepairStep, str | None, bool, str]] = {
    "kimi_not_found": (
        "install_kimi",
        None,
        False,
        "Install the Kimi Code CLI (https://moonshotai.github.io/kimi-code/), then rerun "
        "kimi_status.",
    ),
    "kimi_auth_required": (
        "authenticate",
        None,
        False,
        "Run `kimi login` (device-code flow), or configure a provider in config.toml, then "
        "rerun kimi_status.",
    ),
    "kimi_auth_indeterminate": (
        # NOT `authenticate`: the caller may well be logged in — the probe, not the
        # session, is what failed to answer, so telling them to run `kimi login` would
        # be a false repair step.
        #
        # temporary=True is load-bearing on an emission-site invariant: `login_status()`
        # returns None for a missing binary OR an unanswered probe, and a missing binary
        # is NOT temporary. Every caller must therefore rule the binary out first (today
        # kimi_transfer does, via the kimi_version() -> kimi_not_found gate that runs
        # ahead of this one), leaving "the probe didn't answer in time" as the only cause
        # this code can carry — and that does clear itself. A new emission site that
        # skips that gate must map binary-missing to kimi_not_found, not here.
        "inspect_and_retry",
        None,
        True,
        "The `kimi provider list` probe did not complete, so provider configuration could not "
        "be confirmed. "
        "Run kimi_status to check, then retry.",
    ),
    "unexpanded_env_placeholder": (
        "update_plugin",
        None,
        False,
        "Set the referenced environment variable, or fix the plugin config.",
    ),
    "unsupported_tier": (
        "use_allowed_value",
        None,
        False,
        "Pass one of the tier's allowed_values.",
    ),
    "unsupported_sandbox": (
        "use_allowed_value",
        None,
        False,
        "Pass one of the sandbox's allowed_values.",
    ),
    "unsupported_isolation": (
        "use_allowed_value",
        None,
        False,
        "Pass one of isolation's allowed_values.",
    ),
    "unsupported_detail": (
        "use_allowed_value",
        None,
        False,
        "Pass one of detail's allowed_values.",
    ),
    "invalid_scope": ("correct_arguments", None, False, "Correct the scope argument."),
    "invalid_base": ("correct_arguments", None, False, "Correct the base argument."),
    "invalid_commit": ("correct_arguments", None, False, "Correct the commit argument."),
    "invalid_paths": ("correct_arguments", None, False, "Correct the paths argument."),
    "invalid_arguments": (
        "correct_arguments",
        None,
        False,
        "Check each tool's inputSchema (tools/list) or kimi_capabilities, then retry.",
    ),
    "invalid_workspace_root": (
        "correct_arguments",
        None,
        False,
        "Pass an absolute path to an existing repository root.",
    ),
    "workspace_outside_roots": (
        "use_workspace_in_roots",
        None,
        False,
        "Pass a workspace_root inside one of candidate_roots.",
    ),
    "input_too_large": (
        "reduce_input",
        None,
        False,
        "Trim the input below limit_bytes, or raise the configured byte limit.",
    ),
    "not_a_git_repo": (
        "init_git_repo",
        None,
        False,
        "Point workspace_root at a git repository (propose needs one).",
    ),
    "git_unavailable": (
        "install_git",
        None,
        False,
        "Install git and ensure it is on PATH.",
    ),
    "worktree_error": (
        "inspect_and_retry",
        None,
        False,
        "Inspect the repository state; retry only after correcting it.",
    ),
    "context_too_large": (
        "reduce_input",
        None,
        False,
        "Narrow paths/scope so the gathered context fits.",
    ),
    "timeout": (
        "inspect_and_retry",
        # tool stays None: `timeout` is emitted from a shared classifier (kimi.classify_failure)
        # serving consult, review AND delegate, so a single repair.tool naming one async variant
        # would misdirect the other callers. The prose names all three instead (#195).
        None,
        True,
        "Retrying the same synchronous call (kimi_consult / kimi_review_changes / "
        "kimi_delegate) will likely time out again. Prefer re-running it via the matching "
        "async tool (kimi_consult_async / kimi_review_changes_async / kimi_delegate_async), "
        "then poll kimi_job_status and fetch kimi_job_result — async jobs run to the "
        "separately configured background-job deadline (default 1800s) rather than the 10-600s "
        "sync clamp. Otherwise narrow the task or raise timeout_seconds, then retry.",
    ),
    "nonzero_exit": (
        "inspect_and_retry",
        None,
        False,
        "Inspect the error; retry with a smaller or corrected task.",
    ),
    "resource_not_found": (
        "list_resources",
        # repair.tool names a server TOOL to call; the authoritative resource-discovery
        # path is the MCP `resources/list` method (not a tool), so tool stays None and
        # the alternative prose names both it and kimi_capabilities (#181/F9).
        None,
        False,
        "List the available resource URIs via the MCP resources/list method (or "
        "kimi_capabilities), then retry with an exact URI.",
    ),
    "invalid_json": ("retry_then_report", None, True, "Retry; if it persists, report a bug."),
    "schema_violation": (
        "retry_then_report",
        None,
        True,
        "Retry; if it persists, report a bug.",
    ),
    "internal_error": (
        "retry_then_report",
        None,
        True,
        "Retry; if it persists, run kimi_status and inspect the repo.",
    ),
    "cli_contract_changed": (
        "update_plugin",
        None,
        False,
        "Update moonbridge (the installed kimi CLI changed its contract);"
        " or pin kimi to a supported version, and run kimi_status to check the version.",
    ),
    "invalid_reasoning_effort": (
        "correct_arguments",
        "kimi_models",
        False,
        "The Kimi backend rejected the requested reasoning_effort for this model/account. "
        "Pass an effort the model supports (kimi_models lists advisory per-model "
        "supported_reasoning_efforts), pick a model/effort pair explicitly, or omit "
        "reasoning_effort (and unset MOONBRIDGE_REASONING_EFFORT) to let Kimi resolve it.",
    ),
    "empty_response": (
        "retry_then_report",
        None,
        True,
        "Kimi finished without producing an answer. kimi emits no --output-last-message, "
        "so a read-only tier recovers its answer from the event stream; an empty stream "
        "means there is nothing to report. Retry, and if it persists narrow the question.",
    ),
    "invalid_model": (
        "correct_arguments",
        "kimi_models",
        False,
        "Kimi could not resolve a model alias. `-m` takes an ALIAS defined in the user's "
        'config.toml (e.g. a `[models."..."]` key), not a raw provider model id. '
        "Call kimi_models for the configured aliases, or omit model to use the "
        "configured default_model. If the message names the CONFIGURED DEFAULT rather "
        "than the alias you passed, neither will help — that is a broken `default_model` "
        "in config.toml, which only the user can fix.",
    ),
    "extra_args_rejected": (
        "correct_config",
        None,
        False,
        "kimi rejected an entry in MOONBRIDGE_EXTRA_ARGS (operator config, not a "
        "plugin drift). Fix or remove the offending option/config key/profile and rerun.",
    ),
    "kimi_rate_limited": (
        "retry_after_delay",
        None,
        True,
        "Wait retry_after_ms before retrying; reduce concurrent kimi calls.",
    ),
    # The three `transfer_*` entries were removed with their codes — see the note in
    # schemas.ErrorCode. Their prose named `kimi_transfer`, `npm install -g @openai/kimi`,
    # and `kimi resume`: a tool, a package, and a subcommand that none exist here.
    "job_not_found": (
        "list_jobs",
        "kimi_job_list",
        False,
        "Call kimi_job_list to recover known job_ids in this workspace.",
    ),
    "job_running": (
        "poll_job_status",
        "kimi_job_status",
        True,
        "Poll kimi_job_status until result_available, honoring poll_after_ms.",
    ),
    "job_cancelled": ("start_new_job", None, False, "Start a new job."),
    "job_timeout": ("start_new_job", None, False, "Start a new job."),
    "job_result_incompatible": (
        "start_new_job",
        None,
        False,
        # No "restore the writing release" promise: the record survives a consume
        # attempt (#306), but TTL reaping or count-cap eviction can still remove it
        # before a restore is attempted.
        "This stored result was written by a different moonbridge release and cannot "
        "be read by the one now running; retrying cannot succeed. Start a new job under "
        "the current release, with a new (or no) idempotency_key — a reused old key can "
        "replay this same unreadable record while it still exists.",
    ),
    "job_failed": (
        "inspect_and_retry",
        None,
        False,
        "Inspect the failure detail; start a new job.",
    ),
    "idempotency_conflict": (
        "use_new_idempotency_key",
        None,
        False,
        "This idempotency_key was already used with different effective arguments or"
        " server execution settings; resend the original arguments to replay, or pass a"
        " new idempotency_key to run again.",
    ),
    "idempotency_result_unavailable": (
        "use_new_idempotency_key",
        None,
        False,
        "The prior run for this idempotency_key already completed and its result is no"
        " longer available; pass a new idempotency_key to run again.",
    ),
    "idempotency_in_progress": (
        "retry_after_delay",
        None,
        True,
        "A run for this idempotency_key is still starting; retry the same call after"
        " retry_after_ms.",
    ),
}


def make_error(
    code: ErrorCode,
    message: str,
    *,
    retry_after_ms: int | None = None,
    temporary: bool | None = None,
    repair_next_step: RepairStep | None = None,
    repair_tool: str | _KeepTableTool | None = _KEEP_TABLE_TOOL,
    repair_arguments: dict[str, Any] | None = None,
    repair_alternative: str | None = None,
    details: ErrorDetail | None = None,
    invalid_arguments: list[InvalidArgument] | None = None,
    limit_bytes: int | None = None,
    actual_bytes: int | None = None,
    candidate_roots: list[str] | None = None,
) -> ErrorInfo:
    """Build the §6 error envelope for `code`, deriving the symbolic repair from the
    per-code table. `temporary` defaults to the table's value; pass it to override.
    `retry_after_ms` is honored only when the result is temporary. `repair_tool` overrides
    the table's repair tool when the failing tool is known at the call site but not fixed
    per code (e.g. invalid_arguments names the tool that was called — #184/N3).
    `repair_next_step` likewise overrides the table's symbolic step when one code's
    recovery differs by call site (e.g. a KEYED sync timeout leaves a still-running job
    to poll rather than retry — #201). `repair_tool` distinguishes three states: omitted
    keeps the table's tool; a string overrides it; explicit `None` CLEARS it (no tool in
    the emitted repair) for a call site the table's tool does not fit — e.g. #332's local
    config-shape refusal, which must not steer to kimi_models.

    `invalid_arguments` carries one extra invariant (#416): `docs/REFERENCE.md` promises
    the per-argument list on EVERY envelope with that code, with `details` mirroring the
    first entry. Two producers had drifted from that promise independently, so the rule
    lives here — the one constructor every producer goes through — rather than being
    re-implemented per call site. BOTH halves are enforced: a missing list raises, and
    `details` is ALWAYS derived from the first entry. Deriving unconditionally (rather
    than only filling a gap) is what makes the mirror structural — a call site cannot pass
    a `details` that disagrees, because it cannot pass one at all. Nothing needs to: the
    one detail shape no single entry can mirror, `fields=[...]` for a combined-size
    failure, belongs to `input_too_large`.

    Deliberately not an `ErrorInfo` model_validator: replay reconstructs stored records
    with `ErrorResult.model_validate`, and a record written before this rule legitimately
    carries no list — validating there would make those jobs unreadable."""
    if code == "invalid_arguments":
        if not invalid_arguments:
            raise ValueError(
                "make_error: an invalid_arguments envelope must carry a non-empty "
                "invalid_arguments list (docs/REFERENCE.md)"
            )
        if details is not None:
            raise ValueError(
                "make_error: `details` is derived from invalid_arguments[0] for this code "
                "and must not be supplied (docs/REFERENCE.md: details mirrors the first)"
            )
        first = invalid_arguments[0]
        details = ErrorDetail(
            field=first.field, reason=first.reason, allowed_values=first.allowed_values
        )
    next_step, tool, temp_default, alt_default = _REPAIR_BY_CODE[code]
    if repair_next_step is not None:
        next_step = repair_next_step
    if not isinstance(repair_tool, _KeepTableTool):
        tool = repair_tool
    is_temp = temp_default if temporary is None else temporary
    backoff = retry_after_ms if is_temp else None
    repair = Repair(
        next_step=next_step,
        tool=tool,
        arguments=repair_arguments,
        alternative=repair_alternative or alt_default,
    )
    return ErrorInfo(
        code=code,
        message=message,
        temporary=is_temp,
        retry_after_ms=backoff,
        repair=repair,
        details=details,
        invalid_arguments=invalid_arguments,
        limit_bytes=limit_bytes,
        actual_bytes=actual_bytes,
        candidate_roots=candidate_roots,
    )


def serialize_error_info(error: ErrorInfo) -> dict:
    """Serialize a bare `ErrorInfo`, stripping absent optionals (§8) but ALWAYS retaining
    `retry_after_ms` (§6 wants the key present even when null).

    This is the §6 shape carried in a resource-read failure's JSON-RPC `error.data`
    (audit F9, #181): no `ok`/`meta` wrapper, since a resources/read has no Kimi run to
    describe — inventing a tier/sandbox for it would be dishonest. `serialize_error`
    reuses this so the null-retention policy lives in exactly one place."""
    payload = error.model_dump(mode="json", exclude_none=True)
    payload.setdefault("retry_after_ms", None)
    return payload


def serialize_error(result: ErrorResult) -> dict:  # type: ignore[type-arg]
    """Serialize an ErrorResult, stripping absent optionals (§8) but ALWAYS retaining
    `error.retry_after_ms` (§6 wants the key present even when null)."""
    payload = result.model_dump(mode="json", exclude_none=True)
    payload["error"] = serialize_error_info(result.error)
    return payload
