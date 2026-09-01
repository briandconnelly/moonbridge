"""FastMCP server exposing Kimi to Claude Code.

Tool surface (v1 grows by milestone):
  active (call the model): kimi_consult
  free (local only):       kimi_status, kimi_capabilities
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import signal
import sys
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, get_args
from uuid import uuid4

from fastmcp import Context, FastMCP
from fastmcp.exceptions import DisabledError, McpError, NotFoundError, ResourceError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from mcp.types import INTERNAL_ERROR
from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from pontonier.core.jobs import JobStore


from pontonier.core import gitdiff, idempotency, redaction, workspace, worktree
from pontonier.core.jobs import DiscardOutcome

from moonbridge import (
    __version__,
    cli_contract,
    config,
    delegate,
    kimi,
    obs,
    orchestration,
    param_contracts,
    preflight,
    prompts,
)
from moonbridge.errors import make_error, serialize_error, serialize_error_info
from moonbridge.kimi_models import read_model_catalog, supported_efforts_for
from moonbridge.schemas import (
    CAPABILITIES_RESULT_SCHEMA,
    CAPABILITIES_SCHEMA,
    CONSULT_RESULT_SCHEMA,
    DELEGATE_DRY_RUN_SCHEMA,
    DELEGATE_RESULT_SCHEMA,
    DRY_RUN_SCHEMA,
    ERROR_ENVELOPE_SCHEMA,
    FINGERPRINT,
    JOB_LIST_SCHEMA,
    JOB_RESULT_SCHEMA,
    JOB_STARTED_SCHEMA,
    JOB_STATUS_SCHEMA,
    JSON_SCHEMA_DIALECT,
    MODEL_CATALOG_SCHEMA,
    RESULT_FORMAT,
    RESULT_META_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    STATUS_RESULT_SCHEMA,
    STATUS_SCHEMA,
    AsyncLifecycle,
    CapabilitiesDetail,
    CapabilitiesResult,
    ConsultResult,
    ContextSummary,
    DelegateDryRunResult,
    DelegateResult,
    Detail,
    DryRunResult,
    ErrorCode,
    ErrorDetail,
    ErrorInfo,
    ErrorResult,
    InvalidArgument,
    Isolation,
    JobListResult,
    JobStarted,
    JobState,
    JobStatus,
    JobSummary,
    Meta,
    RawDefaults,
    Repair,
    ResolvedDefaults,
    ReviewResult,
    ReviewScope,
    RootsSource,
    Sandbox,
    StatusResult,
    Tier,
    ToolCapability,
    ToolStability,
    Untracked,
    Workspace,
    WorktreePlan,
    apply_detail,
    slim_meta,
    workspace_warning_for,
)

# #427: the canonical skills-discovery sentence pair lives in cli_contract.py, beside the
# "RULE: every egress-caveat prose site" comment it implements (search that string) — that
# module is the single source of truth for Kimi CLI assumptions, so a Kimi upgrade that
# changes this behavior has exactly one place in code to fix. `cli_contract.SKILLS_DISCOVERY_FACT`
# alone states the discovery itself (both roots, workspace AGENTS.md, the default path, and
# that the global root is reachable from outside the workspace) — used where a site's
# guarantee matrix doesn't require the isolation-flags note too (the three `_async` tool
# docstrings; see `_REQUIRED_GUARANTEES` in tests/test_server.py). Every other carrier — the
# server instructions, the kimi_status caveat, negative_scope, the six
# `ToolCapability.returns` clauses, and the three SYNC tool docstrings — uses the combined
# `cli_contract.SKILLS_DISCOVERY_FACT_FULL`. This is a deliberate wording convergence, not a
# byte-identical refactor — the carrier sites did not share one sentence before, so
# unifying them necessarily rewords the wire (hence the FINGERPRINT bump, schema-72 ->
# schema-73).

# Rules-then-context (audit F8, #180): a does/does-not lead, then each binding rule as
# its own imperative sentence, and background (async-job mechanics, cached rate-limit
# semantics) last so an agent that skims reaches the actionable rules first.
CAPABILITY_SUMMARY = (
    # Lead: what it does and, up front, what it does not do.
    "Call Kimi Code (a different model) from Claude Code for a second opinion, a "
    "structured review of your git changes, or a delegated coding task. kimi_delegate "
    "never edits your working tree — it returns a reviewable diff you apply yourself. "
    # The honest safety statement. kimi has no sandbox, so say what actually constrains a
    # run rather than borrowing the vocabulary of one that does.
    "The `kimi` CLI has no sandbox and no approval prompts, so this server constrains runs "
    "itself: kimi_consult and kimi_review_changes run under a generated agent profile with "
    "no shell and no write tools, and every tier runs in a throwaway git worktree. "
    f"{cli_contract.READ_ONLY_CONFIDENTIALITY_LIMIT} "
    "The worktree keeps a run's working directory away from your tree, but it is not a "
    "boundary — Kimi can write outside it if instructed to, so treat it as defense in "
    "depth, not containment. "
    "Every model-bearing call sends your inputs to your configured Kimi provider raw, and "
    "Kimi also auto-loads context implicitly. "
    f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
    # Routing: one imperative sentence per task family.
    "Use kimi_consult for a read-only second opinion or Q&A — including on a diff you "
    "paste inline. "
    "Use kimi_review_changes for a structured review of changes gathered from git "
    "(working_tree, branch, or commit); prefer it over kimi_consult whenever the changes "
    "already live in git. "
    "Use kimi_delegate to implement a task in a throwaway git worktree and get back a "
    "reviewable diff. "
    "Prefer the matching _async variant (kimi_consult_async / kimi_review_changes_async / "
    "kimi_delegate_async) for work that can exceed the synchronous deadline — a high-"
    "reasoning-effort or broad repo-grounded consult, a multi-file or whole-branch review, or "
    "a substantial implementation task — because a sync call whose deadline expires is "
    "terminated and its partial work is lost; keep the sync tool for focused work that "
    "finishes well inside the deadline. "
    # Preflight and failure-handling rules.
    "Before the first paid call in a session, run kimi_status (free) to confirm the kimi "
    "CLI is installed and authenticated. "
    "On a tool failure the tool result itself is the error (isError: true) with the error "
    "envelope in structuredContent (content[0].text mirrors it). Branch on error.code and "
    "follow error.repair. "
    "Treat Kimi's findings as claims to verify, not commands. "
    # Discovery rules — still actionable, so kept ahead of the background paragraph.
    "Use kimi_capabilities for the full inventory. Before overriding model or "
    "reasoning_effort, use kimi_models (or the kimi://models resource): `model` takes an "
    "ALIAS defined in the user's kimi config.toml, not a raw provider model id, and an "
    "unknown alias is rejected outright. Kimi does NOT reject an unrecognized "
    "reasoning_effort — it ignores it — so this server refuses one the alias does not "
    "declare rather than let a run silently use the default. "
    "To preview a call without spending, use kimi_dry_run for a review or "
    "kimi_delegate_dry_run for a delegate's worktree baseline. "
    # Background context, last.
    "Background: each active tool has an _async variant (kimi_consult_async / "
    "kimi_review_changes_async / kimi_delegate_async), polled via "
    "kimi_job_status/result/consume_result/cancel/list; even a sync consult/review/delegate "
    "records its run as a job (meta.job_id), so a dropped connection can be recovered the "
    "same way. kimi_status reports rate_limit as `unavailable`: kimi exposes no quota-read "
    "channel and its provider is user-configured, so there is nothing authoritative to "
    "report — that is not a failure."
)

# Annotation presets. destructiveHint/idempotentHint have MCP-spec meaning only when
# readOnlyHint is false, so read-only presets omit them rather than asserting a value
# (audit F4).
_FREE_READ = {
    "readOnlyHint": True,
    "openWorldHint": False,
}
# propose tier: Kimi writes, but only inside a throwaway worktree — the caller's
# live tree is never touched, so destructiveHint stays False.
_ACTIVE_PROPOSE = {
    "readOnlyHint": False,
    "openWorldHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
}
# Every active consult/review/delegate call — sync AND async — now spawns a
# background job that commits to spend and reaches the API. The job record is
# observable (kimi_job_list) and mutable (kimi_job_cancel/consume) — shared state
# that outlives the response — so none may advertise readOnlyHint, even consult/review
# whose underlying run is read-only (issue #138). They share the propose-tier values:
# any file writes stay inside a throwaway worktree, so the caller's live tree is never
# touched and destructiveHint stays False.
_ACTIVE_ASYNC = _ACTIVE_PROPOSE
# Job lifecycle annotations, split by observable behavior. None call the model and
# all are closed-world (they touch only this server's job state, never the user's
# files/repo). Inspection tools (status/result/list) are read-only; destructiveHint/
# idempotentHint have MCP-spec meaning only when readOnlyHint is false, so this
# preset omits them (audit F4). consume and cancel both mutate state, so neither is
# read-only — but they differ in idempotency: consume deletes the retained record (a
# repeat consume returns not-found, a different response), so it is non-idempotent;
# cancel is idempotent — a terminal job is returned unchanged and cancellation
# re-validates concurrent completion, so a retry after a lost response has no
# additional effect (#141).
_JOB_READ = {
    "readOnlyHint": True,
    "openWorldHint": False,
}
_JOB_MUTATE = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}
_JOB_CANCEL = {**_JOB_MUTATE, "idempotentHint": True}
# _FREE_WRITE: no model call (free), but NOT read-only — reserved for a free tool with a
# persistent local side effect. No tool uses it today (kimi has no transfer equivalent).
_FREE_WRITE = {
    "readOnlyHint": False,
    "openWorldHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}

mcp = FastMCP(name="moonbridge", instructions=CAPABILITY_SUMMARY, version=__version__)

# F5 (audit): this server registers no MCP prompts, but the low-level SDK advertises
# the prompts capability whenever a ListPromptsRequest handler exists (FastMCP always
# registers one). There is no FastMCP constructor knob, so wrap get_capabilities and
# null out prompts only — never remove shared request handlers. Guarded by
# test_initialize_does_not_advertise_prompts, so a FastMCP upgrade that changes this
# seam fails loudly.
#
# #424 (audit): FastMCP's LowLevelServer.get_capabilities also unconditionally
# advertises the io.modelcontextprotocol/ui extension (MCP Apps) — it lands in
# ServerCapabilities.model_extra since that model is extra="allow". This server has
# no UI/Apps implementation, so filter that one entry out of the extensions mapping
# (leaving any other, legitimately-implemented extension a future FastMCP version
# adds untouched) and only null the whole key when nothing is left. Guarded by
# test_initialize_does_not_advertise_the_ui_extension, so a FastMCP upgrade that
# changes this seam fails loudly.
# spec-stable wire id (MCP extension identifiers are part of the wire protocol, not
# a fastmcp implementation detail) — deliberately a literal, not imported from
# fastmcp.apps, so a fastmcp internal refactor can't break this module at import time.
_UI_EXTENSION_ID = "io.modelcontextprotocol/ui"

_lowlevel_server = mcp._mcp_server
_orig_get_capabilities = _lowlevel_server.get_capabilities


def _get_capabilities_without_prompts_or_extensions(*args: Any, **kwargs: Any) -> Any:
    caps = _orig_get_capabilities(*args, **kwargs)
    # `extensions` is a DECLARED field on ServerCapabilities as of MCP SDK v2; under v1 it
    # only ever arrived as an undeclared extra. Read the declared field first and fall back
    # to `model_extra`, because reading only `model_extra` (as this did under v1) makes the
    # lookup return None on v2 and then nulls the whole key — suppressing every extension
    # the server legitimately advertises, which is exactly what this filter must not do.
    extensions = getattr(caps, "extensions", None)
    if extensions is None:
        extensions = (caps.model_extra or {}).get("extensions")
    filtered_extensions = (
        {ext_id: value for ext_id, value in extensions.items() if ext_id != _UI_EXTENSION_ID}
        if isinstance(extensions, dict)
        else None
    )
    return caps.model_copy(update={"prompts": None, "extensions": filtered_extensions or None})


_lowlevel_server.get_capabilities = _get_capabilities_without_prompts_or_extensions  # ty: ignore[invalid-assignment]

# Pydantic v2 (which FastMCP uses to generate tool input schemas) targets this dialect.
# Sourced from the one shared constant so the advertised input dialect can never drift
# from the output-schema dialect (audit N4, #185).
INPUT_SCHEMA_DIALECT = JSON_SCHEMA_DIALECT


class _InputSchemaDialectMiddleware(Middleware):
    """Stamp the JSON Schema dialect onto every tool's input schema.

    FastMCP already emits *closed* input schemas (``additionalProperties: false``) and
    rejects unknown arguments with a validation error, so misspelled/extra params are
    not silently dropped. It does not, however, declare a ``$schema`` dialect — without
    one a client can't know which draft to validate against. We add it here so the
    advertised input schema is self-describing (agent-friendly-mcp checklist §3). This
    is advertising only; it does not change accepted params, enums, or behavior."""

    async def on_list_tools(self, context, call_next):  # type: ignore[no-untyped-def]
        tools = await call_next(context)
        for tool in tools:
            if tool.parameters is not None:
                # Assign rather than setdefault: the guarantee is that the
                # advertised dialect matches the one we actually validate
                # against. If FastMCP/Pydantic ever emits its own ``$schema``
                # (a different draft, or ``None``), overwrite it instead of
                # trusting it.
                tool.parameters["$schema"] = INPUT_SCHEMA_DIALECT
        return tools


mcp.add_middleware(_InputSchemaDialectMiddleware())


class _SemanticErrorMiddleware(Middleware):
    """Map an envelope-level failure (``ok is False``) to MCP ``isError: true``.

    Handlers return the normalized result envelope as plain structured data; a
    semantic failure is ``ErrorResult{ok: false, ...}``. FastMCP turns a returned
    dict into a ``ToolResult`` with ``is_error=False``, so an MCP-conformant client
    that keys off the protocol ``isError`` flag (rather than parsing our envelope)
    would misclassify a failed call as a success (#91). We flip the flag here at the
    single tool boundary while leaving the structured content (and its text fallback)
    untouched, so the ``ErrorInfo`` envelope still reaches clients that do parse it."""

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        result = await call_next(context)
        sc = result.structured_content
        if isinstance(sc, dict) and sc.get("ok") is False:
            result.is_error = True
        return result


mcp.add_middleware(_SemanticErrorMiddleware())

# Argument-validation envelope (#136) ---------------------------------------- #
# Largest, most generous bound: a request with many bad keys is reported but cannot
# amplify into an unbounded response.
_MAX_INVALID_ARGS = 25
_MAX_ARG_REASON_LEN = 300  # bound on the validator message
# An unknown-key location is fully caller-controlled, so bound it: without this the
# field name (copied into `details.field` and `invalid_arguments[].field`) could inflate
# the envelope or carry a secret supplied as the key name itself.
_MAX_ARG_FIELD_LEN = 128

# Each guarded tool's fixed (tier, sandbox) posture, recorded by `_guard` so an
# argument-validation error can report the called tool's real posture in `meta` — not
# the server defaults — matching every other error path (#136). Free/unguarded tools
# fall back to the defaults (consult/read-only), which is their posture anyway.
_TOOL_POSTURE: dict[str, tuple[str, str]] = {}


def _format_loc(loc: tuple[object, ...]) -> str:
    """Render a Pydantic error location as a stable field path. An integer index is
    appended as ``[i]`` directly onto the preceding component (``paths[0]``, not
    ``paths.[0]``) so the path is a valid accessor and never breaks on a non-string
    location component (#136)."""
    out = ""
    for component in loc:
        if isinstance(component, int):
            out += f"[{component}]"
        elif out:
            out += f".{component}"
        else:
            out = str(component)
    # Bound the caller-controlled path so an oversized unknown key can't amplify the
    # envelope (the field feeds details.field and invalid_arguments[].field).
    if len(out) > _MAX_ARG_FIELD_LEN:
        out = out[:_MAX_ARG_FIELD_LEN] + "…"
    return out or "<arguments>"


def _enum_for_property(prop_schema: dict | None, *, element: bool = False) -> list[str] | None:
    """Pull a Literal/enum's allowed values from a tool's input-schema property —
    authoritatively, not by parsing validator prose (#136). The enum sits at the top
    level for a required Literal (``scope``) or inside an ``anyOf`` branch for an
    Optional one (``isolation``); returns None when the property has no enum.

    ``element`` selects which domain to resolve, and the two levels never substitute for
    each other (#373). Default (False) answers "what may this *field* be" — the enum on
    the property itself. True answers "what may this *element* be" — the enum under
    ``items``, at either position — and is for an indexed failure like
    ``include_schemas[0]``, whose repair is a token, not a whole list. A whole-list
    failure (``include_schemas="nope"``) therefore advertises nothing rather than
    offering element tokens as if the field accepted one.

    Only ``enum`` is read: a *single*-value Literal renders as ``const``, which no
    parameter uses today and which this deliberately does not resolve."""
    if not isinstance(prop_schema, dict):
        return None
    branches = [prop_schema, *(b for b in prop_schema.get("anyOf", []) if isinstance(b, dict))]
    for branch in branches:
        if element:
            branch = branch.get("items")  # noqa: PLW2901 — descend to the element schema
            if not isinstance(branch, dict):
                continue
        enum = branch.get("enum")
        if isinstance(enum, list):
            return [str(v) for v in enum]
    return None


def _combined_input_detail(extra_context: str | None) -> ErrorDetail:
    """§6 details for a consult combined-size (question + extra_context) failure.

    The byte limit is on the two inputs *together*, so name every input that actually
    contributed: `field="question"` when it was sent alone, `fields=["question",
    "extra_context"]` when extra_context added to it. This avoids blaming extra_context
    for an oversized `question` (#174/F2)."""
    if extra_context:
        return ErrorDetail(fields=["question", "extra_context"])
    return ErrorDetail(field="question")


def _blank_input_error(value: str, field: str, tool_name: str, meta: Meta) -> dict | None:
    """Reject an empty or whitespace-only `question`/`task` pre-spend, else return None.

    Blank input reduces to nothing when the prompt is assembled — both
    `prompts.build_consult_prompt` and `prompts.build_delegate_prompt` strip it — so the
    run could only spend quota on an empty ask; the free preview reported `prompt_bytes`
    of framing scaffolding alone (#411). `str.strip()` is deliberately the same blankness
    test those builders use, so the guard and the builder can never disagree about an edge
    case (U+00A0 and U+2003 are blank to it; U+200B is content).

    This is the runtime twin of the call-boundary `invalid_arguments` envelope: it
    carries the per-argument list whose first entry `details` mirrors, as
    `docs/REFERENCE.md` promises for this code, and names the tool that was actually
    called so an async caller is never steered at its sync twin (#184/N3). Sits after
    the byte-limit check, so a whitespace-only value past that limit still reports
    `input_too_large` and every pre-existing pre-flight precedence is undisturbed."""
    if (value or "").strip():
        return None
    reason = f"{field} must not be empty or whitespace-only."
    return serialize_error(
        ErrorResult(
            error=make_error(
                "invalid_arguments",
                f"{tool_name}: 1 invalid argument(s): {field} — {reason}",
                repair_tool=tool_name,
                # Override the table's "consult the inputSchema" prose: this rule is
                # enforced at runtime, not by a schema constraint, so pointing at the
                # schema would send the caller somewhere the answer is not.
                repair_alternative=(
                    f"Supply a {field} with actual content, then retry. Leading and "
                    "trailing whitespace around that content is fine."
                ),
                # `details` is derived from the entry below by make_error (#416), so the
                # mirror it promises cannot drift from what this call site passes.
                invalid_arguments=[InvalidArgument(field=field, reason=reason)],
            ),
            meta=meta,
        )
    )


def _invalid_arguments_envelope(
    tool_name: str,
    *,
    param_names: set[str],
    property_schemas: dict,
    errors: list[Any],  # Pydantic ErrorDetails dicts (or test fixtures)
) -> dict | None:
    """Build an ``invalid_arguments`` error envelope from a Pydantic argument
    ValidationError, or return None when the errors are NOT request-argument failures.

    The None guard prevents misclassifying an unrelated ValidationError (e.g. an
    output-schema validation failure raised after the handler) as a bad-argument
    error: every reported error must reference a declared parameter or be an
    ``unexpected_keyword_argument`` (whose location is the unknown key itself)."""
    missing_types = {"missing", "missing_argument"}
    for err in errors:
        loc = err.get("loc") or ()
        is_extra = err.get("type") == "unexpected_keyword_argument"
        if not is_extra and not (loc and str(loc[0]) in param_names):
            return None

    total = len(errors)
    items: list[InvalidArgument] = []
    for err in errors[:_MAX_INVALID_ARGS]:
        loc: tuple[Any, ...] = tuple(err.get("loc") or ())
        field = _format_loc(loc)
        # The rejected value is never echoed (see InvalidArgument): a string/Literal param
        # accepts arbitrary input that could be a secret, and best-effort redaction can't
        # reliably catch a plain one. reason + allowed_values guide the fix (#136).
        # An indexed loc (`("include_schemas", 0)`) blames one element, so resolve the
        # element's domain rather than the container's (#373).
        indexed = len(loc) > 1 and isinstance(loc[1], int)
        allowed = (
            _enum_for_property(property_schemas.get(str(loc[0])), element=indexed) if loc else None
        )
        items.append(
            InvalidArgument(
                field=field,
                reason=str(err.get("msg", ""))[:_MAX_ARG_REASON_LEN],
                allowed_values=allowed,
            )
        )

    first = items[0]
    shown = f" (showing {len(items)} of {total})" if total > len(items) else ""
    message = f"{tool_name}: {total} invalid argument(s){shown}: {first.field} — {first.reason}"
    # Type-aware repair: name the dominant fix, then point at the authoritative schema.
    types = {err.get("type") for err in errors}
    hints: list[str] = []
    if "unexpected_keyword_argument" in types:
        hints.append("remove the unknown argument(s)")
    if types & missing_types:
        hints.append("provide the required argument(s)")
    if any(t == "literal_error" for t in types):
        hints.append("use one of the field's allowed_values")
    # Always lead with correcting the arguments: repair.tool now names the failing tool
    # (#184/N3) and the rejected values are never echoed (repair.arguments stays absent),
    # so the guidance must not read as "call the same tool again as-is". Append the
    # type-specific hints only when present, so an untyped failure (e.g. a wrong-type
    # value) doesn't render the self-referential "… first — correct the argument(s)."
    # (Copilot review).
    detail = f" — {'; '.join(hints)}" if hints else ""
    repair = (
        f"Correct the argument(s) first{detail}. "
        "Consult each tool's inputSchema (tools/list) or call kimi_capabilities "
        "for the parameters and accepted values, then retry."
    )
    d = config.defaults()
    # Report the called tool's real posture, not the server defaults, so meta.tier/
    # sandbox stay honest for a malformed propose-tier call (e.g. kimi_delegate) (#136).
    tier, sandbox = _TOOL_POSTURE.get(tool_name, (d.tier, d.sandbox))
    meta = _base_meta(
        workspace.server_cwd(),
        None,
        tier=tier,
        sandbox=sandbox,
        isolation=d.isolation,
        model=d.model,
        reasoning_effort=d.reasoning_effort,
        timeout_seconds=config.clamp_timeout(d.timeout_seconds),
    )
    return serialize_error(
        ErrorResult(
            error=make_error(
                "invalid_arguments",
                message[:300],
                repair_tool=tool_name,
                repair_alternative=repair,
                # `details` mirrors items[0], derived by make_error (#416) — this builder
                # used to construct the mirror by hand, which is how it could drift.
                invalid_arguments=items,
            ),
            meta=meta,
        )
    )


class _ArgumentValidationMiddleware(Middleware):
    """Re-emit a tool-argument ``ValidationError`` as the documented error envelope.

    FastMCP validates a call's arguments with Pydantic and raises a ``ValidationError``
    BEFORE the handler runs (an unknown/extra arg, a missing required arg, a wrong type,
    or an out-of-enum Literal value). Left alone, the caller gets ``isError: true`` with
    ``structured_content=None`` and raw validator prose — no symbolic ``code``,
    ``repair``, ``request_id``, or ``fingerprint`` — bypassing the result contract (#136).
    We catch it here at the call boundary and return the normal ``invalid_arguments``
    envelope with ``is_error=True`` set directly (no reliance on _SemanticErrorMiddleware).

    Only argument-validation failures are mapped: ``_invalid_arguments_envelope`` returns
    None for a ValidationError whose locations are not request arguments (e.g. an
    output-schema failure raised inside ``call_next``), and we re-raise that untouched.

    Since fastmcp 3.4.3, a bad call surfaces as ``fastmcp.exceptions.ValidationError``
    (not a Pydantic subclass) carrying only ``str(e)``; the structured errors survive on
    its ``__cause__``. We accept both shapes so the envelope holds across the whole
    supported fastmcp range, and re-raise anything whose cause is not a Pydantic error."""

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        try:
            return await call_next(context)
        except (ValidationError, FastMCPValidationError) as exc:
            cause = exc if isinstance(exc, ValidationError) else exc.__cause__
            if not isinstance(cause, ValidationError):
                raise
            name = context.message.name
            try:
                tool = await mcp.get_tool(name)
                params = tool.parameters if tool is not None else None
                props = params.get("properties", {}) if params else {}
            except Exception:
                # Can't introspect the tool's schema → can't safely classify; preserve
                # the original failure rather than guess.
                raise exc from None
            envelope = _invalid_arguments_envelope(
                name,
                param_names=set(props),
                property_schemas=props,
                errors=cause.errors(),
            )
            if envelope is None:
                raise
            return ToolResult(structured_content=envelope, is_error=True)


mcp.add_middleware(_ArgumentValidationMiddleware())

# Resource-read error envelope (#181/F9) ------------------------------------- #
# MCP numeric error code for "resource not found" — ERA-DEPENDENT since ADR 0005.
# 2025-11-25 and earlier use -32002 (see fastmcp.server.mixins.mcp_operations); the SDK
# exposes no named constant, so we name both here. 2026-07-28 renumbered it to -32602 and
# states that implementations of that revision MUST NOT emit -32002
# (modelcontextprotocol.io/specification/2026-07-28/basic/index § Error Codes). This server
# serves both eras, so a single constant would violate the spec on one of them.
# Read failures reuse the JSON-RPC standard INTERNAL_ERROR (-32603) on both eras.
_MCP_RESOURCE_NOT_FOUND = -32002
_MCP_RESOURCE_NOT_FOUND_MODERN = -32602
# Revisions that use the sessionless per-request envelope, and so the renumbered code.
# Read off the installed SDK rather than hardcoded, so a future modern revision is covered
# without an edit here; falls back to the known value if the SDK moves the constant.
try:
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    _MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = tuple(MODERN_PROTOCOL_VERSIONS)
except ImportError:  # pragma: no cover - defensive: SDK layout change
    _MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)


def _resource_not_found_code(context: Any) -> int:
    """The not-found code for the era this request negotiated.

    Fails SAFE to the legacy -32002: an unreadable protocol version means we cannot prove
    the request is on a modern connection, and emitting the legacy code to a modern client
    is a spec violation in the same direction the SDK itself still takes by default,
    whereas emitting -32602 to a legacy client would break code that matches -32002."""
    fastmcp_context = getattr(context, "fastmcp_context", None)
    request_context = getattr(fastmcp_context, "request_context", None)
    version = getattr(request_context, "protocol_version", None)
    if version in _MODERN_PROTOCOL_VERSIONS:
        return _MCP_RESOURCE_NOT_FOUND_MODERN
    return _MCP_RESOURCE_NOT_FOUND


class _ResourceErrorMiddleware(Middleware):
    """Carry the §6 error envelope in a resource-read failure's JSON-RPC ``error.data``.

    Unlike a tool call (whose failure is an ``ok: false`` envelope in structuredContent),
    a ``resources/read`` failure is a JSON-RPC error. FastMCP would return it with
    ``error.data: null`` — no symbolic ``code``, ``temporary``, or ``repair`` — so a
    resource read is the one surface that bypassed the unified contract (audit F9, #181).

    We intercept at the ``on_read_resource`` seam (mirroring ``_ArgumentValidationMiddleware``
    at the tool seam) and re-raise an ``McpError`` whose ``error.data`` is the serialized
    ``ErrorInfo`` shape. The mcp SDK's request handler does ``response = err.error`` for an
    ``McpError``, so the ``data`` we attach survives verbatim to the client.

    Exception routing is deliberate and ordered (per a cross-model design review):
    - ``NotFoundError``/``DisabledError`` (unknown or disabled URI) → ``resource_not_found``
      with the era's MCP numeric code (-32002 handshake, -32602 modern; see
      ``_resource_not_found_code``). FastMCP maps both to "resource not found" itself, so
      we match.
    - ``ResourceError`` (a resource function raised; the core wraps arbitrary handler
      exceptions into this) → ``internal_error`` with -32603.
    - Any ``McpError`` an inner layer already raised is re-raised untouched — never
      reclassified — so a deliberate protocol error keeps its own code/data.
    - Nothing else is caught: an unexpected ``Exception`` keeps FastMCP's existing handling,
      and cancellation (a ``BaseException``) propagates. The client-visible message is
      generic (no URI or exception text echoed — matching the redaction posture of #189)."""

    async def on_read_resource(self, context, call_next):  # type: ignore[no-untyped-def]
        # `context.message` is a `ReadResourceRequestParams` on a real request (verified
        # 2026-07-26: reading kimi://models yielded uri: kimi://models); the unit tests
        # that drive this middleware directly pass a bare stand-in context with no
        # `.message` at all, so both attribute hops are defensive, not just the inner one.
        request_message = getattr(context, "message", None)
        uri = str(getattr(request_message, "uri", "") or "") or None
        try:
            return await call_next(context)
        except (NotFoundError, DisabledError) as exc:
            raise self._envelope_error(
                "resource_not_found", _resource_not_found_code(context), "Resource not found.", uri
            ) from exc
        except ResourceError as exc:
            raise self._envelope_error(
                "internal_error", INTERNAL_ERROR, "Resource read failed.", uri
            ) from exc

    @staticmethod
    def _envelope_error(
        code: ErrorCode, mcp_code: int, message: str, resource_uri: str | None
    ) -> McpError:
        info = make_error(code, message)
        # The requested URI is client-supplied. It is echoed only after FastMCP has already
        # parsed it as a URI, and the human-readable `message` stays generic (no URI, no
        # exception text) — matching the redaction posture of #189.
        info.resource_uri = resource_uri
        info.request_id = uuid4().hex
        data = serialize_error_info(info)
        return McpError(code=mcp_code, message=message, data=data)


mcp.add_middleware(_ResourceErrorMiddleware())

# The propose orchestration lives in delegate.py; re-exported here for test access.
_diffstat = delegate._diffstat


# --------------------------------------------------------------------------- #
# Described param annotations (#93)
# --------------------------------------------------------------------------- #
# Each ambiguous param's `description` is defined once here and reused across every tool
# signature, so the advertised input schema carries the constraint/semantics that
# previously lived only in docstring prose — and the wording can never drift between
# tools. Descriptions only: no numeric/pattern constraints are added (a schema rule that
# disagreed with runtime validation would be worse than none), so accepted values are
# unchanged. timeout_seconds documents the clamp rather than enforcing ge/le, matching
# config.clamp_timeout()'s coerce-don't-reject behavior.
QuestionParam = Annotated[
    str,
    Field(
        description="The question or prompt to send Kimi (a different model) for a "
        "read-only answer. Must be non-blank: empty or whitespace-only is "
        "rejected before any model call."
    ),
]
TaskParam = Annotated[
    str,
    Field(
        description="The coding task for Kimi to implement inside a throwaway git "
        "worktree; the resulting diff is returned for review, not applied to your tree. "
        "Must be non-blank: empty or whitespace-only is rejected before any model call."
    ),
]
WorkspaceRootParam = Annotated[
    str | None,
    Field(
        description="Absolute path to the target repo root — pass it (or an MCP root) to "
        "target the intended repo; otherwise the call falls back to the server's own cwd "
        "and sets meta.workspace_warning."
    ),
]
TranscriptPathParam = Annotated[
    str,
    Field(
        description="Absolute path to the Claude Code session transcript (.jsonl) to hand "
        "off to Kimi. Must be an existing, non-empty .jsonl file under "
        "~/.claude/projects. Find the current session's transcript as the newest *.jsonl "
        "under ~/.claude/projects/<cwd-slug>/. If that is ambiguous — for example, more than "
        "one recent transcript could be the current session — ask the user which one "
        "to transfer."
    ),
]
ExtraContextParam = Annotated[
    str | None,
    # Compressed inline (#333, audit-2 F2); full caveats and bounds at kimi://params.
    Field(description=param_contracts.PARAMETER_CONTRACTS["extra_context"].summary),
]
ModelParam = Annotated[
    str | None,
    Field(
        description="Override the Kimi model slug for this call; defaults to the "
        "server/Kimi default when unset."
    ),
]
# Bounds on the reasoning-effort VALUE, enforced at the MCP boundary (like
# IdempotencyKeyParam's length bounds) and re-checked pre-spend on the RESOLVED value
# (_reasoning_effort_shape_error below), which the MOONBRIDGE_REASONING_EFFORT
# env default reaches without ever crossing the MCP boundary. config owns the bounds
# (config.reasoning_effort_shape_error) so the two enforcement points cannot drift.
_REASONING_EFFORT_MAX_LENGTH = config.REASONING_EFFORT_MAX_LENGTH
_REASONING_EFFORT_VALUE_PATTERN = config.REASONING_EFFORT_VALUE_PATTERN
ReasoningEffortParam = Annotated[
    str | None,
    Field(
        max_length=_REASONING_EFFORT_MAX_LENGTH,
        pattern=_REASONING_EFFORT_VALUE_PATTERN,
        # Compressed inline (#333); full rejection/bounds semantics at kimi://params.
        description=param_contracts.PARAMETER_CONTRACTS["reasoning_effort"].summary,
    ),
]
TimeoutSecondsParam = Annotated[
    int | None,
    Field(
        description="Per-call wall-clock timeout in seconds, clamped to 10..600 "
        "(out-of-range values are coerced, not rejected). Defaults to the server's "
        "configured timeout."
    ),
]
BaseParam = Annotated[
    str | None,
    Field(description="Base git ref for scope='branch'; the review covers base...HEAD."),
]
CommitParam = Annotated[
    str | None,
    Field(description="Commit SHA or ref to review for scope='commit'."),
]
PathsParam = Annotated[
    list[str] | None,
    Field(
        description="Repo-relative paths to narrow the review ('/' separators, no '..'); "
        "omit to review all changes in scope."
    ),
]
IsolationParam = Annotated[
    Isolation | None,
    Field(description=param_contracts.PARAMETER_CONTRACTS["isolation"].summary),
]
ScopeParam = Annotated[
    ReviewScope,
    Field(
        description="Which changes to review: 'working_tree' (tracked changes vs HEAD; "
        "untracked files follow the `untracked` policy, off by default), 'branch' "
        "(needs base), or 'commit' (needs commit)."
    ),
]
UntrackedParam = Annotated[
    Untracked,
    Field(
        description="How working_tree scope treats untracked files: 'explicit_only' "
        "(default) includes only those named in `paths`; 'include' reviews all "
        "non-ignored untracked files (SENDS their contents to your configured "
        "provider — opt-in egress); "
        "'exclude' includes none. Omitted ones are disclosed in `coverage`. Inert for "
        "branch/commit scopes."
    ),
]
DetailParam = Annotated[
    Detail,
    Field(
        description="Response verbosity: 'summary' (default) omits the raw model text; "
        "'full' includes it."
    ),
]
# kimi_capabilities-only override of DetailParam, and since #339 its own `Detail` value set
# too (`CapabilitiesDetail`) — the shared Literal stays two-valued so the other five
# DetailParam users are unaffected. Its own accurate description: DetailParam's shared "omits
# the raw model text" wording is wrong here — this tool makes no model call and has no raw
# model text to omit (F1 review finding).
CapabilitiesDetailParam = Annotated[
    CapabilitiesDetail,
    Field(
        description="What to return: 'summary' (default) returns each tool's name, cost, "
        "stability, and error_codes; the *_async tools also get async_lifecycle. 'full' adds "
        "use_when, returns, and the parameter lists (which tools/list already carries). "
        "'contracts' drops tool_details: fetch a schema, or recheck fingerprint, without "
        "re-paying for the inventory."
    ),
]
JobIdParam = Annotated[
    str,
    Field(
        description="The job_id from an *_async call or a sync call's meta.job_id; recover "
        "lost ids with kimi_job_list."
    ),
]
JobLimitParam = Annotated[
    int | None,
    Field(
        ge=1,
        le=1000,
        description="Maximum jobs to return, newest first (1-1000). Omit (or pass null) to "
        "return every retained job that matches — the default; it caps nothing, and does not "
        "override `status`. Only an explicit limit truncates: then the response sets "
        "truncated=true and the extra rows are dropped.",
    ),
]
JobStatusFilterParam = Annotated[
    JobState | None,
    Field(
        description="Return only jobs in this lifecycle state ('running', 'done', 'failed', "
        "'cancelled', 'timeout'); omit for all states."
    ),
]
IdempotencyKeyParam = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=200,
        # Compressed inline (#333); full lifecycle semantics at kimi://params.
        description=param_contracts.PARAMETER_CONTRACTS["idempotency_key"].summary,
    ),
]
# Named so a test can derive the advertised token set with get_args and assert it against
# what kimi_capabilities actually returns — widening the Literal without wiring the payload
# would otherwise go unnoticed (#370).
IncludeSchemasToken = Literal[
    "error-envelope",
    "result-meta",
    "capabilities-result",
    "status-result",
    "parameter-contracts",
]
IncludeSchemasParam = Annotated[
    list[IncludeSchemasToken] | None,
    Field(
        description="Opt-in tool-reachable fallback for resource-blind clients: also embed "
        "the full 'error-envelope', 'result-meta', 'capabilities-result', and/or "
        "'status-result' schema, and/or the 'parameter-contracts' document (a contract "
        "doc, not a JSON Schema, sourced from the kimi://params resource body), in the "
        "response — the default payload omits them and points at the kimi:// resources "
        "instead.",
    ),
]
# kimi_delegate_dry_run reuses these params but never calls Kimi or returns a diff, so
# it needs preview-accurate wording rather than the active-delegate descriptions above.
TaskDryRunParam = Annotated[
    str,
    Field(
        description="The coding task you want Kimi to implement via a real "
        "kimi_delegate call; this dry run only previews the seeded baseline and prompt "
        "size — it does NOT call Kimi or return a diff. Must be non-blank: empty or "
        "whitespace-only is rejected here too, not previewed."
    ),
]
ModelDryRunParam = Annotated[
    str | None,
    Field(
        description="The Kimi model slug the previewed paid call would use; defaults "
        "to the server default (MOONBRIDGE_MODEL) when unset, so the preview "
        "mirrors the paid call's resolution. This dry run does not call Kimi or "
        "validate the model."
    ),
]
ReasoningEffortDryRunParam = Annotated[
    str | None,
    Field(
        max_length=_REASONING_EFFORT_MAX_LENGTH,
        pattern=_REASONING_EFFORT_VALUE_PATTERN,
        description="The reasoning effort the previewed paid call would send (as a "
        "`model_reasoning_effort` config override); defaults to the server default "
        "(MOONBRIDGE_REASONING_EFFORT) when unset, so the preview mirrors the "
        "paid call's resolution. This dry run does not call Kimi or validate the "
        "value beyond the paid params' shape bounds (no control or surrogate "
        f"characters, ≤{_REASONING_EFFORT_MAX_LENGTH} chars).",
    ),
]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
async def _roots_from_ctx(ctx: Context | None) -> tuple[list[str], RootsSource]:  # noqa: ARG001
    """Always ``([], "unsupported")``: this server can no longer ask a client for its roots.

    FastMCP 4 / MCP SDK v2 removed ``ctx.list_roots()`` from ``Context`` on EVERY protocol
    era, not just the sessionless one — verified, ``hasattr(ctx, "list_roots")`` is False
    under both ``mode="legacy"`` and ``mode="auto"``. ``roots/list`` was a server-to-client
    request, and the modern protocol has no server-to-client request direction at all, so
    the pushed form has nowhere to go (ADR 0005).

    ``workspace_root`` is the durable, first-choice path on every tool that resolves a
    working directory, and was already the primary mechanism here — ADR 0004 recorded roots
    as an advisory fallback and scheduled its removal for exactly this trigger ("drop it
    only when/if the underlying MCP SDK removes roots support"). Tool-argument scoping is
    also the migration target the 2026-07-28 spec names for the deprecated roots feature.

    ``ctx`` is retained so the six call sites and the tuple contract are unchanged; it is
    deliberately unused. This is a function rather than an inlined constant so the reason
    stays attached to the behavior, and so restoring a probe (should the guard pattern ever
    be worth its extra round trip on a paid call) is a one-place change.
    """
    return [], "unsupported"


def _dry_run_effective_model(requested: str | None) -> str | None:
    """The model override the previewed paid call would actually SEND.

    Mirrors the paid path: --model is help-gated (build_exec_command drops it when the
    installed CLI does not advertise it, and reconcile_dropped_model then nulls
    meta.model), so a preview that echoed the requested slug on such a CLI would claim
    an override the paid run silently drops. The probe is process-cached
    (HELP_CACHE_TTL_SECONDS) and fails open, like the paid path."""
    if requested is None:
        return None
    if not preflight.is_supported(kimi.cli_contract.MODEL_FLAG, preflight.flag_support()):
        return None
    return requested


# #342: the prompt-size half of the deadline-advisory predicate. An EXACT constant, not
# a fraction of max_input_bytes — grounded in the CHANGELOG's own datapoints for this
# deadline: the 180->300s timeout raise (#338/#341) recovered the mid-tier consult/review
# runs observed exceeding the old 180s cap, but the destructive >~420s cliff remains
# unremoved, so a call already well past the historical mid-tier size is worth flagging
# before it's sent, not after it's terminated. 100_000 bytes sits below max_input_bytes'
# 200_000-byte default, comfortably inside the sizes that were observed running long.
_DEADLINE_ADVISORY_PROMPT_BYTES = 100_000
_DEADLINE_ADVISORY_HIGH_EFFORTS = frozenset({"high", "xhigh"})


def _deadline_advisory(
    would_call_model: bool,
    prompt_bytes: int,
    effort: str | None,
    timeout_seconds: int,
    async_tool: str,
) -> str | None:
    """Advise when a previewed paid call may exceed its synchronous deadline.

    Gates entirely on `would_call_model` (round-2 correction, load-bearing): a preview
    whose paid call would short-circuit and run no model — e.g. kimi_dry_run's empty
    diff — cannot overrun a deadline it never approaches, so an effort/size-only
    advisory there would be factually false. When would_call_model is True, advise iff
    the prompt exceeds `_DEADLINE_ADVISORY_PROMPT_BYTES` or `effort` is exactly "high"
    or "xhigh" (case-sensitive, no normalization — effort is a free-form string per
    config.py, and an unrecognized value is deliberately NOT treated as high). A hint
    only: every other preview field is unaffected.

    `async_tool` is the REGISTERED name of the previewed PAID tool's own `_async`
    counterpart (e.g. "kimi_review_changes_async" for a kimi_dry_run preview,
    "kimi_delegate_async" for a kimi_delegate_dry_run preview) — never this dry-run
    tool's own name, which has no `_async` variant (there is no kimi_dry_run_async).
    Named verbatim in the returned text so an agent can call it directly (round-3
    correction, verified by Kimi review: an unrouted generic phrase like "this tool's
    _async variant" would point at a nonexistent tool when read from the dry-run
    caller's own perspective)."""
    if not would_call_model:
        return None
    triggers_on_size = prompt_bytes > _DEADLINE_ADVISORY_PROMPT_BYTES
    triggers_on_effort = effort in _DEADLINE_ADVISORY_HIGH_EFFORTS
    if not (triggers_on_size or triggers_on_effort):
        return None
    return (
        f"This previewed call's prompt size or reasoning effort may exceed the "
        f"{timeout_seconds}s synchronous deadline; prefer {async_tool} (the async "
        "counterpart of the previewed call), which is polled instead of terminated "
        "if the run outlasts the deadline."
    )


def _resolve_isolation(value: str | None) -> tuple[str | None, ErrorInfo | None]:
    isolation = value or config.defaults().isolation
    if isolation not in config.VALID_ISOLATIONS:
        return None, make_error(
            "unsupported_isolation",
            f"unsupported isolation: {isolation}",
            details=ErrorDetail(field="isolation", allowed_values=list(config.VALID_ISOLATIONS)),
        )
    return isolation, None


def _resolve_detail(value: str | None) -> tuple[str | None, ErrorInfo | None]:
    """Validate the `detail` param (#56). Returns (detail, None) or (None, error)."""
    detail = value or "summary"
    valid = get_args(Detail)
    if detail not in valid:
        return None, make_error(
            "unsupported_detail",
            f"unsupported detail: {detail}",
            details=ErrorDetail(field="detail", allowed_values=list(valid)),
        )
    return detail, None


def _workspace_error_result(
    error_code: str, error_detail: str | None, roots: list[str], meta: Meta
) -> dict:
    """Build a workspace-resolution error envelope. For `workspace_outside_roots`, attach
    the client-supplied MCP roots as `candidate_roots` so an agent can pick a valid
    `workspace_root` without parsing prose — never arbitrary local paths (#95)."""
    candidate_roots = list(roots) if error_code == "workspace_outside_roots" and roots else None
    return serialize_error(
        ErrorResult(
            error=make_error(
                cast("ErrorCode", error_code),
                error_detail or "invalid workspace",
                details=ErrorDetail(field="workspace_root"),
                candidate_roots=candidate_roots,
            ),
            meta=meta,
        )
    )


def _placeholder_error(meta: Meta) -> dict | None:
    placeholders = config.placeholder_env_vars()
    if not placeholders:
        return None
    return serialize_error(
        ErrorResult(
            error=make_error(
                "unexpanded_env_placeholder",
                f"Unexpanded ${{...}} env placeholders: {', '.join(placeholders)}.",
                repair_alternative=config.ENV_PLACEHOLDER_REPAIR,
            ),
            meta=meta,
        )
    )


def _extra_args_error(meta: Meta) -> dict | None:
    """Preflight the MOONBRIDGE_EXTRA_ARGS knob before any spend (#231).

    Mirrors _placeholder_error: if the knob is set but fails to parse/allowlist,
    return a structured extra_args_rejected envelope so the caller never pays for a
    call kimi would reject. The `error` string is value-free (built in config), so no
    secret `-c` value can leak here."""
    extra = config.extra_args()
    if not extra.configured or extra.valid:
        return None
    return serialize_error(
        ErrorResult(
            error=make_error(
                "extra_args_rejected",
                f"{config.EXTRA_ARGS_ENV} is invalid: {extra.error}.",
            ),
            meta=meta,
        )
    )


def _reasoning_effort_unsupported_error(
    effort: str | None, model: str | None, meta: Meta
) -> dict | None:
    """Pre-spend guard on whether the resolved model ACCEPTS the resolved effort.

    This is the only validation that happens at all. Verified on kimi-code 0.35.0: kimi
    SILENTLY IGNORES an unrecognized KIMI_MODEL_THINKING_EFFORT — it exits 0 and answers at
    the model's default. So there is no backend rejection to classify, and without this
    check the run would succeed while `meta.reasoning_effort` claimed an effort that was
    never applied. Refusing costs nothing (zero spend) and keeps the envelope honest.

    Only fires when the alias's supported set is actually known. `supported_efforts_for`
    returns None for "cannot tell", which must never be read as "nothing is allowed".
    """
    if effort is None:
        return None
    supported = supported_efforts_for(model)
    if not supported or effort in supported:
        return None
    meta.reasoning_effort = None
    return serialize_error(
        ErrorResult(
            error=make_error(
                "invalid_reasoning_effort",
                "the requested reasoning_effort is not one this model declares.",
                details=ErrorDetail(field="reasoning_effort"),
                repair_alternative=(
                    f"Pass one of: {', '.join(supported)} — or omit reasoning_effort to use "
                    "the model's default. kimi_models lists each alias's declared efforts. "
                    "Refused locally: kimi ignores an unrecognized effort instead of "
                    "rejecting it, so the run would silently use the default while the "
                    "result claimed otherwise."
                ),
            ),
            meta=meta,
        )
    )


def _reasoning_effort_shape_error(
    effort: str | None, meta: Meta, *, from_config: bool
) -> dict | None:
    """Pre-spend guard on the RESOLVED reasoning effort (#309, Kimi re-review).

    The MCP boundary already enforces the shape bounds on the per-call parameter, but
    the MOONBRIDGE_REASONING_EFFORT env default (and a direct in-process call)
    never crosses that boundary — without this check an argv-hostile operator default
    would reach Popen (a NUL raises ValueError; an argv-scale value surfaces as a
    misleading kimi_not_found). Zero spend: the run is refused before any subprocess.

    `invalid_reasoning_effort` is shared with the backend-rejection path, whose table
    repair steers to kimi_models (correct there). That is wrong here: the value never
    reached the backend, so the machine repair is provenance-specific and carries no tool
    (#332). `from_config` is True when the invalid value is the resolved env default
    (repair `correct_config` — fix the operator config) and False when it is an explicit
    per-call argument that reached this guard via a direct in-process call (repair
    `correct_arguments` — over MCP such a value is invalid_arguments at the boundary)."""
    if effort is None:
        return None
    reason = config.reasoning_effort_shape_error(effort)
    if reason is None:
        return None
    # Never echo the invalid raw value back through meta: a surrogate-bearing value
    # would break serializing this very envelope (the same failure the guard exists
    # to prevent), and control-character values have no place in a result either.
    meta.reasoning_effort = None
    return serialize_error(
        ErrorResult(
            error=make_error(
                "invalid_reasoning_effort",
                f"the requested reasoning_effort {reason}.",
                details=ErrorDetail(field="reasoning_effort"),
                repair_next_step="correct_config" if from_config else "correct_arguments",
                repair_tool=None,  # clear the table's kimi_models: the backend never saw it
                repair_alternative=(
                    "Fix the per-call reasoning_effort or the "
                    f"{config.ENV_PREFIX}REASONING_EFFORT default (≤"
                    f"{config.REASONING_EFFORT_MAX_LENGTH} chars, no control or "
                    "surrogate characters), or omit the override. The value never "
                    "reached kimi (zero spend)."
                ),
            ),
            meta=meta,
        )
    )


def _base_meta(
    cwd: str,
    source: str | None,
    *,
    tier: str,
    sandbox: str,
    isolation: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int,
    **extra: Any,
) -> Meta:
    return Meta(
        cwd=cwd,
        workspace_source=source,
        workspace_warning=workspace_warning_for(source, cwd),
        tier=cast("Tier", tier),
        sandbox=cast("Sandbox", sandbox),
        isolation=cast("Isolation", isolation),
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        elapsed_ms=0,
        **extra,
    )


def _internal_error_result(
    tool_name: str, exc: BaseException, *, tier: str, sandbox: str, elapsed_ms: int = 0
) -> dict:
    """Best-effort `internal_error` envelope for an unexpected tool failure.

    Used by the tool boundary so a bug or unforeseen exception still returns the
    documented result envelope (not an opaque transport error) and a caller can
    branch on `internal_error` — which these tools already advertise."""
    d = config.defaults()
    meta = _base_meta(
        workspace.server_cwd(),
        None,
        tier=tier,
        sandbox=sandbox,
        isolation=d.isolation,
        model=d.model,
        reasoning_effort=d.reasoning_effort,
        timeout_seconds=config.clamp_timeout(d.timeout_seconds),
    )
    meta.elapsed_ms = elapsed_ms
    return serialize_error(
        ErrorResult(
            error=make_error(
                "internal_error",
                f"{tool_name} failed unexpectedly: {redaction.exc_summary(exc)}"[:300],
                repair_alternative=(
                    "Server-side error; retry. If it persists, run kimi_status and inspect "
                    "the server's stderr log (set MOONBRIDGE_LOG_LEVEL=DEBUG for detail)."
                ),
            ),
            meta=meta,
        )
    )


def _guard(
    *, tier: str = "consult", sandbox: str = "read-only"
) -> Callable[[Callable[..., Awaitable[dict]]], Callable[..., Awaitable[dict]]]:
    """Wrap an async tool so an unexpected exception becomes a structured
    `internal_error` envelope (logged with a traceback) instead of escaping the
    handler. Cancellation is a `BaseException`, so it propagates untouched —
    `except Exception` never catches it — preserving MCP cancel semantics (#39)."""

    def decorator(fn: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        name = getattr(fn, "__name__", "tool")
        # Record this tool's fixed posture so an argument-validation error can report it.
        _TOOL_POSTURE[name] = (tier, sandbox)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict:
            start = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                obs.get_logger("moonbridge.server").error(
                    "tool %s raised %s after %dms",
                    name,
                    type(exc).__name__,
                    elapsed_ms,
                    exc_info=True,
                )
                return _internal_error_result(
                    name, exc, tier=tier, sandbox=sandbox, elapsed_ms=elapsed_ms
                )

        return wrapper

    return decorator


# F9: per-tool stability, in one place. Previously this lived as nine inline
# `stability="experimental"` literals inside the kimi_capabilities body, which meant the
# tier could not be mirrored anywhere else without hand-copying the list. Both surfaces —
# ToolCapability.stability and each tool's namespaced _meta — now read this map, and a test
# asserts they agree.
_STABILITY_META_KEY = "dev.bconnelly.moonbridge/stability"
_SERVER_STABILITY = "alpha"

# Tools more experimental than the server-wide tier. Anything absent inherits _SERVER_STABILITY.
_TOOL_STABILITY: dict[str, ToolStability] = {
    "kimi_consult_async": "experimental",
    "kimi_review_changes_async": "experimental",
    "kimi_delegate_async": "experimental",
    "kimi_job_status": "experimental",
    "kimi_job_result": "experimental",
    "kimi_job_consume_result": "experimental",
    "kimi_job_cancel": "experimental",
    "kimi_job_list": "experimental",
}


def _tool_meta(name: str) -> dict[str, str]:
    return {_STABILITY_META_KEY: _TOOL_STABILITY.get(name, _SERVER_STABILITY)}


# --------------------------------------------------------------------------- #
# Free tools
# --------------------------------------------------------------------------- #
@mcp.tool(
    annotations=_FREE_READ,
    output_schema=STATUS_SCHEMA,
    title="Check Kimi readiness (free)",
    meta=_tool_meta("kimi_status"),
)
def kimi_status() -> dict:
    """Check that the `kimi` CLI is installed, authenticated, and a supported
    version, and report the resolved defaults. Free — no model call. Run it before
    your first paid call in a session to confirm setup, and again whenever a run
    fails with a setup error.
    Spend: do not plan spend around `rate_limit` — it always reports `unavailable`, and that
    is not a failure. kimi exposes no quota-read channel and its provider is user-configured,
    so there is nothing authoritative to report. The only quota signal this server can give
    you is the `kimi_rate_limited` error on a call that was actually throttled; it carries
    `retry_after_ms`."""
    d = config.defaults()
    version = kimi.kimi_version()
    found = version is not None
    authenticated, auth_detail = kimi.login_status() if found else (None, None)
    version_supported = config.version_supported(version)
    fs = preflight.flag_support(force=True)
    missing = preflight.missing_expected_flags(fs)

    version_warning = None
    if version_supported is False:
        version_warning = (
            f"kimi version {version} is outside the tested set; tools may still "
            "work but are unverified for this version."
        )
    flags_warning = None
    if missing:
        flags_warning = (
            f"`kimi --help` did not list expected flags: {', '.join(missing)}. "
            "The CLI contract may have drifted; an update to moonbridge may be needed."
        )

    ready = bool(found and authenticated)
    if not found:
        readiness_detail = "kimi CLI not found on PATH."
    elif authenticated is None:
        readiness_detail = "Could not determine kimi auth status."
    elif not authenticated:
        readiness_detail = "kimi is not authenticated; run `kimi login`."
    else:
        readiness_detail = "Ready: kimi is installed and authenticated."

    timeout = config.clamp_timeout(d.timeout_seconds)
    extra = config.extra_args()
    return StatusResult(
        kimi_found=found,
        kimi_version=version,
        kimi_authenticated=authenticated,
        auth_detail=auth_detail,
        version_supported=version_supported,
        version_warning=version_warning,
        flags_warning=flags_warning,
        ready=ready,
        readiness_detail=readiness_detail,
        extra_args_configured=extra.configured,
        extra_args_count=extra.option_count,
        extra_args_valid=extra.valid,
        raw_defaults=RawDefaults(
            tier=d.tier,
            sandbox=d.sandbox,
            isolation=d.isolation,
            model=d.model,
            reasoning_effort=d.reasoning_effort,
            timeout_seconds=d.timeout_seconds,
        ),
        resolved_defaults=ResolvedDefaults(
            tier=cast("Tier", d.tier),
            sandbox=cast("Sandbox", d.sandbox),
            isolation=cast("Isolation", d.isolation),
            model=d.model,
            reasoning_effort=d.reasoning_effort,
            timeout_seconds=timeout,
            timeout_bounds=[config.MIN_TIMEOUT_SECONDS, config.MAX_TIMEOUT_SECONDS],
        ),
        # rate_limit is left to its default ('unavailable'): kimi exposes no quota-read
        # channel (Codex's app-server `account/rateLimits/read` has no equivalent) and the
        # provider is user-configured, so there is nothing authoritative to report.
        caveat="The active tools send your content to your configured Kimi provider via the "
        "kimi CLI: kimi_consult sends your question and context; kimi_review_changes sends "
        "the secret-redacted diff plus your raw extra_context; kimi_delegate sends your task "
        "and the worktree files Kimi reads. Every tier runs in a throwaway git worktree, and "
        "kimi_consult/kimi_review_changes additionally run under a read-only agent profile "
        "with no shell or write tools. "
        f"{cli_contract.READ_ONLY_CONFIDENTIALITY_LIMIT} "
        f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
        "A selected skill's body can reach the model even if your prompt never mentions it. "
        "Secret redaction is best-effort and does not cover your inputs. Treat results "
        "as claims to verify.",
    ).model_dump(mode="json")


# Error codes each tool may return, advertised per-tool in kimi_capabilities so
# agents can branch/recover without triggering the error first. Advisory, not a
# closed contract. Composed from shared groups to keep the lists from drifting;
# every code is asserted to be a valid ErrorCode by tests/test_packaging.py.
_WORKSPACE_ERRORS: tuple[ErrorCode, ...] = ("invalid_workspace_root", "workspace_outside_roots")
_RUNTIME_ERRORS: tuple[ErrorCode, ...] = (
    "kimi_not_found",
    "kimi_auth_required",
    "unexpanded_env_placeholder",
    "timeout",
    "nonzero_exit",
    "invalid_json",
    "schema_violation",
    "cli_contract_changed",
    # Reachable on every Kimi-running tool, from two paths (#309, #332): the backend
    # rejected the sent effort, OR the local pre-spend guard refused a hostile resolved
    # value (the per-call reasoning_effort or the MOONBRIDGE_REASONING_EFFORT default)
    # before any subprocess. The two paths carry different machine repairs.
    "invalid_reasoning_effort",
    "extra_args_rejected",
    "kimi_rate_limited",
    "internal_error",
)
_GITDIFF_ERROR_CODES: tuple[ErrorCode, ...] = (
    # invalid_scope is intentionally omitted: `scope` is a Literal param, so FastMCP
    # rejects an out-of-enum value before the handler can reach the gitdiff guard that
    # produces it. Over MCP that rejection now surfaces as invalid_arguments (#136), not
    # this code, so invalid_scope stays unadvertised. See _SCHEMA_GATED_CODES.
    "invalid_base",
    "invalid_commit",
    "invalid_paths",
    "not_a_git_repo",
    "git_unavailable",
)
# Advertised only on the six spend-committing tools that accept idempotency_key.
_IDEMPOTENCY_ERRORS: tuple[ErrorCode, ...] = (
    "idempotency_conflict",
    "idempotency_result_unavailable",
    "idempotency_in_progress",
)
_JOB_READ_ERRORS: tuple[ErrorCode, ...] = (*_WORKSPACE_ERRORS, "job_not_found", "internal_error")
# Advertised only where a FINISHED stored envelope is validated for return: the three
# sync tools (whose await/reattach path shares _finished_job_envelope) and the two
# job-result fetch tools. Async starters and status/list/cancel never validate one,
# so this code on them would be a false contract.
_FINISHED_RESULT_ERRORS: tuple[ErrorCode, ...] = ("job_result_incompatible",)
_JOB_RESULT_ERRORS: tuple[ErrorCode, ...] = (
    *_JOB_READ_ERRORS,
    # unsupported_detail omitted: `detail` is a Literal param; over MCP a bad value
    # surfaces as invalid_arguments (#136), not this code. See _SCHEMA_GATED_CODES.
    "job_running",
    "job_cancelled",
    "job_timeout",
    "job_failed",
    *_FINISHED_RESULT_ERRORS,
)


def _err_codes(*groups: tuple[ErrorCode, ...]) -> list[ErrorCode]:
    """Flatten error-code groups, dropping duplicates while preserving order. Each
    literal is checked against ErrorCode by the type checker via the group types."""
    seen: dict[ErrorCode, None] = {}
    for group in groups:
        for code in group:
            seen[code] = None
    return list(seen)


# Error codes whose only production path is an out-of-enum value on a Literal-typed
# tool param (isolation -> unsupported_isolation, detail -> unsupported_detail,
# scope -> invalid_scope). FastMCP rejects such input BEFORE the handler runs, and that
# rejection is now re-emitted as the `invalid_arguments` envelope at the call boundary
# (#136) — so a real MCP call_tool caller receives invalid_arguments, never these
# per-param codes. They remain MCP-unreachable by their own symbolic code; advertising
# them would be a false contract (#92). They stay in ErrorCode and the in-handler
# _resolve_*/gitdiff guards (which still fire on direct Python calls, as defense-in-depth)
# but are never advertised per-tool. The capabilities injector strips them defensively so
# a future re-add to a group can't leak one back into the advertised surface;
# tests/test_server.py pins the invariant.
_SCHEMA_GATED_CODES: frozenset[ErrorCode] = frozenset(
    {"unsupported_isolation", "unsupported_detail", "invalid_scope"}
)


_TOOL_ERROR_CODES: dict[str, list[ErrorCode]] = {
    # Note: unsupported_isolation/unsupported_detail (and invalid_scope, via
    # _GITDIFF_ERROR_CODES) are deliberately absent — those params are Literal-typed, so
    # FastMCP rejects out-of-enum input before the handler runs, making the codes
    # MCP-unreachable (#92). _SCHEMA_GATED_CODES also strips them defensively below.
    "kimi_consult": _err_codes(
        _WORKSPACE_ERRORS,
        ("input_too_large", "context_too_large"),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
        _FINISHED_RESULT_ERRORS,
    ),
    "kimi_consult_async": _err_codes(
        _WORKSPACE_ERRORS,
        ("input_too_large", "context_too_large"),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
    ),
    "kimi_review_changes": _err_codes(
        _WORKSPACE_ERRORS,
        _GITDIFF_ERROR_CODES,
        ("input_too_large", "context_too_large"),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
        _FINISHED_RESULT_ERRORS,
    ),
    "kimi_review_changes_async": _err_codes(
        _WORKSPACE_ERRORS,
        _GITDIFF_ERROR_CODES,
        ("input_too_large", "context_too_large"),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
    ),
    "kimi_delegate": _err_codes(
        _WORKSPACE_ERRORS,
        (
            "input_too_large",
            "not_a_git_repo",
            "worktree_error",
        ),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
        _FINISHED_RESULT_ERRORS,
    ),
    "kimi_delegate_async": _err_codes(
        _WORKSPACE_ERRORS,
        ("input_too_large", "not_a_git_repo", "worktree_error"),
        _RUNTIME_ERRORS,
        _IDEMPOTENCY_ERRORS,
    ),
    "kimi_models": [],
    "kimi_status": [],
    "kimi_capabilities": [],
    # Both dry runs advertise invalid_reasoning_effort even though they never call
    # Kimi: the pre-spend shape guard on the RESOLVED effort runs there too, so an
    # invalid MOONBRIDGE_REASONING_EFFORT default surfaces the code with no
    # subprocess involved.
    "kimi_dry_run": _err_codes(
        _WORKSPACE_ERRORS,
        _GITDIFF_ERROR_CODES,
        (
            "input_too_large",
            "unexpanded_env_placeholder",
            "extra_args_rejected",
            "invalid_reasoning_effort",
            "internal_error",
        ),
    ),
    "kimi_delegate_dry_run": _err_codes(
        _WORKSPACE_ERRORS,
        (
            "unexpanded_env_placeholder",
            "extra_args_rejected",
            "invalid_reasoning_effort",
            "input_too_large",
            "not_a_git_repo",
            "worktree_error",
            "internal_error",
        ),
    ),
    "kimi_job_status": _err_codes(_JOB_READ_ERRORS),
    "kimi_job_result": _err_codes(_JOB_RESULT_ERRORS),
    "kimi_job_consume_result": _err_codes(_JOB_RESULT_ERRORS),
    "kimi_job_cancel": _err_codes(_JOB_READ_ERRORS),
    "kimi_job_list": _err_codes(_WORKSPACE_ERRORS, ("internal_error",)),
}

# The *_async tools run via this server's custom job lifecycle (no native MCP
# tasks/progress). Advertised structurally on each so a client can discover the exact
# poll/result/consume/cancel/list tools and JobStatus fields, and detect the absence of
# native tasks/progress, without parsing description prose (#94). The tool names and
# JobStatus field names are the single source of truth here.
_ASYNC_TOOLS: frozenset[str] = frozenset(
    {"kimi_consult_async", "kimi_review_changes_async", "kimi_delegate_async"}
)
_ASYNC_LIFECYCLE = AsyncLifecycle(
    poll_tool="kimi_job_status",
    result_tool="kimi_job_result",
    consume_tool="kimi_job_consume_result",
    cancel_tool="kimi_job_cancel",
    list_tool="kimi_job_list",
    status_field="status",
    result_ready_field="result_available",
    result_ok_field="result_ok",
    poll_after_field="poll_after_ms",
    activity_support="kimi_events",
    event_count_field="events_seen",
    last_event_field="last_event_at",
    event_age_field="event_age_ms",
)
# F1: the tool_details fields with no `tools/list` equivalent. Everything else in a
# ToolCapability restates the preloaded catalog (use_when ~= description, returns ~=
# outputSchema, *_params ~= inputSchema), so detail="summary" drops it. `stability` is
# serialized even when None — it is null for default-tier tools, and stripping it would
# re-create the gap F9 closes. `_normalize_tool_details` (not this tuple) is what puts the
# key there, for every mode that carries an inventory.
_CAPABILITY_SUMMARY_FIELDS = ("name", "cost", "stability", "error_codes", "async_lifecycle")
# Declared field order, so a re-added key lands where the model puts it rather than at the end.
_TOOL_CAPABILITY_FIELDS = tuple(ToolCapability.model_fields)


def _normalize_tool_details(entry: dict) -> dict:
    """Re-add the `stability` key `exclude_none` drops for a default-tier tool (#399).

    `stability` is None wherever a tool inherits the server-wide tier, so `exclude_none`
    strips it — but every detail mode that carries an inventory promises the key is
    present, with null meaning inheritance. Forcing it here, before any mode branches, is
    what keeps `full` from dropping a key `summary` carries: the modes disagreed for as
    long as only the summary branch did it.
    """
    entry.setdefault("stability", None)
    return {k: entry[k] for k in _TOOL_CAPABILITY_FIELDS if k in entry}


@mcp.tool(
    annotations=_FREE_READ,
    output_schema=CAPABILITIES_SCHEMA,
    title="List server capabilities (free)",
    meta=_tool_meta("kimi_capabilities"),
)
def kimi_capabilities(
    include_schemas: IncludeSchemasParam = None,
    detail: CapabilitiesDetailParam = "summary",
) -> dict:
    """List this server's tools, tiers, and the result fingerprint.
    Free — no model call. Clients can cache by the fingerprint.

    `detail="summary"` (default) returns each tool's name, cost, stability, and
    error_codes — the facts `tools/list` does not already carry — plus async_lifecycle,
    but only for the `*_async` tools. `detail="full"` adds
    use_when/returns/required_params/key_optional_params, restating what you already hold.
    `detail="contracts"` omits tool_details.

    Pass include_schemas to also embed the full 'error-envelope', 'result-meta',
    'capabilities-result', and/or 'status-result' schema, and/or the 'parameter-contracts'
    document (a contract doc, not a JSON Schema) — a tool-reachable fallback to the
    kimi:// resources for resource-blind clients. It works in any detail mode."""
    caps = CapabilitiesResult(
        name="moonbridge",
        version=__version__,
        transport="stdio",
        stability=_SERVER_STABILITY,
        active_tools=[
            "kimi_consult",
            "kimi_consult_async",
            "kimi_review_changes",
            "kimi_review_changes_async",
            "kimi_delegate",
            "kimi_delegate_async",
        ],
        free_tools=[
            "kimi_models",
            "kimi_status",
            "kimi_dry_run",
            "kimi_delegate_dry_run",
            "kimi_capabilities",
            "kimi_job_status",
            "kimi_job_result",
            "kimi_job_consume_result",
            "kimi_job_cancel",
            "kimi_job_list",
        ],
        tool_details=[
            ToolCapability(
                name="kimi_consult",
                cost="active",
                use_when="You want a read-only second opinion or answer from Kimi "
                "(a different model) on a question, design, or an ad-hoc diff you paste "
                "inline; use kimi_review_changes when the diff comes from git. Prefer "
                "kimi_consult_async for a high-reasoning-effort or broad repo-grounded "
                "consult that can exceed the synchronous deadline.",
                required_params=["question"],
                key_optional_params=[
                    "workspace_root",
                    "extra_context",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "detail",
                    "idempotency_key",
                ],
                returns="A result envelope with summary, optional findings, and meta. "
                "detail='summary' (default) omits raw_response.text; detail='full' includes it. "
                "Egress: sends question+extra_context (raw, unredacted) to your configured "
                "provider; Kimi "
                "always runs with a resolved working dir (workspace_root, else the server cwd — "
                "MCP roots are unavailable) and may read and send files from it. "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
                "A selected skill's body can reach the model. "
                "Recorded as a terminal job (meta.job_id) recoverable via kimi_job_result "
                "after a dropped connection.",
            ),
            ToolCapability(
                name="kimi_consult_async",
                cost="active",
                stability=_TOOL_STABILITY.get("kimi_consult_async"),
                use_when="You want a read-only second opinion from Kimi for a "
                "high-reasoning-effort or broad repo-grounded consult that can exceed the "
                "synchronous deadline, so you want a job_id immediately instead of blocking; "
                "async counterpart to kimi_consult.",
                required_params=["question"],
                key_optional_params=[
                    "workspace_root",
                    "extra_context",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "idempotency_key",
                ],
                returns="A job handle (job_id, status, deadline, ttl). Poll with "
                "kimi_job_status; read the consult envelope with kimi_job_result. "
                "Egress: same as kimi_consult — sends question+extra_context (raw) to "
                "your configured provider, plus files Kimi reads from its resolved working dir "
                "(workspace_root, else the server cwd — MCP roots are unavailable). "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
                "A selected skill's body can reach the model.",
            ),
            ToolCapability(
                name="kimi_review_changes",
                cost="active",
                use_when="You want Kimi to review your git changes (working_tree, "
                "branch, or commit) and return structured findings. Prefer "
                "kimi_review_changes_async for a multi-file or whole-branch review that can "
                "exceed the synchronous deadline.",
                key_optional_params=[
                    "scope",
                    "base",
                    "commit",
                    "paths",
                    "workspace_root",
                    "extra_context",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "detail",
                    "idempotency_key",
                ],
                returns="A result envelope with verdict, findings, and a context summary. "
                "detail='summary' (default) omits raw_response.text; detail='full' includes it. "
                "Egress: sends the bounded, secret-redacted diff plus your raw (unredacted) "
                "extra_context to your configured provider; Kimi may also read other repo files. "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
                "A selected skill's body can reach the model. "
                "Recorded as a terminal job (meta.job_id) recoverable via kimi_job_result "
                "after a dropped connection.",
            ),
            ToolCapability(
                name="kimi_review_changes_async",
                cost="active",
                stability=_TOOL_STABILITY.get("kimi_review_changes_async"),
                use_when="You want Kimi to review your git changes (working_tree, branch, "
                "or commit) for a multi-file or whole-branch review that can exceed the "
                "synchronous deadline, so you want a job_id immediately instead of blocking; "
                "async counterpart to kimi_review_changes.",
                key_optional_params=[
                    "scope",
                    "base",
                    "commit",
                    "paths",
                    "workspace_root",
                    "extra_context",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "idempotency_key",
                ],
                returns="A job handle (job_id, status, deadline, ttl). Poll with "
                "kimi_job_status; read the review envelope with kimi_job_result. "
                "Egress: same as kimi_review_changes — sends the secret-redacted diff "
                "plus your raw extra_context to your configured provider; Kimi may also read "
                "other repo "
                "files. "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} "
                "A selected skill's body can reach the model.",
            ),
            ToolCapability(
                name="kimi_delegate",
                cost="active",
                use_when="You want Kimi to implement a coding task and return a "
                "reviewable diff WITHOUT touching your working tree (it works in a "
                "throwaway git worktree). Prefer kimi_delegate_async for a substantial or "
                "multi-file task that can exceed the synchronous deadline.",
                required_params=["task"],
                key_optional_params=[
                    "workspace_root",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "detail",
                    "idempotency_key",
                ],
                returns="A result envelope whose `diff` holds Kimi's proposed, "
                "unapplied changes plus a summary. detail='summary' (default) omits "
                "raw_response.text; detail='full' includes it. "
                "Egress: sends your task (raw) to your configured provider and lets Kimi read "
                "tracked "
                "files in the throwaway worktree and send their content. "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} For delegate, that workspace is the "
                "worktree; scrubbing it doesn't exclude those entries. "
                "Recorded as a terminal job (meta.job_id) recoverable via kimi_job_result "
                "after a dropped connection.",
            ),
            ToolCapability(
                name="kimi_delegate_async",
                cost="active",
                stability=_TOOL_STABILITY.get("kimi_delegate_async"),
                use_when="You want Kimi to implement a coding task as a reviewable diff "
                "(NOT applied to your working tree) for a substantial or multi-file task that "
                "can exceed the synchronous deadline, so you want a job_id immediately instead "
                "of blocking; async counterpart to kimi_delegate.",
                required_params=["task"],
                key_optional_params=[
                    "workspace_root",
                    "model",
                    "reasoning_effort",
                    "isolation",
                    "idempotency_key",
                ],
                returns="A job handle (job_id, status, deadline, ttl). Poll with "
                "kimi_job_status; read with kimi_job_result. "
                "Egress: same as kimi_delegate — sends your task (raw) to your configured "
                "provider plus "
                "the worktree files Kimi reads. "
                f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} For delegate, that workspace is the "
                "worktree; scrubbing it doesn't exclude those entries.",
            ),
            ToolCapability(
                name="kimi_job_status",
                cost="free",
                stability=_TOOL_STABILITY.get("kimi_job_status"),
                use_when="To poll a background job's state without fetching the result. "
                "Jobs may originate from an async call or a sync consult/review/delegate's "
                "meta.job_id.",
                required_params=["job_id"],
                key_optional_params=["workspace_root"],
                returns="Status, elapsed time, expiry, result_available, and "
                "result_ok (a done job's outcome: true=success, false=stored error, "
                "null=not-yet-known/unclassifiable — triage a failure without a fetch).",
            ),
            ToolCapability(
                name="kimi_job_result",
                cost="free",
                stability=_TOOL_STABILITY.get("kimi_job_result"),
                use_when="When kimi_job_status reports result_available=true. Works for "
                "async and sync-originated jobs alike.",
                required_params=["job_id"],
                key_optional_params=["workspace_root", "detail"],
                returns="The finished job's envelope (delegate diff, consult answer, or "
                "review verdict — branch on `tool`), with meta.job_id set. detail='summary' "
                "(default) omits raw_response.text; detail='full' includes it.",
            ),
            ToolCapability(
                name="kimi_job_consume_result",
                cost="free",
                stability=_TOOL_STABILITY.get("kimi_job_consume_result"),
                use_when="To fetch a finished job's result and delete the stored record. "
                "Works for async and sync-originated jobs alike.",
                required_params=["job_id"],
                key_optional_params=["workspace_root", "detail"],
                returns="The same envelope as kimi_job_result; removes completed state "
                "only once the stored result has been read back intact and validated "
                "(an unreadable stored result is kept for kimi_job_result).",
            ),
            ToolCapability(
                name="kimi_job_cancel",
                cost="free",
                stability=_TOOL_STABILITY.get("kimi_job_cancel"),
                use_when="To stop a running background job.",
                required_params=["job_id"],
                key_optional_params=["workspace_root"],
                returns="The job's status after cancellation.",
            ),
            ToolCapability(
                name="kimi_job_list",
                cost="free",
                stability=_TOOL_STABILITY.get("kimi_job_list"),
                use_when="To recover job_ids or inspect known jobs for a workspace, "
                "including sync-originated ones.",
                key_optional_params=["workspace_root", "limit", "status"],
                returns="Compact job summaries, newest first, each with result_ok "
                "(true=success, false=stored error, null=running/unclassifiable) so a "
                "stored failure is triageable without a per-job fetch. Every retained job "
                "matching `status` unless the caller also passes `limit`, which caps the "
                "rows and sets truncated=true — a cap, not a page: the extra rows are "
                "dropped and there is no cursor. Not permanent "
                "storage: "
                "terminal records expire after the TTL, and a per-workspace soft cap "
                "(default 50) evicts the oldest terminal records as new jobs start. "
                "Running jobs are never evicted, so the list can transiently exceed the "
                "cap; older finished jobs drop off. Includes sync-originated records; "
                "the cap/TTL eviction covers both.",
            ),
            ToolCapability(
                name="kimi_status",
                cost="free",
                use_when="Before active calls, to confirm kimi is installed and authenticated.",
                returns="Readiness, version, auth, and resolved defaults.",
            ),
            ToolCapability(
                name="kimi_dry_run",
                cost="free",
                use_when="Before kimi_review_changes, to preview scope/diff size/"
                "redactions without spending.",
                key_optional_params=[
                    "scope",
                    "base",
                    "commit",
                    "paths",
                    "workspace_root",
                    "extra_context",
                    "model",
                    "reasoning_effort",
                    "isolation",
                ],
                returns="Scope, context summary, prompt size, redactions, and the "
                "effective model/reasoning_effort overrides the paid call would send "
                "(unvalidated).",
            ),
            ToolCapability(
                name="kimi_delegate_dry_run",
                cost="free",
                use_when="Before kimi_delegate/kimi_delegate_async, to preview the "
                "seeded baseline, prompt size, and workspace without spending.",
                required_params=["task"],
                key_optional_params=["workspace_root", "model", "reasoning_effort", "isolation"],
                returns="The HEAD baseline (commit, tracked/uncommitted/untracked "
                "counts and size), prompt size, the effective model/reasoning_effort "
                "overrides the paid call would send (unvalidated), and the resolved "
                "workspace — no worktree created.",
            ),
            ToolCapability(
                name="kimi_capabilities",
                cost="free",
                use_when="To discover the tool inventory, tiers, and result fingerprint "
                "(cache by it).",
                returns="This inventory: tools, tiers, sandboxes, scope, negative_scope, "
                "prerequisites, deprecation_policy, per-tool error_codes, async_lifecycle "
                "(on the *_async tools), and fingerprint. A top-level `stability` names the "
                "server lifecycle stage; a per-tool `stability` is an advisory maturity "
                "override, always present, and null there means it inherits the "
                "server-wide value.",
            ),
            ToolCapability(
                name="kimi_models",
                cost="free",
                use_when="To discover valid `model` slugs — and each model's advertised "
                "reasoning-effort set — before passing `model` or `reasoning_effort` to "
                "a Kimi call; also available at the kimi://models resource. Advisory — "
                "kimi/the backend validate the real values at run time.",
                returns="An advisory model catalog: source (cache|static|none), models "
                "(slug + display_name + default_reasoning_effort/"
                "supported_reasoning_efforts when the cache advertises them), and the "
                "cache's fetched_at/client_version when read from Kimi's on-disk "
                "cache. Not fingerprint-stable — do not cache it by the capabilities "
                "fingerprint.",
            ),
        ],
        tiers=list(config.VALID_TIERS),
        sandboxes=list(kimi.cli_contract.VALID_SANDBOXES),
        scope=[
            "Get a second opinion or answer from Kimi (read-only).",
            "Review git changes and return structured findings.",
            "Delegate a coding task and get a reviewable worktree diff (not applied).",
            "Run a long consult, review, or delegate in the background and poll it via job tools.",
        ],
        negative_scope=[
            "Does not apply edits to your working tree (delegate returns a diff).",
            "Does not sandbox Kimi: the kimi CLI has no sandbox and no approvals, so a "
            "delegated task runs shell commands with your own privileges and can reach "
            "the network.",
            "Does not keep your content on the machine: consult, review, and delegate "
            "(and their *_async variants) each send caller content to your configured "
            "provider via the "
            "kimi CLI — consult sends question+extra_context (plus files Kimi reads "
            "from its resolved working dir: workspace_root, else the server cwd — MCP "
            "roots are unavailable); review sends the bounded, secret-redacted diff "
            "plus your raw extra_context; delegate sends the task and lets Kimi read "
            "tracked files in the throwaway worktree. "
            f"{cli_contract.SKILLS_DISCOVERY_FACT_FULL} For delegate, that workspace is the "
            "throwaway worktree; scrubbing it doesn't exclude those entries. A "
            "selected skill's body can reach the model even if your prompt never "
            "mentions it.",
            "NETWORK IS NOT BLOCKED: kimi has no sandbox, so a delegated task CAN reach "
            "the network — it may push, fetch, install dependencies, or call out. Scope "
            "the task accordingly; do not rely on network isolation that does not exist. "
            "The Kimi model call itself also sends your task and repo context to your "
            "configured provider.",
            "Does not guarantee secrets stay local: secret redaction is best-effort and "
            "covers the gathered diff and Kimi's returned output — NOT your supplied "
            "inputs (question/task/extra_context), and not secrets Kimi reads from "
            "files itself during a run.",
        ],
        prerequisites=["kimi CLI on PATH", "authenticated via `kimi login`"],
        deprecation_policy="Pre-1.0: minor versions may change the agent-visible "
        "surface; the fingerprint changes when they do.",
        protocol_revision="2026-07-28",
    )
    # Inject per-tool error codes from the single source of truth; KeyError here
    # means a newly advertised tool is missing from _TOOL_ERROR_CODES. Strip any
    # schema-gated code defensively so a Literal-param rejection code can never be
    # advertised as an MCP-returnable envelope (#92). Every tool can receive
    # invalid_arguments at the call boundary (#136), so it is advertised universally.
    for cap in caps.tool_details:
        codes = [c for c in _TOOL_ERROR_CODES[cap.name] if c not in _SCHEMA_GATED_CODES]
        if "invalid_arguments" not in codes:
            codes.append("invalid_arguments")
        cap.error_codes = codes
        if cap.name in _ASYNC_TOOLS:
            cap.async_lifecycle = _ASYNC_LIFECYCLE
    if include_schemas:
        # Opt-in only (#179): embed the requested full contracts so a resource-blind client
        # can reach them from tools/list alone. De-duplicated and order-stable.
        available = {
            "error-envelope": ERROR_ENVELOPE_SCHEMA,
            "result-meta": RESULT_META_SCHEMA,
            "capabilities-result": CAPABILITIES_RESULT_SCHEMA,
            "status-result": STATUS_RESULT_SCHEMA,
            # A contract document (not a JSON Schema): the full kimi://params body, so a
            # resource-blind client can still reach the compressed params' full semantics (#333).
            "parameter-contracts": param_contracts.resource_body(),
        }
        # No `if k in available` guard: on an MCP call FastMCP rejects an off-enum token as
        # invalid_arguments before this runs, so a missing key there can only mean the
        # advertised Literal and this dict have drifted — a server bug that must fail loudly
        # rather than silently omit the schema the client asked for (#370). A direct Python
        # call bypasses that validation and gets a raw KeyError for an off-enum token; that
        # violates the annotated Literal's domain, and this module is not a public Python API.
        caps.schemas = {k: available[k] for k in dict.fromkeys(include_schemas)}
    # exclude_none so optional per-tool fields are omitted entirely when unset (rather
    # than emitting noisy nulls): only the *_async tools carry `async_lifecycle`, and
    # use_when/returns are full-only. `stability` is the one field that must survive that
    # strip, so _normalize_tool_details puts it back — once, before any mode branches.
    payload = caps.model_dump(mode="json", exclude_none=True)
    payload["tool_details"] = [_normalize_tool_details(e) for e in payload["tool_details"]]
    if detail == "contracts":
        # #339: shed the inventory and return. `tool_details` is already non-required in
        # both published schemas (default_factory=list keeps it out of `required`), so
        # dropping it needs no schema change and no new success branch — verified by a test
        # that validates this payload against both. Several other fields are non-required
        # too, so schema validation alone would NOT catch one going missing; the rule stops
        # at this key because it is the heavy one (~66% of the bare payload), and a
        # set-equality test — not the schema — is what pins that nothing else disappears.
        del payload["tool_details"]
        return payload
    if detail == "summary":
        payload["tool_details"] = [
            # A plain projection: `stability` needs no special case here now that
            # _normalize_tool_details has already forced it on every entry (#399).
            {k: entry[k] for k in _CAPABILITY_SUMMARY_FIELDS if k in entry}
            for entry in payload["tool_details"]
        ]
    return payload


def _model_catalog_payload() -> dict:
    """Single source for the tool and resource so their payloads cannot drift."""
    return read_model_catalog().model_dump(mode="json", exclude_none=True)


_TRIAGE_META_KEY = "dev.bconnelly.moonbridge/triage"


def _static_triage(payload: dict) -> dict[str, dict]:
    """Triage metadata for a resource whose body is a fixed, generated schema.

    An agent deciding whether a resource body is worth the context wants `size` and
    `lastModified` — kimi://error-envelope reads back at ~18 KB with no advance signal.
    Neither native field is reachable on fastmcp 3.4.4 (its Resource model has no `size`;
    the installed SDK's Annotations has no `lastModified`), so the metadata rides the
    namespaced `_meta` key instead. Computed from the payload at registration, so it
    cannot go stale against the body."""
    # .encode() makes this a true byte count rather than a character count that happens
    # to agree with it: json.dumps defaults to ensure_ascii=True, which escapes every
    # non-ASCII character to \uXXXX, so today len(str) == len(str.encode()) for every
    # payload here. That equality is a property of the current dump settings, not of the
    # field's name — encoding explicitly keeps size_bytes true by construction if a dump
    # ever used ensure_ascii=False or the body picked up non-ASCII content.
    return {_TRIAGE_META_KEY: {"size_bytes": len(json.dumps(payload).encode())}}


@mcp.tool(
    annotations=_FREE_READ,
    output_schema=MODEL_CATALOG_SCHEMA,
    title="List Kimi models (free)",
    meta=_tool_meta("kimi_models"),
)
def kimi_models() -> dict:
    """List Kimi model slugs you can pass as `model`, with each model's advertised
    reasoning-effort set for `reasoning_effort`. Free — no model call.

    Advisory discovery only: read from Kimi's on-disk cache when present, else a
    bundled fallback (`source` says which; the fallback carries no effort data).
    The kimi CLI validates the real slug and the backend validates the real effort, so
    an unlisted value may still work and a listed one may be unavailable to your
    account. Same payload as the kimi://models resource. Not fingerprint-stable — do
    not cache it by the capabilities fingerprint."""
    return _model_catalog_payload()


@mcp.resource(
    "kimi://models",
    name="kimi-models",
    title="Kimi model catalog",
    mime_type="application/json",
    meta={
        _TRIAGE_META_KEY: {
            # This body is a refreshed cache, not a fixed schema, so a declared size would
            # go stale. The server advertises resources.subscribe=false, so there is no
            # update notification — freshness is observable only in the body itself.
            "volatile": True,
            "freshness_via": "the payload's `fetched_at` field (also on kimi_models)",
        }
    },
)
def kimi_models_resource() -> dict:
    """Advisory Kimi model catalog (same payload as the kimi_models tool)."""
    return _model_catalog_payload()


@mcp.resource(
    "kimi://error-envelope",
    name="kimi-error-envelope",
    title="Kimi error envelope schema",
    mime_type="application/schema+json",
    meta=_static_triage(ERROR_ENVELOPE_SCHEMA),
)
def error_envelope_resource() -> dict:
    """The canonical full error envelope (ErrorResult). The per-tool outputSchemas carry
    only a compact opaque error branch; this is the discoverable full shape."""
    return ERROR_ENVELOPE_SCHEMA


@mcp.resource(
    "kimi://result-meta",
    name="kimi-result-meta",
    title="Kimi result metadata schema",
    mime_type="application/schema+json",
    meta=_static_triage(RESULT_META_SCHEMA),
)
def result_meta_resource() -> dict:
    """The canonical full result-metadata schema (Meta). Every success envelope carries an
    opaque `meta` pointer instead of inlining this per tool; this is the full shape (F1)."""
    return RESULT_META_SCHEMA


@mcp.resource(
    "kimi://capabilities-result",
    name="kimi-capabilities-result",
    title="Kimi capabilities result schema",
    mime_type="application/schema+json",
    meta=_static_triage(CAPABILITIES_RESULT_SCHEMA),
)
def capabilities_result_resource() -> dict:
    """The canonical full kimi_capabilities result schema. The tool's outputSchema opaques
    `tool_details` and points here for the full shape (#242)."""
    return CAPABILITIES_RESULT_SCHEMA


@mcp.resource(
    "kimi://status-result",
    name="kimi-status-result",
    title="Kimi status result schema",
    mime_type="application/schema+json",
    meta=_static_triage(STATUS_RESULT_SCHEMA),
)
def status_result_resource() -> dict:
    """The canonical full kimi_status result schema. The tool's outputSchema opaques
    `rate_limit`/`raw_defaults`/`resolved_defaults` and points here for the full shape (#242)."""
    return STATUS_RESULT_SCHEMA


@mcp.resource(
    "kimi://params",
    name="kimi-params",
    title="Kimi tool parameter contracts",
    mime_type="application/json",
    meta=_static_triage(param_contracts.resource_body()),
)
def params_resource() -> dict:
    """Full semantics for parameters whose tools/list description is a compressed summary
    (#333). MCP inlines each parameter description into every tool's inputSchema, so the
    lengthy lifecycle/validation prose is shipped once here instead of repeated on the
    wire; the inline summary keeps the first-call selection, safety, and spend-critical
    facts and points here for the complete lifecycle and recovery semantics."""
    return param_contracts.resource_body()


# --------------------------------------------------------------------------- #
# Active tools
# --------------------------------------------------------------------------- #
# --- shared per-pair input preparation (#204) --------------------------------
# Each active tool and its `_async` twin share the same preparation: resolve
# isolation (and, for the sync twin, `detail`), the workspace, build `meta`, run the
# placeholder + input-size pre-flights, and assemble the run `spec`. These helpers
# hold that once so the sync/async pair cannot drift. They return either
# `(meta, cwd, spec, detail_v)` on success or a ready error envelope (a dict), so the
# caller branches on `isinstance(prep, dict)`. `timeout_seconds` is the sync per-call
# timeout or the async job deadline — the only field that legitimately differs between
# a pair's two specs, and it is the sole hash-affecting difference (parity is pinned in
# tests/test_tool_pair_parity.py). Hash-compatible behavior is required: the spec keys
# feed the idempotency arg hash, so changing a HASHED key would invalidate live dedup
# entries. A key listed in _ARG_HASH_EXCLUDE is pure provenance and may be added freely
# (that is how `roots_source` reaches the worker without moving any hash — #393).
#
# `include_detail` (not a nullable `detail`) selects the sync twin: `_resolve_detail`
# maps None to "summary", so None cannot double as an "async, skip detail" sentinel.


async def _prepare_consult(
    *,
    question: str,
    tool_name: str,
    workspace_root: str | None,
    extra_context: str | None,
    model: str | None,
    reasoning_effort: str | None,
    isolation: str | None,
    timeout_seconds: int,
    ctx: Context | None,
    defaults: config.Defaults,
    include_detail: bool,
    detail: str | None = None,
) -> tuple[Meta, str, dict, str | None] | dict:
    """Shared preparation for kimi_consult / kimi_consult_async."""
    d = defaults
    # Exact-None precedence: an explicit "" is the caller's value, passed through for
    # the backend to judge — never silently coalesced to the server default (#309).
    effort = reasoning_effort if reasoning_effort is not None else d.reasoning_effort
    # `isolation or d.isolation` keeps the default-isolation fallback on this one
    # snapshot; _resolve_isolation(None) would otherwise read config.defaults() again.
    isolation_v, iso_err = _resolve_isolation(isolation or d.isolation)
    cwd_guess = workspace.server_cwd()
    if iso_err is not None:
        meta = _base_meta(
            cwd_guess,
            None,
            tier="consult",
            sandbox="read-only",
            isolation=d.isolation,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=timeout_seconds,
        )
        return serialize_error(ErrorResult(error=iso_err, meta=meta))
    assert isolation_v is not None

    detail_v: str | None = None
    if include_detail:
        detail_v, detail_err = _resolve_detail(detail)
        if detail_err is not None:
            meta = _base_meta(
                cwd_guess,
                None,
                tier="consult",
                sandbox="read-only",
                isolation=isolation_v,
                model=model or d.model,
                reasoning_effort=effort,
                timeout_seconds=timeout_seconds,
            )
            return serialize_error(ErrorResult(error=detail_err, meta=meta))
        assert detail_v is not None

    roots, roots_source = await _roots_from_ctx(ctx)
    # On a resolve error `wres.path`/`wres.source` are None, so `cwd_guess`/None — the
    # same meta the sync twin used to build separately for its error path.
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    meta = _base_meta(
        cwd,
        wres.source,
        tier="consult",
        sandbox="read-only",
        isolation=isolation_v,
        model=model or d.model,
        reasoning_effort=effort,
        timeout_seconds=timeout_seconds,
        roots_source=roots_source,
    )
    if wres.error_code is not None:
        return _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)

    placeholder = _placeholder_error(meta)
    if placeholder is not None:
        return placeholder
    extra_args_err = _extra_args_error(meta)
    if extra_args_err is not None:
        return extra_args_err
    effort_err = _reasoning_effort_shape_error(
        effort, meta, from_config=reasoning_effort is None
    ) or _reasoning_effort_unsupported_error(effort, model or d.model, meta)
    if effort_err is not None:
        return effort_err

    limit = config.max_input_bytes()
    combined = (question or "") + (extra_context or "")
    combined_bytes = len(combined.encode("utf-8"))
    if combined_bytes > limit:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "input_too_large",
                    f"question + extra_context exceeds {limit} bytes.",
                    details=_combined_input_detail(extra_context),
                    limit_bytes=limit,
                    actual_bytes=combined_bytes,
                ),
                meta=meta,
            )
        )
    blank_err = _blank_input_error(question, "question", tool_name, meta)
    if blank_err is not None:
        return blank_err

    spec = {
        "kind": "kimi_consult",
        "question": question,
        "extra_context": extra_context or "",
        "cwd": cwd,
        "workspace_source": wres.source,
        "tier": "consult",
        "sandbox": "read-only",
        "isolation": isolation_v,
        "model": model or d.model,
        "timeout_seconds": timeout_seconds,
        # consult runs in a throwaway worktree like every other tier, because kimi has no
        # read-only sandbox — so it needs a git budget of its own.
        "git_timeout": config.git_timeout_seconds(),
        # Provenance, not an input: written unconditionally (unlike the two conditional
        # keys below) because it is hash-excluded, so its presence cannot invalidate a
        # pre-#393 dedup entry. The worker needs it to reach a delivered envelope (#393).
        "roots_source": roots_source,
    }
    # Written only when an effort override applies: an absent key keeps a no-effort
    # spec byte-identical to the pre-#309 shape, so existing idempotency arg hashes
    # (and their live dedup entries) survive the upgrade.
    if effort is not None:
        spec["reasoning_effort"] = effort
    return meta, cwd, spec, detail_v


async def _prepare_review(
    *,
    workspace_root: str | None,
    scope: str,
    base: str | None,
    commit: str | None,
    paths: list[str] | None,
    untracked: str = "explicit_only",
    extra_context: str | None,
    model: str | None,
    reasoning_effort: str | None,
    isolation: str | None,
    timeout_seconds: int,
    ctx: Context | None,
    defaults: config.Defaults,
    include_detail: bool,
    detail: str | None = None,
) -> tuple[Meta, str, dict, str | None] | dict:
    """Shared preparation for kimi_review_changes / kimi_review_changes_async.

    No input_too_large pre-check: the diff is gathered in the worker, which enforces
    max_bytes (and bounds extra_context)."""
    d = defaults
    # See _prepare_consult: exact-None precedence for the effort override.
    effort = reasoning_effort if reasoning_effort is not None else d.reasoning_effort
    # See _prepare_consult: fall back to this snapshot's isolation, not a fresh read.
    isolation_v, iso_err = _resolve_isolation(isolation or d.isolation)
    cwd_guess = workspace.server_cwd()
    if iso_err is not None:
        meta = _base_meta(
            cwd_guess,
            None,
            tier="consult",
            sandbox="read-only",
            isolation=d.isolation,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=timeout_seconds,
        )
        return serialize_error(ErrorResult(error=iso_err, meta=meta))
    assert isolation_v is not None

    detail_v: str | None = None
    if include_detail:
        detail_v, detail_err = _resolve_detail(detail)
        if detail_err is not None:
            meta = _base_meta(
                cwd_guess,
                None,
                tier="consult",
                sandbox="read-only",
                isolation=isolation_v,
                model=model or d.model,
                reasoning_effort=effort,
                timeout_seconds=timeout_seconds,
            )
            return serialize_error(ErrorResult(error=detail_err, meta=meta))
        assert detail_v is not None

    roots, roots_source = await _roots_from_ctx(ctx)
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    meta = _base_meta(
        cwd,
        wres.source,
        tier="consult",
        sandbox="read-only",
        isolation=isolation_v,
        model=model or d.model,
        reasoning_effort=effort,
        timeout_seconds=timeout_seconds,
        scope=scope,
        base=base,
        commit=commit,
        paths=paths,
        roots_source=roots_source,
    )
    if wres.error_code is not None:
        return _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)

    placeholder = _placeholder_error(meta)
    if placeholder is not None:
        return placeholder
    extra_args_err = _extra_args_error(meta)
    if extra_args_err is not None:
        return extra_args_err
    effort_err = _reasoning_effort_shape_error(
        effort, meta, from_config=reasoning_effort is None
    ) or _reasoning_effort_unsupported_error(effort, model or d.model, meta)
    if effort_err is not None:
        return effort_err

    spec = {
        "kind": "kimi_review_changes",
        "cwd": cwd,
        "workspace_source": wres.source,
        "tier": "consult",
        "sandbox": "read-only",
        "isolation": isolation_v,
        "model": model or d.model,
        "timeout_seconds": timeout_seconds,
        "scope": scope,
        "base": base,
        "commit": commit,
        "paths": paths,
        "extra_context": extra_context or "",
        "git_timeout": config.git_timeout_seconds(),
        "max_bytes": config.max_input_bytes(),
        # See _prepare_consult: hash-excluded provenance, written unconditionally (#393).
        "roots_source": roots_source,
    }
    # See _prepare_consult: written only when set, preserving pre-#309 arg hashes.
    if effort is not None:
        spec["reasoning_effort"] = effort
    # Likewise omit the default `untracked` so existing idempotency hashes and pre-#319
    # worker specs (which lack the key) keep replaying; the worker reads the default.
    if untracked != "explicit_only":
        spec["untracked"] = untracked
    return meta, cwd, spec, detail_v


async def _prepare_delegate(
    *,
    task: str,
    tool_name: str,
    workspace_root: str | None,
    model: str | None,
    reasoning_effort: str | None,
    isolation: str | None,
    timeout_seconds: int,
    ctx: Context | None,
    defaults: config.Defaults,
    include_detail: bool,
    detail: str | None = None,
) -> tuple[Meta, str, dict, str | None] | dict:
    """Shared preparation for kimi_delegate / kimi_delegate_async.

    Distinct pre-flight order from consult/review: the task-size check precedes `detail`
    resolution, and a synchronous git preflight (`ensure_repo_with_head`) fails fast —
    no spend, no record — if this is not a git repo with a commit to base on."""
    d = defaults
    # See _prepare_consult: exact-None precedence for the effort override.
    effort = reasoning_effort if reasoning_effort is not None else d.reasoning_effort
    # See _prepare_consult: fall back to this snapshot's isolation, not a fresh read.
    isolation_v, iso_err = _resolve_isolation(isolation or d.isolation)
    cwd_guess = workspace.server_cwd()
    if iso_err is not None:
        meta = _base_meta(
            cwd_guess,
            None,
            tier="propose",
            sandbox="workspace-write",
            isolation=d.isolation,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=timeout_seconds,
        )
        return serialize_error(ErrorResult(error=iso_err, meta=meta))
    assert isolation_v is not None

    roots, roots_source = await _roots_from_ctx(ctx)
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    meta = _base_meta(
        cwd,
        wres.source,
        tier="propose",
        sandbox="workspace-write",
        isolation=isolation_v,
        model=model or d.model,
        reasoning_effort=effort,
        timeout_seconds=timeout_seconds,
        roots_source=roots_source,
    )
    if wres.error_code is not None:
        return _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)

    placeholder = _placeholder_error(meta)
    if placeholder is not None:
        return placeholder
    extra_args_err = _extra_args_error(meta)
    if extra_args_err is not None:
        return extra_args_err
    effort_err = _reasoning_effort_shape_error(
        effort, meta, from_config=reasoning_effort is None
    ) or _reasoning_effort_unsupported_error(effort, model or d.model, meta)
    if effort_err is not None:
        return effort_err

    limit = config.max_input_bytes()
    task_bytes = len((task or "").encode("utf-8"))
    if task_bytes > limit:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "input_too_large",
                    f"task exceeds {limit} bytes.",
                    details=ErrorDetail(field="task"),
                    limit_bytes=limit,
                    actual_bytes=task_bytes,
                ),
                meta=meta,
            )
        )
    blank_err = _blank_input_error(task, "task", tool_name, meta)
    if blank_err is not None:
        return blank_err

    detail_v: str | None = None
    if include_detail:
        detail_v, detail_err = _resolve_detail(detail)
        if detail_err is not None:
            return serialize_error(ErrorResult(error=detail_err, meta=meta))
        assert detail_v is not None

    git_timeout = config.git_timeout_seconds()
    try:
        worktree.ensure_repo_with_head(cwd, timeout=git_timeout)
    except worktree.NotAGitRepoError as exc:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "not_a_git_repo",
                    str(exc),
                    details=ErrorDetail(field="workspace_root"),
                ),
                meta=meta,
            )
        )
    except (worktree.NoCommitsError, worktree.WorktreeError) as exc:
        return serialize_error(
            ErrorResult(
                error=make_error("worktree_error", str(exc)[:300]),
                meta=meta,
            )
        )

    spec = {
        "kind": "kimi_delegate",
        "task": task,
        "cwd": cwd,
        "workspace_source": wres.source,
        "tier": "propose",
        "sandbox": "workspace-write",
        "isolation": isolation_v,
        "model": model or d.model,
        "timeout_seconds": timeout_seconds,
        "git_timeout": git_timeout,
        "max_diff_bytes": config.max_delegate_diff_bytes(),
        # See _prepare_consult: hash-excluded provenance, written unconditionally (#393).
        "roots_source": roots_source,
    }
    # See _prepare_consult: written only when set, preserving pre-#309 arg hashes.
    if effort is not None:
        spec["reasoning_effort"] = effort
    return meta, cwd, spec, detail_v


# _ACTIVE_ASYNC (not read-only): the sync tool now creates an observable job record
# via the detached worker, so it can't advertise readOnlyHint (issue #138).
@mcp.tool(
    annotations=_ACTIVE_ASYNC,
    output_schema=CONSULT_RESULT_SCHEMA,
    title="Consult Kimi (paid)",
    meta=_tool_meta("kimi_consult"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_consult(
    question: QuestionParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    extra_context: ExtraContextParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    timeout_seconds: TimeoutSecondsParam = None,
    detail: DetailParam = "summary",
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Ask Kimi (a different model) for a read-only second opinion or answer.

    PAID — this spends Kimi quota on every new call; there is no dry-run preview for a
    consult, so run kimi_status (free) first to confirm the CLI is installed and
    authenticated.

    Runs `kimi -p` under a generated read-only agent profile — Kimi holds no shell and
    no write tool, so it never edits files. A STATIC review, not a verify mode: without
    those tools it cannot run a test/build/lint pass to confirm its claims —
    treat findings as unvalidated claims you verify yourself. Pass `workspace_root`
    (absolute) for a repo-grounded question; omit it for pure Q&A. Returns a result
    envelope.

    Data egress: this sends your `question` and `extra_context` to your configured
    Kimi provider via the
    kimi CLI. Kimi always runs with a resolved working directory (`workspace_root`, else the
    server's cwd as a fallback — MCP roots are unavailable), so it may read files there and
    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace). Skill
    names and descriptions are exposed to the model up front, so that content can be sent
    even if your prompt never mentions it. The isolation setting does not suppress any of
    it: kimi's built-in skills always load, and AGENTS.md is read regardless.

    Your inputs are sent raw and unredacted. Secret redaction is best-effort and covers the
    gathered diff and Kimi's returned output — not what you type, and not the files Kimi
    reads for itself.

    Progress & recovery: blocks up to the resolved deadline (`timeout_seconds`, clamped
    10-600s; when omitted, the server-configured value, built-in default 300s). If that deadline
    expires the run is terminated and its partial output is not recoverable or resumable, so for a
    high-`reasoning_effort` or broad repo-grounded consult that may exceed it, prefer
    `kimi_consult_async` (a background job, built-in default 1800s deadline; poll
    `kimi_job_status`). Coarse `notifications/progress` streams while it blocks when your client
    requests it; some MCP clients background a long call before the deadline, so
    `timeout_seconds` bounds the run, not necessarily the inline wait — either way the detached run
    (`meta.job_id`) is recoverable via `kimi_job_list`→`kimi_job_status`→`kimi_job_result`."""
    d = config.defaults()
    timeout = config.clamp_timeout(
        timeout_seconds if timeout_seconds is not None else d.timeout_seconds
    )
    prep = await _prepare_consult(
        question=question,
        tool_name="kimi_consult",
        workspace_root=workspace_root,
        extra_context=extra_context,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=timeout,
        ctx=ctx,
        defaults=d,
        include_detail=True,
        detail=detail,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, detail_v = prep
    assert detail_v is not None  # include_detail=True always resolves a detail
    return await _run_sync(
        meta,
        cwd,
        kind="kimi_consult",
        tool="kimi_consult",
        spec=spec,
        timeout=timeout,
        detail_v=detail_v,
        ctx=ctx,
        idempotency_key=idempotency_key,
    )


# _ACTIVE_ASYNC (not read-only): the sync tool now creates an observable job record
# via the detached worker, so it can't advertise readOnlyHint (issue #138).
@mcp.tool(
    annotations=_ACTIVE_ASYNC,
    output_schema=REVIEW_RESULT_SCHEMA,
    title="Review git changes (paid)",
    meta=_tool_meta("kimi_review_changes"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_review_changes(
    scope: ScopeParam = "working_tree",
    ctx: Context | None = None,
    base: BaseParam = None,
    commit: CommitParam = None,
    paths: PathsParam = None,
    untracked: UntrackedParam = "explicit_only",
    workspace_root: WorkspaceRootParam = None,
    extra_context: ExtraContextParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    timeout_seconds: TimeoutSecondsParam = None,
    detail: DetailParam = "summary",
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Ask Kimi (a different model) to review your git changes for an independent
    second opinion.

    PAID — this spends Kimi quota on every new call; use kimi_dry_run or kimi_status
    (both free) first if you only need to check scope or readiness.

    scope: `working_tree` (tracked changes vs HEAD — untracked files follow the
    `untracked` policy and are NOT reviewed by default), `branch` (needs `base`, reviews
    `base...HEAD`), or `commit` (needs a `commit` SHA). The diff is gathered, secret-
    redacted, and bounded by this server; Kimi reviews it read-only and returns
    structured findings. Pass `workspace_root` (absolute) for the right repo. Optional
    `extra_context` (author intent, bounded like the diff) cuts false positives.

    The result's top-level `review_status` and `coverage` disclose whether the model
    actually ran and what it was shown: a `pass` over partial coverage is surfaced as
    `unknown`, and a tree with nothing reviewable returns `not_run`, never a `pass`.

    STATIC review, not a verify mode: the read-only agent profile gives Kimi no shell
    and no write tool, so it cannot run the project's checks to confirm its findings —
    treat them as unvalidated claims you verify yourself before acting.

    Data egress: this sends the gathered diff to your configured provider via the
    kimi CLI. The diff is
    secret-redacted (best-effort), but your `extra_context` is sent raw (unredacted),
    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace). Skill
    names and descriptions are exposed to the model up front, so that content can be sent
    even if your prompt never mentions it. The isolation setting does not suppress any of
    it: kimi's built-in skills always load, and AGENTS.md is read regardless.

    Your inputs are sent raw and unredacted. Secret redaction is best-effort and covers the
    gathered diff and Kimi's returned output — not what you type, and not the files Kimi
    reads for itself.

    Progress & recovery: blocks up to the resolved deadline (`timeout_seconds`, clamped
    10-600s; when omitted, the server-configured value, built-in default 300s). If that deadline
    expires the run is terminated and its partial output is not recoverable or resumable, so for a
    multi-file or whole-branch review that may exceed it, prefer `kimi_review_changes_async` (a
    background job, built-in default 1800s deadline; poll `kimi_job_status`). Coarse
    `notifications/progress` streams while it blocks when your client requests it; some MCP
    clients background a long call before the deadline, so `timeout_seconds` bounds the run, not
    necessarily the inline wait — either way the detached run (`meta.job_id`) is recoverable via
    `kimi_job_list`→`kimi_job_status`→`kimi_job_result`."""
    d = config.defaults()
    timeout = config.clamp_timeout(
        timeout_seconds if timeout_seconds is not None else d.timeout_seconds
    )
    prep = await _prepare_review(
        workspace_root=workspace_root,
        scope=scope,
        base=base,
        commit=commit,
        paths=paths,
        untracked=untracked,
        extra_context=extra_context,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=timeout,
        ctx=ctx,
        defaults=d,
        include_detail=True,
        detail=detail,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, detail_v = prep
    assert detail_v is not None  # include_detail=True always resolves a detail
    return await _run_sync(
        meta,
        cwd,
        kind="kimi_review_changes",
        tool="kimi_review_changes",
        spec=spec,
        timeout=timeout,
        detail_v=detail_v,
        ctx=ctx,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    annotations=_ACTIVE_PROPOSE,
    output_schema=DELEGATE_RESULT_SCHEMA,
    title="Delegate a coding task (paid)",
    meta=_tool_meta("kimi_delegate"),
)
@_guard(tier="propose", sandbox="workspace-write")
async def kimi_delegate(
    task: TaskParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    timeout_seconds: TimeoutSecondsParam = None,
    detail: DetailParam = "summary",
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Delegate a coding task to Kimi (a different model) in an isolated git
    worktree, and get back a **reviewable diff that is NOT applied** to your tree.

    PAID — this spends Kimi quota on every new call; use kimi_delegate_dry_run or
    kimi_status (both free) first if you only need to check scope or readiness.

    Kimi edits files with `workspace-write`, but only inside a throwaway worktree
    seeded from your current tracked state. The returned `diff` is Kimi's changes;
    review it, then apply it yourself if you want it. Requires a git repo with at
    least one commit. Pass `workspace_root` (absolute).

    NETWORK IS NOT BLOCKED: kimi has no sandbox, so a delegated task CAN reach the
    network — it may `git push`/`fetch`, run `gh`, `curl`, publish, or install
    dependencies, and it runs shell commands with your own user's privileges. Scope
    tasks accordingly and review the returned diff before applying it; the diff shows
    what changed in the worktree, not what else the run did. The Kimi model call also
    sends your `task` to your configured provider and lets Kimi read tracked files in
    the worktree and send their content.
    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace). Skill
    names and descriptions are exposed to the model up front, so that content can be sent
    even if your prompt never mentions it. The isolation setting does not suppress any of
    it: kimi's built-in skills always load, and AGENTS.md is read regardless.

    Your inputs are sent raw and unredacted. Secret redaction is best-effort and covers the
    gathered diff and Kimi's returned output — not what you type, and not the files Kimi
    reads for itself.

    Progress & recovery: blocks up to the resolved deadline (`timeout_seconds`, clamped
    10-600s; when omitted, the server-configured value, built-in default 300s). If that deadline
    expires the run is terminated and its partial output is not recoverable or resumable, so for a
    substantial or multi-file task that may exceed it, prefer `kimi_delegate_async` (a background
    job, built-in default 1800s deadline; poll `kimi_job_status`). Coarse `notifications/progress`
    streams while it blocks when your client requests it; some MCP clients background a long call
    before the deadline, so `timeout_seconds` bounds the run, not necessarily the inline wait —
    either way the detached run (`meta.job_id`) is recoverable via
    `kimi_job_list`→`kimi_job_status`→`kimi_job_result`."""
    d = config.defaults()
    timeout = config.clamp_timeout(
        timeout_seconds if timeout_seconds is not None else d.timeout_seconds
    )
    prep = await _prepare_delegate(
        task=task,
        tool_name="kimi_delegate",
        workspace_root=workspace_root,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=timeout,
        ctx=ctx,
        defaults=d,
        include_detail=True,
        detail=detail,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, detail_v = prep
    assert detail_v is not None  # include_detail=True always resolves a detail
    return await _run_sync(
        meta,
        cwd,
        kind="kimi_delegate",
        tool="kimi_delegate",
        spec=spec,
        timeout=timeout,
        detail_v=detail_v,
        ctx=ctx,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    annotations=_ACTIVE_ASYNC,
    output_schema=JOB_STARTED_SCHEMA,
    title="Delegate in background (paid)",
    meta=_tool_meta("kimi_delegate_async"),
)
@_guard(tier="propose", sandbox="workspace-write")
async def kimi_delegate_async(
    task: TaskParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Delegate a coding task to Kimi in the background and get a `job_id` back
    immediately (does not block on the run).

    PAID — this spends Kimi quota on every new call; use kimi_delegate_dry_run or
    kimi_status (both free) first if you only need to check scope or readiness.

    Same propose-tier behavior as `kimi_delegate` — Kimi works in a throwaway git
    worktree and the result carries a **reviewable diff that is NOT applied** — but
    detached; prefer it for a substantial or multi-file implementation task that can exceed the
    synchronous deadline (built-in default 300s), since a sync run whose deadline expires loses
    its partial work (this job's own deadline is separately configured, built-in default 1800s).
    Starting a job commits to spend (it runs to completion or its wall-clock deadline even if
    you never poll). Poll `kimi_job_status`; read/consume with
    `kimi_job_result`/`kimi_job_consume_result`; stop with `kimi_job_cancel`. Requires a git
    repo with at least one commit; pass `workspace_root` (absolute).

    NETWORK IS NOT BLOCKED: like `kimi_delegate`, this has no sandbox — a delegated task
    CAN push, fetch, install dependencies, or call out, running with your own user's
    privileges. Scope tasks accordingly and review the returned diff before applying it.
    The Kimi model call also sends your `task` (raw) to your configured provider and lets
    Kimi read tracked files in the worktree and send their content.

    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace).

    Secret redaction is best-effort and does not cover your `task` or the files Kimi
    reads for itself."""
    # Background jobs are bounded by the wall-clock deadline, not the sync timeout.
    deadline = config.job_max_seconds()
    prep = await _prepare_delegate(
        task=task,
        tool_name="kimi_delegate_async",
        workspace_root=workspace_root,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=deadline,
        ctx=ctx,
        defaults=config.defaults(),
        include_detail=False,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, _ = prep
    return await _start_async(
        meta,
        cwd,
        kind="kimi_delegate",
        tool="kimi_delegate_async",
        spec=spec,
        deadline=deadline,
        idempotency_key=idempotency_key,
    )


def _worker_cmd(job_dir: object) -> list[str]:
    return [sys.executable, "-m", "moonbridge._worker", str(job_dir)]


# Fields of a run `spec` that do NOT belong in the idempotency argument hash: pure
# provenance/scope dimensions already captured by (workspace, tool), never a knob that
# changes the paid run. `detail` is never in a spec (it is presentation-only). Hashing
# raw effective values is fine — the hash is internal and never returned.
# Spec keys that describe HOW the call was resolved rather than WHAT it asks for. They
# must not enter the dedup identity: `roots_source` in particular is per-connection, so
# the same logical keyed call can legitimately see a different value across reconnects
# and must still replay rather than raise idempotency_conflict (#393).
# `git_timeout` is server configuration, not caller input: two otherwise-identical calls
# should dedup together even if the operator retimed git in between, and an idempotency
# key must not silently stop replaying because a timeout default moved.
_ARG_HASH_EXCLUDE = frozenset({"cwd", "workspace_source", "kind", "roots_source", "git_timeout"})

# Backoff hint for idempotency_in_progress (a reservation still being published, or a
# contended lock), and how long a SYNC keyed call waits for that publication before
# giving up. The wait is a module constant so tests can compress it; publication normally
# takes milliseconds.
_IDEM_IN_PROGRESS_RETRY_MS = 250
_IDEM_SYNC_INPROGRESS_WAIT_S = 1.0
_IDEM_SYNC_INPROGRESS_POLL_S = 0.05
# Backoff hint for a transient read failure on an idempotency record (#202): the
# record may be intact, so the caller retries the same key after a short pause.
_IDEM_IO_ERROR_RETRY_MS = 1000

# Bound on acquiring the idempotency coordination locks (the per-process lock and the
# cross-process index flock) for a keyed start. A sibling stuck mid-critical-section (or a
# suspended peer holding the flock) degrades to a retryable idempotency_in_progress within
# this window instead of hanging a thread-pool worker indefinitely (#199). Kept under the
# sync in-progress wait so a bounded acquire fits inside that budget; normal contention
# clears in milliseconds.
_IDEM_LOCK_ACQUIRE_TIMEOUT_S = 0.5

_IDEM_MESSAGES = {
    "idempotency_conflict": (
        "idempotency_key already used with different effective arguments or server "
        "execution settings."
    ),
    "idempotency_result_unavailable": (
        "A prior run for this idempotency_key already completed; its result is no longer available."
    ),
    # Covers both a reservation still being published and a contended coordination lock
    # (the index flock serializes the whole workspace, so contention may be another key).
    "idempotency_in_progress": (
        "Idempotency coordination is momentarily busy (a run is still starting or the "
        "workspace lock is contended); retry shortly."
    ),
}


def _arg_hash_for_spec(spec: dict) -> str:
    """Hash the effective run inputs of a spec, dropping pure provenance fields."""
    return idempotency.arg_hash({k: v for k, v in spec.items() if k not in _ARG_HASH_EXCLUDE})


def _spawn_failure_envelope(exc: Exception, meta: Meta) -> dict:
    return serialize_error(
        ErrorResult(
            error=make_error(
                "internal_error",
                f"failed to start background job: {redaction.exc_summary(exc)}"[:300],
                repair_alternative=(
                    "Check the job state-dir permissions (MOONBRIDGE_STATE_DIR) and retry."
                ),
            ),
            meta=meta,
        )
    )


def _idem_error(code: str, meta: Meta, *, retry_after_ms: int | None = None) -> dict:
    return serialize_error(
        ErrorResult(
            error=make_error(
                cast("ErrorCode", code), _IDEM_MESSAGES[code], retry_after_ms=retry_after_ms
            ),
            meta=meta,
        )
    )


# The terminal (non-created/replay) outcomes of a keyed `start_idempotent`, mapped to
# their error-envelope inputs. Shared by _start_async and _run_sync so the two return
# paths cannot drift (#204). Any unexpected outcome degrades to in_progress (retryable),
# preserving both callers' prior "anything else -> in_progress" fallthrough.
_IDEM_TERMINAL_ERRORS: dict[str, tuple[str, int | None]] = {
    "conflict": ("idempotency_conflict", None),
    "unavailable": ("idempotency_result_unavailable", None),
    "in_progress": ("idempotency_in_progress", _IDEM_IN_PROGRESS_RETRY_MS),
}


def _idem_terminal_error(result_kind: str, meta: Meta) -> dict:
    code, retry_after_ms = _IDEM_TERMINAL_ERRORS.get(
        result_kind, _IDEM_TERMINAL_ERRORS["in_progress"]
    )
    return _idem_error(code, meta, retry_after_ms=retry_after_ms)


def _idem_io_error(meta: Meta) -> dict:
    """Retryable internal_error envelope for a transient read failure on an
    idempotency record (#202). Uses the existing internal_error code (temporary:
    True) with repair prose that tells the agent to retry the same call with the
    same idempotency_key, not start a new paid run."""
    return serialize_error(
        ErrorResult(
            error=make_error(
                "internal_error",
                "Transient storage error reading the idempotency record.",
                retry_after_ms=_IDEM_IO_ERROR_RETRY_MS,
                repair_alternative=("Retry the same call with the same idempotency_key."),
            ),
            meta=meta,
        )
    )


def _job_started_handle(
    job_id: str,
    *,
    kind: str,
    status: JobState,
    started_at: str,
    deadline: int,
    expires_at: str | None,
    meta: Meta,
) -> dict:
    meta.job_id = job_id
    poll_arguments: dict[str, Any] = {"job_id": job_id}
    if meta.cwd:
        poll_arguments["workspace_root"] = meta.cwd
    return JobStarted(
        job_id=job_id,
        kind=kind,
        status=status,
        started_at=started_at,
        deadline_seconds=deadline,
        ttl_seconds=config.job_ttl_seconds(),
        expires_at=expires_at,
        meta=meta,
        follow_up=Repair(
            next_step="poll_job_status",
            tool="kimi_job_status",
            arguments=poll_arguments,
            alternative=(
                "Poll kimi_job_status with these arguments, honoring poll_after_ms between "
                "polls; read the result with kimi_job_result once result_available is true. "
                "Recover a lost job_id with kimi_job_list."
            ),
        ),
    ).model_dump(mode="json")


def _mark_replayed(env: dict) -> dict:
    """Stamp meta.idempotency_replayed=true on an outgoing envelope so the caller can
    see no new spend occurred. Applied after the result is built (a replayed done job's
    envelope carries the worker's stored meta, not this call's), so the signal is not
    persisted into result.json."""
    meta = env.get("meta")
    if isinstance(meta, dict):
        meta["idempotency_replayed"] = True
    return env


# Strong refs to shielded-start futures whose awaiter was cancelled mid-spawn, so the
# loop cannot garbage-collect them before their cleanup callback runs (asyncio holds only
# weak refs to tasks). Each callback discards its own entry.
_PENDING_START_CLEANUPS: set[asyncio.Future] = set()


def _swallow_future_result(fut: asyncio.Future) -> None:
    """Done-callback that retrieves a fire-and-forget future's outcome so a failure isn't
    reported as an unretrieved-future warning. A cancelled future (e.g. loop teardown) has
    nothing to retrieve and ``.exception()`` would re-raise ``CancelledError`` from the
    callback, so skip it — the callback must always be safe."""
    if not fut.cancelled():
        fut.exception()


def _stop_orphaned_start(store: JobStore, cwd: str) -> Callable[[asyncio.Future], None]:
    """Build a done-callback for a shielded unkeyed start whose awaiter was cancelled
    mid-spawn (#199). Moving the spawn off-loop made it cancellable, so a client Esc during
    the spawn could otherwise leave a just-started (paid) job with no waiter to stop it —
    unkeyed jobs have no idempotency record to recover them. Once the spawn finishes this
    cancels the job it created so spend still stops; the store.cancel is offloaded to a
    thread because the callback runs on the event loop.

    Bound: if the event loop itself is torn down before the shielded spawn returns, the
    spawn's own task is cancelled and the job id is unavailable here — the job then becomes
    an ordinary detached worker that survives server shutdown and self-terminates at its
    deadline, which is the job store's designed behavior (a dropped server never kills a
    live worker), not an unbounded leak."""

    def _cb(fut: asyncio.Future) -> None:
        _PENDING_START_CLEANUPS.discard(fut)
        if fut.cancelled() or fut.exception() is not None:
            return  # the spawn itself was cancelled or failed: no job to stop
        job_id, _ = fut.result()
        with contextlib.suppress(RuntimeError):  # loop may be closing during teardown
            cancel_fut = asyncio.get_running_loop().run_in_executor(None, store.cancel, cwd, job_id)
            # Retrieve the fire-and-forget result so a store.cancel failure doesn't surface
            # as an unretrieved-future warning; cleanup failures are best-effort here.
            cancel_fut.add_done_callback(_swallow_future_result)

    return _cb


async def _start_job(meta: Meta, cwd: str, *, kind: str, spec: dict, deadline: int) -> dict:
    """Spawn a detached worker for `spec` and return the JobStarted handle (or an
    internal_error envelope if the job process could not be launched). Shared by
    every *_async tool so the spawn/handle contract stays identical across kinds."""
    store = config.job_store()
    # Off-loop: the subprocess spawn + meta/spec writes are blocking (#199). Shield the
    # spawn so a cancellation landing during it can't orphan a paid job: the spawn always
    # runs to completion, and if we were cancelled we register a callback that cancels the
    # resulting job before re-raising (keyed jobs are intentionally durable across cancel,
    # so this guard is only for the unkeyed path).
    start_fut = asyncio.ensure_future(
        asyncio.to_thread(
            store.start,
            _worker_cmd,
            cwd,
            kind=kind,
            extra={"result_format": RESULT_FORMAT},
            write_spec=spec,
        )
    )
    try:
        job_id, started_at = await asyncio.shield(start_fut)
    except asyncio.CancelledError:
        _PENDING_START_CLEANUPS.add(start_fut)
        start_fut.add_done_callback(_stop_orphaned_start(store, cwd))
        raise
    except OSError as exc:
        return _spawn_failure_envelope(exc, meta)
    return _job_started_handle(
        job_id,
        kind=kind,
        status="running",
        started_at=started_at,
        deadline=deadline,
        expires_at=None,
        meta=meta,
    )


async def _start_async(
    meta: Meta,
    cwd: str,
    *,
    kind: str,
    tool: str,
    spec: dict,
    deadline: int,
    idempotency_key: str | None,
) -> dict:
    """The *_async return path. Without a key it is exactly `_start_job`. With one it
    reserves (tool, key): a first reservation spawns and returns a running handle; a
    duplicate returns the existing job's REAL handle (its true status/timestamps, not a
    synthetic 'running'); conflict/unavailable/in-progress become their error envelopes.
    An _async caller never blocks, so in-progress is returned immediately (retryable).

    The keyed path's `start_idempotent`/`status` calls are blocking (cross-process flock,
    index sweep, subprocess spawn), so they run off the event loop via `asyncio.to_thread`
    to keep this process responsive to concurrent MCP requests (#199)."""
    if idempotency_key is None:
        return await _start_job(meta, cwd, kind=kind, spec=spec, deadline=deadline)
    store = config.job_store()
    try:
        outcome = await asyncio.to_thread(
            store.start_idempotent,
            _worker_cmd,
            cwd,
            kind=kind,
            tool=tool,
            key=idempotency_key,
            arg_hash=_arg_hash_for_spec(spec),
            extra={"result_format": RESULT_FORMAT},
            write_spec=spec,
            lock_timeout=_IDEM_LOCK_ACQUIRE_TIMEOUT_S,
        )
    except OSError as exc:
        return _spawn_failure_envelope(exc, meta)
    result_kind = outcome["kind"]
    if result_kind == "created":
        return _job_started_handle(
            outcome["job_id"],
            kind=kind,
            status="running",
            started_at=outcome["started_at"],
            deadline=deadline,
            expires_at=None,
            meta=meta,
        )
    if result_kind == "replay":
        snap = await asyncio.to_thread(store.status, cwd, outcome["job_id"])
        if snap is None:  # vanished between reserve and read (rare) -> treat as gone
            return _idem_error("idempotency_result_unavailable", meta)
        meta.idempotency_replayed = True
        return _job_started_handle(
            outcome["job_id"],
            kind=kind,
            status=snap["status"],
            started_at=snap["started_at"],
            deadline=snap["deadline_seconds"],
            expires_at=snap["expires_at"],
            meta=meta,
        )
    if result_kind == "io_error":
        return _idem_io_error(meta)
    # conflict / unavailable / in_progress (and any unexpected kind) -> error envelope.
    return _idem_terminal_error(result_kind, meta)


# Local poll cadence for a sync handler awaiting its own detached job: in-process
# disk reads, so much tighter than the client-facing poll_after_ms backoff.
_SYNC_POLL_INTERVAL_S = 0.25
# Post-timeout grace: the worker enforces the kimi timeout itself and writes a
# timeout envelope; this only covers worker scheduling/IO slack before we give up.
_SYNC_AWAIT_GRACE_S = 30
# Minimum spacing between `notifications/progress` sends while awaiting (F2): a
# module constant (not a literal) so tests can compress it to keep the suite fast.
_SYNC_PROGRESS_THROTTLE_S = 1.0


async def _await_job_result(
    cwd: str,
    job_id: str,
    kind: str,
    meta: Meta,
    detail_v: str,
    timeout: int,
    ctx: Context | None,
    *,
    keyed: bool = False,
) -> dict:
    """Await this handler's own detached job and return its envelope (F3).

    Explicit cancellation (client Esc / notifications/cancelled) cancels the job so
    spend stops; a transport drop kills this server but not the worker, leaving the
    result recoverable via kimi_job_list/kimi_job_result. While running, throttled
    `notifications/progress` are reported via `ctx` (F2) — message-only, at most one
    per `_SYNC_PROGRESS_THROTTLE_S` and only when `events_seen` changed, so a caller
    with no progressToken (or no `ctx` at all) sees no behavior change.

    When ``keyed`` (this call carried an idempotency_key), the job is treated as a
    durable shared run: neither a local-grace timeout nor this waiter's own
    cancellation cancels it, because another idempotent caller may be awaiting the same
    job. The run continues to its own deadline and stays recoverable via its job_id;
    only an explicit kimi_job_cancel stops it. That aligns with the point of an
    idempotency_key — the run should survive this connection dropping."""
    store = config.job_store()
    deadline = time.monotonic() + timeout + _SYNC_AWAIT_GRACE_S
    last_progress_at = 0.0
    last_events = -1
    try:
        while True:
            rec = await asyncio.to_thread(store.status, cwd, job_id)
            if rec is None:
                return _job_result_corrupt("job record disappeared while awaiting", meta)
            if rec["status"] != "running":
                break
            events = rec.get("events_seen", 0)
            now = time.monotonic()
            if (
                ctx is not None
                and events != last_events
                and now - last_progress_at >= _SYNC_PROGRESS_THROTTLE_S
            ):
                last_events = events
                last_progress_at = now
                with contextlib.suppress(Exception):
                    # Message-only, indeterminate progress: no fake total, and never
                    # raw event content (it can carry file contents/paths). With no
                    # progressToken from the caller, FastMCP's report_progress is a
                    # documented no-op, so this degrades silently either way.
                    await ctx.report_progress(
                        progress=float(events), message=f"kimi events: {events}"
                    )
            if time.monotonic() > deadline:
                if not keyed:
                    # Unkeyed: cancel to stop spend, and keep the static timeout-table
                    # repair — re-running via the async variant is the right recovery
                    # because this run is now gone.
                    await asyncio.to_thread(store.cancel, cwd, job_id)
                    return serialize_error(
                        ErrorResult(
                            error=make_error(
                                "timeout",
                                f"kimi run exceeded {timeout}s and the grace window; "
                                "job cancelled.",
                            ),
                            meta=meta,
                        )
                    )
                # Keyed: the shared run was NOT cancelled — it continues to its own
                # deadline. Steer the agent to POLL that run (as job_running does) rather
                # than the table's async escape hatch: sync and async are different dedup
                # identities, so re-running would start a SECOND paid run while this one
                # completes unobserved (#201). Echo the record's grown poll_after_ms so
                # the backoff matches kimi_job_status's own hint.
                poll_params: dict[str, Any] = {"job_id": job_id}
                if cwd:
                    poll_params["workspace_root"] = cwd
                return serialize_error(
                    ErrorResult(
                        error=make_error(
                            "timeout",
                            f"kimi run exceeded {timeout}s and the grace window; the job "
                            "continues in the background; fetch it via kimi_job_result.",
                            repair_next_step="poll_job_status",
                            repair_tool="kimi_job_status",
                            repair_arguments=poll_params,
                            retry_after_ms=rec.get("poll_after_ms"),
                            repair_alternative=(
                                "This keyed run continues in the background to its own "
                                "deadline. Poll kimi_job_status with the job_id above, "
                                "then read the result with kimi_job_result. Do not switch "
                                "to the async variant or drop the idempotency_key — either "
                                "starts a new paid run under a different dedup identity. "
                                "Repeating this exact keyed call reattaches to the same run "
                                "without new spend (it may hit the same local wait deadline)."
                            ),
                        ),
                        meta=meta,
                    )
                )
            await asyncio.sleep(_SYNC_POLL_INTERVAL_S)
    except asyncio.CancelledError:
        # Deliberate cancellation must stop spend — UNLESS this call is keyed, when the
        # job may be a run shared with another idempotent caller and must not be killed
        # by one waiter's cancellation. Synchronous on purpose: an already-cancelled
        # task cannot reliably await cleanup.
        if not keyed:
            with contextlib.suppress(Exception):
                store.cancel(cwd, job_id)
        raise
    rec2, payload = await asyncio.to_thread(store.result_payload, cwd, job_id)
    if rec2 is None:
        return _job_result_corrupt("job record expired before its result was read", meta)
    envelope, _delivered = _finished_job_envelope(rec2, payload, job_id, kind, meta, detail_v, None)
    return envelope


async def _run_sync(
    meta: Meta,
    cwd: str,
    *,
    kind: str,
    tool: str,
    spec: dict,
    timeout: int,
    detail_v: str,
    ctx: Context | None,
    idempotency_key: str | None,
) -> dict:
    """The synchronous active-tool tail: start (or dedup) the detached job and await it.
    Without a key it is the prior behavior. With one, a first reservation awaits its own
    new job; a duplicate awaits the EXISTING job's result and stamps
    meta.idempotency_replayed; conflict/unavailable become their error envelopes. A
    still-publishing reservation is waited on briefly (publication is normally
    sub-second) before returning idempotency_in_progress. A keyed await never cancels
    the shared job on timeout or client cancellation."""
    store = config.job_store()
    if idempotency_key is None:
        handle = await _start_job(meta, cwd, kind=kind, spec=spec, deadline=timeout)
        if handle.get("ok") is False:
            return handle  # spawn failure: internal_error, no spend, no record
        return await _await_job_result(cwd, handle["job_id"], kind, meta, detail_v, timeout, ctx)

    arg_hash = _arg_hash_for_spec(spec)
    wait_deadline = time.monotonic() + _IDEM_SYNC_INPROGRESS_WAIT_S
    while True:
        try:
            # Off-loop: blocking flock + index sweep + subprocess spawn (#199).
            outcome = await asyncio.to_thread(
                store.start_idempotent,
                _worker_cmd,
                cwd,
                kind=kind,
                tool=tool,
                key=idempotency_key,
                arg_hash=arg_hash,
                extra={"result_format": RESULT_FORMAT},
                write_spec=spec,
                lock_timeout=_IDEM_LOCK_ACQUIRE_TIMEOUT_S,
            )
        except OSError as exc:
            return _spawn_failure_envelope(exc, meta)
        result_kind = outcome["kind"]
        if result_kind == "created":
            # Set meta.job_id up front so a keyed timeout/terminal-error envelope (which
            # is built from this meta, not the job's stored one) still names the durable
            # job the caller is told to recover via kimi_job_result.
            meta.job_id = outcome["job_id"]
            return await _await_job_result(
                cwd, outcome["job_id"], kind, meta, detail_v, timeout, ctx, keyed=True
            )
        if result_kind == "replay":
            meta.job_id = outcome["job_id"]
            env = await _await_job_result(
                cwd, outcome["job_id"], kind, meta, detail_v, timeout, ctx, keyed=True
            )
            return _mark_replayed(env)
        if result_kind in ("conflict", "unavailable"):
            return _idem_terminal_error(result_kind, meta)
        # in_progress OR io_error: a transient state. Wait briefly for it to resolve
        # (a concurrent reservation finishing publish, or a flaky read clearing) rather
        # than bouncing the caller — a momentary blip can self-heal into a clean replay
        # within the wait budget. Only past the deadline do we surface the specific
        # envelope: io_error for a persistent read failure, idempotency_in_progress for
        # a still-publishing reservation (#202).
        if time.monotonic() >= wait_deadline:
            if result_kind == "io_error":
                return _idem_io_error(meta)
            return _idem_terminal_error("in_progress", meta)
        await asyncio.sleep(_IDEM_SYNC_INPROGRESS_POLL_S)


@mcp.tool(
    annotations=_ACTIVE_ASYNC,
    output_schema=JOB_STARTED_SCHEMA,
    title="Consult Kimi in background (paid)",
    meta=_tool_meta("kimi_consult_async"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_consult_async(
    question: QuestionParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    extra_context: ExtraContextParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Ask Kimi for a read-only second opinion in the background; get a `job_id`
    back immediately instead of blocking.

    PAID — this spends Kimi quota on every new call; there is no dry-run preview for a
    consult, so run kimi_status (free) first to confirm the CLI is installed and
    authenticated.

    Same read-only behavior as `kimi_consult` (Kimi never edits files), but detached —
    prefer it for a high-`reasoning_effort` or broad repo-grounded consult that can exceed the
    synchronous deadline (built-in default 300s), since a sync run whose deadline expires loses
    its partial work; this job's own deadline is separately configured (built-in default 1800s).
    Starting a job commits to spend (it runs to completion or its wall-clock deadline even if
    you never poll). Poll `kimi_job_status`; read/consume the consult envelope with
    `kimi_job_result`/`kimi_job_consume_result`; stop with `kimi_job_cancel`.

    Data egress: same as `kimi_consult` — sends your `question` and `extra_context`
    (raw, unredacted) to your configured provider via the kimi CLI, plus files Kimi reads from its
    resolved working directory (`workspace_root`, else the server cwd — MCP roots are unavailable).
    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace).

    Your inputs are sent raw and unredacted. Secret redaction is best-effort and covers the
    gathered diff and Kimi's returned output — not what you type, and not the files Kimi
    reads for itself.
    """
    deadline = config.job_max_seconds()
    prep = await _prepare_consult(
        question=question,
        tool_name="kimi_consult_async",
        workspace_root=workspace_root,
        extra_context=extra_context,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=deadline,
        ctx=ctx,
        defaults=config.defaults(),
        include_detail=False,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, _ = prep
    return await _start_async(
        meta,
        cwd,
        kind="kimi_consult",
        tool="kimi_consult_async",
        spec=spec,
        deadline=deadline,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    annotations=_ACTIVE_ASYNC,
    output_schema=JOB_STARTED_SCHEMA,
    title="Review git changes in background (paid)",
    meta=_tool_meta("kimi_review_changes_async"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_review_changes_async(
    scope: ScopeParam = "working_tree",
    ctx: Context | None = None,
    base: BaseParam = None,
    commit: CommitParam = None,
    paths: PathsParam = None,
    untracked: UntrackedParam = "explicit_only",
    workspace_root: WorkspaceRootParam = None,
    extra_context: ExtraContextParam = None,
    model: ModelParam = None,
    reasoning_effort: ReasoningEffortParam = None,
    isolation: IsolationParam = None,
    idempotency_key: IdempotencyKeyParam = None,
) -> dict:
    """Review your git changes in the background; get a `job_id` back immediately.

    PAID — this spends Kimi quota on every new call; use kimi_dry_run or kimi_status
    (both free) first if you only need to check scope or readiness.

    Same read-only behavior as `kimi_review_changes` (the diff is gathered, secret-
    redacted, and bounded, then reviewed read-only), but detached — prefer it for a multi-file
    or whole-branch review that can exceed the synchronous deadline (built-in default 300s),
    since a sync run whose deadline expires loses its partial work; this job's own deadline is
    separately configured (built-in default 1800s). The diff is gathered inside the job, so a bad
    `base`/`commit` comes back as the same structured error with **zero spend** (a bad
    `scope` is rejected by MCP input validation before the job starts). Starting a job commits to
    spend. Poll `kimi_job_status`; read/consume the review envelope with
    `kimi_job_result`/`kimi_job_consume_result`; stop with `kimi_job_cancel`. Pass
    `workspace_root` (absolute).

    Data egress: same as `kimi_review_changes` — sends the secret-redacted diff plus
    your raw (unredacted) `extra_context` to your configured provider via the kimi
    CLI; Kimi may also
    Kimi auto-loads the resolved workspace's AGENTS.md and discovers skills from its own
    config (including `extra_skill_dirs`, which may point outside the workspace).

    Your inputs are sent raw and unredacted. Secret redaction is best-effort and covers the
    gathered diff and Kimi's returned output — not what you type, and not the files Kimi
    reads for itself.
    """
    deadline = config.job_max_seconds()
    prep = await _prepare_review(
        workspace_root=workspace_root,
        scope=scope,
        base=base,
        commit=commit,
        paths=paths,
        untracked=untracked,
        extra_context=extra_context,
        model=model,
        reasoning_effort=reasoning_effort,
        isolation=isolation,
        timeout_seconds=deadline,
        ctx=ctx,
        defaults=config.defaults(),
        include_detail=False,
    )
    if isinstance(prep, dict):
        return prep
    meta, cwd, spec, _ = prep
    return await _start_async(
        meta,
        cwd,
        kind="kimi_review_changes",
        tool="kimi_review_changes_async",
        spec=spec,
        deadline=deadline,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    annotations=_FREE_READ,
    output_schema=DRY_RUN_SCHEMA,
    title="Preview a review (free)",
    meta=_tool_meta("kimi_dry_run"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_dry_run(
    scope: ScopeParam = "working_tree",
    ctx: Context | None = None,
    base: BaseParam = None,
    commit: CommitParam = None,
    paths: PathsParam = None,
    untracked: UntrackedParam = "explicit_only",
    workspace_root: WorkspaceRootParam = None,
    extra_context: ExtraContextParam = None,
    model: ModelDryRunParam = None,
    reasoning_effort: ReasoningEffortDryRunParam = None,
    isolation: IsolationParam = None,
) -> dict:
    """Preview what a `kimi_review_changes` call would send — scope, diff size,
    redactions, truncation. Free — no model call, no spend. Use it before a
    review to inspect the scope and the reported redactions; redaction is
    best-effort, so treat the preview as a check on scope, not as confirmation
    that no secret remains. Pass the same `extra_context` and `untracked` policy
    you would give the review so the preview matches it. `would_call_model` reports
    whether the paid call would actually run the model (False on an empty diff, where
    `prompt_bytes` is 0), and `coverage` discloses omitted untracked files just as the
    review would. The result echoes the effective `model`/`reasoning_effort` overrides
    the paid call would send (unvalidated). `deadline_advisory` is non-null when size
    or effort risks the synchronous deadline (null whenever `would_call_model` is
    False) and names `kimi_review_changes_async` verbatim — the async counterpart of
    the previewed call, not of this dry-run tool. A hint, not a refusal."""
    d = config.defaults()
    # Mirror _prepare_review's resolution so the preview reports what the paid call
    # would send: falsey-coalesced model, exact-None effort (#309).
    effort = reasoning_effort if reasoning_effort is not None else d.reasoning_effort
    cwd_guess = workspace.server_cwd()
    isolation_v, iso_err = _resolve_isolation(isolation)
    if iso_err is not None:
        # Validate like the active tools rather than silently normalizing — a dry
        # run must preview the same outcome the real call would produce (issue #6).
        meta = _base_meta(
            cwd_guess,
            None,
            tier="consult",
            sandbox="read-only",
            isolation=d.isolation,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=config.clamp_timeout(d.timeout_seconds),
        )
        return serialize_error(ErrorResult(error=iso_err, meta=meta))
    assert isolation_v is not None  # narrowed: iso_err was None
    roots, roots_source = await _roots_from_ctx(ctx)
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    if wres.error_code is not None:
        meta = _base_meta(
            cwd_guess,
            None,
            tier="consult",
            sandbox="read-only",
            isolation=isolation_v,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=config.clamp_timeout(d.timeout_seconds),
            roots_source=roots_source,
        )
        return _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)

    # Mirror kimi_review_changes: surface an unexpanded ${...} env placeholder before
    # gathering the diff, so the preview fails exactly where the paid review would (#46).
    dry_meta = _base_meta(
        cwd,
        wres.source,
        tier="consult",
        sandbox="read-only",
        isolation=isolation_v,
        model=model or d.model,
        reasoning_effort=effort,
        timeout_seconds=config.clamp_timeout(d.timeout_seconds),
        scope=scope,
        base=base,
        commit=commit,
        paths=paths,
        roots_source=roots_source,
    )
    placeholder = _placeholder_error(dry_meta)
    if placeholder is not None:
        return placeholder
    extra_args_err = _extra_args_error(dry_meta)
    if extra_args_err is not None:
        return extra_args_err
    effort_err = _reasoning_effort_shape_error(
        effort, dry_meta, from_config=reasoning_effort is None
    )
    if effort_err is not None:
        return effort_err

    max_bytes = config.max_input_bytes()
    extra_context_bytes = len((extra_context or "").encode("utf-8"))
    if extra_context_bytes > max_bytes:
        # Mirror the real review's validation so the preview fails exactly where the
        # paid call would (issue #6: a dry run must not green-light an oversize input).
        meta = _base_meta(
            cwd,
            wres.source,
            tier="consult",
            sandbox="read-only",
            isolation=isolation_v,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=config.clamp_timeout(d.timeout_seconds),
            scope=scope,
            base=base,
            commit=commit,
            roots_source=roots_source,
        )
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "input_too_large",
                    f"extra_context exceeds {max_bytes} bytes.",
                    details=ErrorDetail(field="extra_context"),
                    limit_bytes=max_bytes,
                    actual_bytes=extra_context_bytes,
                ),
                meta=meta,
            )
        )
    try:
        diff = gitdiff.gather_diff(
            cwd,
            scope,
            base=base,
            commit=commit,
            paths=paths,
            untracked=untracked,
            timeout=config.git_timeout_seconds(),
            max_bytes=max_bytes,
        )
    except orchestration.GITDIFF_EXCEPTIONS as exc:
        # Reuse run_review's exception set (not a hand-maintained copy) so a new gitdiff
        # error — e.g. InvalidUntrackedError — is handled here too, mapped to a structured
        # envelope by gitdiff_error rather than surfacing unhandled (PR #322 review).
        meta = _base_meta(
            cwd,
            wres.source,
            tier="consult",
            sandbox="read-only",
            isolation=isolation_v,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=config.clamp_timeout(d.timeout_seconds),
            scope=scope,
            base=base,
            commit=commit,
            roots_source=roots_source,
        )
        return orchestration.gitdiff_error(exc, meta)

    label = scope if scope != "branch" else f"branch {base}...HEAD"
    # Mirror the paid path's empty-diff short-circuit (orchestration.run_review): when
    # nothing is reviewable the real call sends 0 bytes and makes no model call, so the
    # preview must report exactly that instead of the size of a prompt never sent (#320).
    would_call_model = not (diff.summary.files_changed == 0 and not diff.text.strip())
    prompt = prompts.build_review_prompt(diff.text, label, extra_context or "")
    prompt_bytes = len(prompt.encode("utf-8")) if would_call_model else 0
    deadline_advisory = _deadline_advisory(
        would_call_model,
        prompt_bytes,
        effort,
        config.clamp_timeout(d.timeout_seconds),
        "kimi_review_changes_async",
    )
    return DryRunResult(
        cwd=cwd,
        workspace_source=wres.source,
        workspace_warning=workspace_warning_for(wres.source, cwd),
        roots_source=roots_source,
        tier="consult",
        sandbox="read-only",
        isolation=cast("Isolation", isolation_v),
        model=_dry_run_effective_model(model or d.model),
        reasoning_effort=effort,
        scope=scope,
        base=base,
        commit=commit,
        paths=paths or [],
        context_summary=ContextSummary(
            files_changed=diff.summary.files_changed,
            lines_added=diff.summary.lines_added,
            lines_removed=diff.summary.lines_removed,
        ),
        would_call_model=would_call_model,
        prompt_bytes=prompt_bytes,
        coverage=orchestration.build_coverage(scope=scope, diff=diff),
        max_input_bytes=max_bytes,
        truncated=diff.truncated,
        truncation_hint=diff.truncation_hint,
        redacted_paths_count=len(diff.redacted_paths),
        redacted_paths=diff.redacted_paths,
        deadline_advisory=deadline_advisory,
    ).model_dump(mode="json")


# Plain-language caveats for a delegate dry run: a no-worktree preview cannot prove
# uncommitted changes will replay, and untracked files are never seeded.
_DELEGATE_PLAN_NOTE = (
    "Seeds a throwaway worktree from HEAD plus your uncommitted tracked changes; "
    "this preview does not validate that those changes replay, so the real run may "
    "warn and base on HEAD only. Untracked files are never copied into the worktree."
)


@mcp.tool(
    annotations=_FREE_READ,
    output_schema=DELEGATE_DRY_RUN_SCHEMA,
    title="Preview a delegate (free)",
    meta=_tool_meta("kimi_delegate_dry_run"),
)
@_guard(tier="propose", sandbox="workspace-write")
async def kimi_delegate_dry_run(
    task: TaskDryRunParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    model: ModelDryRunParam = None,
    reasoning_effort: ReasoningEffortDryRunParam = None,
    isolation: IsolationParam = None,
) -> dict:
    """Preview what a `kimi_delegate`/`kimi_delegate_async` call would do — the
    baseline it seeds from (HEAD commit, tracked file count/size, uncommitted and
    untracked counts), the prompt size that would be sent, and the resolved
    workspace/isolation. Free — no model call, no spend, no worktree created.

    Use it before delegating to confirm scope and repo before committing to cost,
    exactly as `kimi_dry_run` previews `kimi_review_changes`. Mirrors the real
    delegate's zero-spend validation (workspace, isolation, task size, git repo), so
    a failure here is a failure the paid call would also hit. The returned
    `tier`/`sandbox` describe the previewed propose run, not this read-only preview;
    the result echoes the effective `model`/`reasoning_effort` overrides the paid
    call would send (unvalidated). `deadline_advisory` is non-null when size or
    reasoning effort risks the synchronous deadline and names `kimi_delegate_async`
    verbatim — the async counterpart of the previewed call, not of this dry-run tool.
    A hint, not a refusal."""
    d = config.defaults()
    # See kimi_dry_run: mirror the paid call's resolution (#309).
    effort = reasoning_effort if reasoning_effort is not None else d.reasoning_effort
    timeout = config.clamp_timeout(d.timeout_seconds)
    isolation_v, iso_err = _resolve_isolation(isolation)
    cwd_guess = workspace.server_cwd()
    if iso_err is not None:
        meta = _base_meta(
            cwd_guess,
            None,
            tier="propose",
            sandbox="workspace-write",
            isolation=d.isolation,
            model=model or d.model,
            reasoning_effort=effort,
            timeout_seconds=timeout,
        )
        return serialize_error(ErrorResult(error=iso_err, meta=meta))
    assert isolation_v is not None

    roots, roots_source = await _roots_from_ctx(ctx)
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    meta = _base_meta(
        cwd,
        wres.source,
        tier="propose",
        sandbox="workspace-write",
        isolation=isolation_v,
        model=model or d.model,
        reasoning_effort=effort,
        timeout_seconds=timeout,
        roots_source=roots_source,
    )
    if wres.error_code is not None:
        return _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)

    placeholder = _placeholder_error(meta)
    if placeholder is not None:
        return placeholder
    extra_args_err = _extra_args_error(meta)
    if extra_args_err is not None:
        return extra_args_err
    effort_err = _reasoning_effort_shape_error(
        effort, meta, from_config=reasoning_effort is None
    ) or _reasoning_effort_unsupported_error(effort, model or d.model, meta)
    if effort_err is not None:
        return effort_err

    limit = config.max_input_bytes()
    task_bytes = len((task or "").encode("utf-8"))
    if task_bytes > limit:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "input_too_large",
                    f"task exceeds {limit} bytes.",
                    details=ErrorDetail(field="task"),
                    limit_bytes=limit,
                    actual_bytes=task_bytes,
                ),
                meta=meta,
            )
        )
    # Ahead of worktree.plan(), matching where the paid call rejects it: this preview's
    # contract is that a failure here is a failure the paid call would also hit (#411).
    blank_err = _blank_input_error(task, "task", "kimi_delegate_dry_run", meta)
    if blank_err is not None:
        return blank_err

    try:
        plan = worktree.plan(cwd, timeout=config.git_timeout_seconds())
    except worktree.NotAGitRepoError as exc:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "not_a_git_repo",
                    str(exc),
                    details=ErrorDetail(field="workspace_root"),
                ),
                meta=meta,
            )
        )
    except (worktree.NoCommitsError, worktree.WorktreeError) as exc:
        return serialize_error(
            ErrorResult(
                error=make_error(
                    "worktree_error",
                    str(exc)[:300],
                    # The preview is read-only (no worktree is created), so a dirty tree is
                    # fine; this fires only when the repo has no commit to base on or a git
                    # command failed.
                    repair_alternative=(
                        "Ensure the repo has at least one commit and that git commands "
                        "succeed (e.g. finish any in-progress merge/rebase)."
                    ),
                ),
                meta=meta,
            )
        )

    prompt = prompts.build_delegate_prompt(task)
    prompt_bytes = len(prompt.encode("utf-8"))
    # Unlike kimi_dry_run, a delegate preview has no empty-diff-style short-circuit: a
    # blank task is already rejected above (_blank_input_error), so every successful
    # preview here reflects a real call that WOULD run the model — would_call_model is
    # unconditionally True at this point (#342).
    deadline_advisory = _deadline_advisory(
        True, prompt_bytes, effort, timeout, "kimi_delegate_async"
    )
    return DelegateDryRunResult(
        cwd=cwd,
        workspace_source=wres.source,
        workspace_warning=workspace_warning_for(wres.source, cwd),
        roots_source=roots_source,
        isolation=cast("Isolation", isolation_v),
        model=_dry_run_effective_model(model or d.model),
        reasoning_effort=effort,
        prompt_bytes=prompt_bytes,
        max_input_bytes=limit,
        worktree_plan=WorktreePlan(
            head_commit=plan.head_commit,
            head_subject=plan.head_subject,
            tracked_files=plan.tracked_files,
            tracked_bytes=plan.tracked_bytes,
            uncommitted_tracked_files=plan.uncommitted_tracked_files,
            untracked_files=plan.untracked_files,
            note=_DELEGATE_PLAN_NOTE,
        ),
        deadline_advisory=deadline_advisory,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Background-job lifecycle (free — local job state only, no model call)
# --------------------------------------------------------------------------- #
# Non-done job states mapped to the result-envelope error contract.
_STATE_TO_ERROR: dict[str, tuple[str, str]] = {
    "running": ("job_running", "The job is still running."),
    "cancelled": ("job_cancelled", "The job was cancelled."),
    "timeout": (
        "job_timeout",
        "The job exceeded its wall-clock deadline and was stopped.",
    ),
    "failed": ("job_failed", "The job failed without producing a result."),
}


def _job_meta(
    cwd: str,
    source: str | None,
    kind: str | None = None,
    roots_source: RootsSource | None = None,
) -> Meta:
    """Meta for a lifecycle-GENERATED error envelope (deadline as timeout). A kimi_job_*
    call never runs Kimi and never writes the caller's workspace, so tier/sandbox report
    the operation's own read-only posture — consistent with readOnlyHint — rather than the
    inspected job's posture (audit F5, #177). When a job record was resolved, its `kind`
    is surfaced via meta.job_kind so an agent can still recover the job's own posture; it
    stays None for not-found and pre-lookup errors, which resolved no job."""
    d = config.defaults()
    return _base_meta(
        cwd,
        source,
        tier="consult",
        sandbox="read-only",
        isolation=d.isolation,
        model=d.model,
        reasoning_effort=d.reasoning_effort,
        timeout_seconds=config.job_max_seconds(),
        job_kind=kind,
        roots_source=roots_source,
    )


def _job_workspace(cwd: str, source: str | None) -> Workspace:
    """Compact workspace context for job-lifecycle SUCCESS responses (#54): the same
    cwd/source/warning the error envelope's Meta carries, so a successful status/list
    call shows which repo it targeted and warns on a cwd fallback."""
    return Workspace(
        cwd=cwd,
        workspace_source=source,
        workspace_warning=workspace_warning_for(source, cwd),
    )


def _job_not_found(job_id: str, meta: Meta, workspace_root: str | None = None) -> dict:
    # kimi_job_list takes only workspace_root (not job_id); echo the caller's value
    # so the repair targets the same workspace the lookup used.
    list_params: dict[str, Any] = {"workspace_root": workspace_root} if workspace_root else {}
    return serialize_error(
        ErrorResult(
            error=make_error(
                "job_not_found",
                f"No job '{job_id}' in this workspace.",
                details=ErrorDetail(field="job_id"),
                repair_arguments=list_params or None,
            ),
            meta=meta,
        )
    )


async def _resolve_job_workspace(
    ctx: Context | None, workspace_root: str | None
) -> tuple[str, str | None, RootsSource, dict | None]:
    """Resolve the workspace for a lifecycle call. Returns (cwd, source, roots_source,
    error).

    `roots_source` is returned on the success path too, not just folded into the
    workspace-error envelope: every lifecycle-GENERATED error below reports the roots
    state THIS lookup saw, which is often why the caller is looking at the wrong
    workspace at all (#393). It never overwrites a delivered stored result — that one
    keeps the originating run's value."""
    cwd_guess = workspace.server_cwd()
    roots, roots_source = await _roots_from_ctx(ctx)
    wres = workspace.resolve_workspace(workspace_root, roots, cwd_guess)
    cwd = wres.path or cwd_guess
    if wres.error_code is not None:
        meta = _job_meta(cwd, wres.source, roots_source=roots_source)
        err = _workspace_error_result(wres.error_code, wres.error_detail, roots, meta)
        return cwd, wres.source, roots_source, err
    return cwd, wres.source, roots_source, None


def _job_status_model(data: dict, workspace: Workspace) -> JobStatus:
    state = data["status"]
    mapped = _STATE_TO_ERROR.get(state)
    detail = mapped[1] if (mapped and state not in ("running", "done")) else None
    return JobStatus(
        job_id=data["job_id"],
        kind=data["kind"],
        status=data["status"],
        started_at=data["started_at"],
        elapsed_ms=data["elapsed_ms"],
        deadline_seconds=data["deadline_seconds"],
        poll_after_ms=data["poll_after_ms"],
        ttl_seconds=data["ttl_seconds"],
        expires_at=data["expires_at"],
        result_available=data["result_available"],
        # Strict: the store always stamps result_ok (null-valued when unknown), so a
        # missing key is an internal contract break that must surface, not silently
        # become null and defeat the required-nullable field (#335).
        result_ok=data["result_ok"],
        detail=detail,
        cleanup_warnings=data.get("cleanup_warnings", []),
        events_seen=data.get("events_seen", 0),
        last_event_at=data.get("last_event_at"),
        event_age_ms=data.get("event_age_ms"),
        workspace=workspace,
    )


@mcp.tool(
    annotations=_JOB_READ,
    output_schema=JOB_STATUS_SCHEMA,
    title="Check job status (free)",
    meta=_tool_meta("kimi_job_status"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_job_status(
    job_id: JobIdParam, ctx: Context | None = None, workspace_root: WorkspaceRootParam = None
) -> dict:
    """Check a background job's lifecycle state without fetching the full result.

    Use after any `*_async` call (kimi_delegate_async, kimi_consult_async,
    kimi_review_changes_async) or any sync consult/review/delegate (whose `meta.job_id`
    names its record). Returns status, elapsed time, expiry, and `result_available`; when
    it is true, call kimi_job_result. `result_ok` reports a done job's producer-declared
    outcome — true (success), false (a stored error envelope), or null (running, no stored
    envelope, an unclassifiable payload, or a record finalized before this field) — so you
    can spot a stored FAILURE without fetching it. It does not guarantee the payload is
    still fetchable; a cross-release record may report an outcome yet fail kimi_job_result
    with job_result_incompatible. Free — no model call.

    Honor `poll_after_ms` between polls — for a running job it GROWS with elapsed
    runtime (bounded), so following it backs you off instead of tight-looping (a
    delegate often runs ~20s). `expires_at` is null while running and is set once the
    job finishes; results are then retained `ttl_seconds` past that completion."""
    cwd, source, roots_source, err = await _resolve_job_workspace(ctx, workspace_root)
    if err is not None:
        return err
    store = config.job_store()
    data = await asyncio.to_thread(store.status, cwd, job_id)
    if data is None:
        return _job_not_found(
            job_id, _job_meta(cwd, source, roots_source=roots_source), workspace_root
        )
    return _job_status_model(data, _job_workspace(cwd, source)).model_dump(mode="json")


# A finished job's success payload must match the result model for its kind, so
# kimi_job_result returns exactly the envelope that kind's synchronous tool would.
_JOB_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "kimi_delegate": DelegateResult,
    "kimi_consult": ConsultResult,
    "kimi_review_changes": ReviewResult,
}


def _validate_job_success(payload: dict, kind: str, rec: dict, meta: Meta) -> dict:
    """Return a done job's success payload after checking it matches the expected
    result type for its kind. A delegate result carries no verdict/confidence (#31),
    so those are dropped first (an older worker may still have written them). An
    unknown kind or a payload that does not validate is classified via the record's
    stored result-format version — cross-release incompatibility or corruption (#305)
    — rather than passed through as an arbitrary envelope."""
    if kind == "kimi_delegate":
        payload.pop("verdict", None)
        payload.pop("confidence", None)
    model = _JOB_RESULT_MODELS.get(kind)
    if model is None:
        return _job_result_unreadable(f"unknown job kind {kind!r}", rec, payload, meta)
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        return _job_result_unreadable(
            f"stored {kind} result did not match its schema: {exc}", rec, payload, meta
        )
    return payload


def _stored_result_format(rec: dict) -> int | None:
    """The result-format version stamped on the job record at spawn, or None when it
    is absent or unusable. The record's `extra` is opaque JSON that can hold anything
    after corruption, and bool/float compare equal to int (True == 1), so only an
    exact `int` >= 1 is trusted as a discriminator."""
    extra = rec.get("extra")
    value = extra.get("result_format") if isinstance(extra, dict) else None
    return value if type(value) is int and value >= 1 else None


def _job_result_unreadable(detail: str, rec: dict, payload: dict, meta: Meta) -> dict:
    """Classify a stored payload that failed strict validation. A record stamped with
    a DIFFERENT persisted result format was written by another release and can never
    be read by this one — job_result_incompatible, permanent, never retryable. An
    equal, missing, or unusable stamp means corruption — internal_error, unchanged.
    Classification never inspects the validation-error types: a newer release's
    additions can fail as extra_forbidden OR literal_error, and a differing format
    makes the record unreadable whichever field tripped (#305)."""
    fmt = _stored_result_format(rec)
    if fmt is None or fmt == RESULT_FORMAT:
        return _job_result_corrupt(detail, meta)
    stored_meta = payload.get("meta")
    version = stored_meta.get("server_version") if isinstance(stored_meta, dict) else None
    fingerprint = stored_meta.get("fingerprint") if isinstance(stored_meta, dict) else None
    provenance = f"result_format {fmt}; this release reads {RESULT_FORMAT}"
    if isinstance(version, str) and version:
        provenance += f", producer server_version {version}"
    if isinstance(fingerprint, str) and fingerprint:
        provenance += f", producer fingerprint {fingerprint}"
    # The provenance strings and `detail` echo stored payload fragments, so redact the
    # whole composed message at this single sink and bound its size, mirroring
    # _job_result_corrupt.
    message = (
        f"stored job result was written under a different result format ({provenance}): {detail}"
    )
    return serialize_error(
        ErrorResult(
            error=make_error(
                "job_result_incompatible",
                (redaction.redact_text(message) or "")[:500],
            ),
            meta=meta,
        )
    )


def _job_result_corrupt(detail: str, meta: Meta) -> dict:
    # `detail` interpolates ValidationError text that can echo stored payload fragments
    # (Pydantic's input_value), so redact at this single sink for both corrupt-result paths.
    return serialize_error(
        ErrorResult(
            error=make_error(
                "internal_error",
                f"job result could not be returned: {redaction.redact_text(detail) or ''}"[:300],
                repair_alternative=(
                    "Start a new job; if this persists, run kimi_status and check the server logs."
                ),
            ),
            meta=meta,
        )
    )


async def _job_result_impl(
    job_id: JobIdParam,
    ctx: Context | None,
    workspace_root: str | None,
    *,
    consume: bool,
    detail: str = "summary",
) -> dict:
    cwd, source, roots_source, err = await _resolve_job_workspace(ctx, workspace_root)
    if err is not None:
        return err
    detail_v, detail_err = _resolve_detail(detail)
    if detail_err is not None:
        return serialize_error(
            ErrorResult(error=detail_err, meta=_job_meta(cwd, source, roots_source=roots_source))
        )
    assert detail_v is not None
    store = config.job_store()
    rec, payload = await asyncio.to_thread(store.result_payload, cwd, job_id)
    if rec is None:
        return _job_not_found(
            job_id, _job_meta(cwd, source, roots_source=roots_source), workspace_root
        )
    # Derive the lifecycle-error meta from the job's kind so a running/corrupt
    # consult/review job reports consult/read-only, not the default propose tier.
    meta = _job_meta(cwd, source, rec["kind"], roots_source=roots_source)
    envelope, delivered = _finished_job_envelope(
        rec, payload, job_id, rec["kind"], meta, detail_v, workspace_root
    )
    if not (consume and delivered):
        # Not a consume, or the envelope does not faithfully deliver the stored
        # payload (lifecycle error, corruption, incompatibility): the record must
        # survive so kimi_job_result can still reach it (#306).
        return envelope
    outcome = await asyncio.to_thread(store.discard, cwd, job_id)
    if outcome is DiscardOutcome.MISSING:
        # Another consume, TTL reaping, or count-cap eviction removed the record
        # first — report job_not_found, matching what this caller would have seen
        # had the winner run marginally earlier. Only the store's own verdict can
        # say this: a post-hoc status probe cannot tell a lost race from a partial
        # deletion failure (#314 review).
        return _job_not_found(job_id, meta, workspace_root)
    # REMOVED delivers the consumed result. DELETE_FAILED delivers too: the payload
    # validated and delivering beats destroying — deletion stays best-effort (as
    # the pre-split rmtree was) and the TTL reaper owns the retained record.
    return envelope


def _finished_job_envelope(
    rec: dict,
    payload: dict | None,
    job_id: str,
    kind: str,
    meta: Meta,
    detail_v: str,
    workspace_root: str | None,
) -> tuple[dict, bool]:
    """Map a terminal-or-running job record to the caller-facing envelope. Shared by
    the job-fetch tools and the sync await path so the two can never diverge.

    Also reports whether the envelope faithfully DELIVERS the stored payload — True
    only for a validated stored success or a validated stored error result. Generated
    envelopes (still-running/cancelled/failed lifecycle errors, corruption,
    incompatibility) return False: they describe the record rather than deliver it,
    so a consume must not destroy it (#306). The flag is explicit because ok alone
    cannot decide this — a validated stored error is ok:false yet delivered."""
    # Every envelope built here is ABOUT job_id, including the generated failure ones
    # (corrupt/incompatible/state errors), so stamp the correlation onto the fallback
    # meta up front. A validated stored payload carries its own meta and gets the same
    # job_id stamped after validation below.
    meta.job_id = job_id
    state = rec["status"]
    if state == "done" and payload is not None:
        # Validation must see the STORED bytes: patching meta first would silently heal a
        # corrupt job_id/fingerprint and destroy the producer-fingerprint evidence (#305).
        # job_id (caller correlation) and the CURRENT fingerprint (the payload is
        # normalized to this server's surface, so a stale contract id would mislead
        # clients that cache/branch on it) are stamped only AFTER validation succeeds.
        stored_meta = payload.get("meta")
        stored_version = (
            stored_meta.get("server_version") if isinstance(stored_meta, dict) else None
        )
        if payload.get("ok") is True:
            validated_payload = _validate_job_success(payload, kind, rec, meta)
            # ok stays True only when validation succeeded; _validate_job_success
            # substitutes a generated corrupt/incompatible error envelope otherwise.
            delivered = validated_payload.get("ok") is True
            if delivered and isinstance(validated_payload.get("meta"), dict):
                validated_payload["meta"]["job_id"] = job_id
                validated_payload["meta"]["fingerprint"] = FINGERPRINT
            # slim_meta AFTER apply_detail, and after the job_id/fingerprint stamping
            # above — both write non-null values, so neither reintroduces a null key.
            # This is the single delivery chokepoint the sync and replay paths share,
            # which is what keeps their wire shapes identical (#334).
            return slim_meta(apply_detail(validated_payload, detail_v)), delivered
        # An error payload (ok: false) should be an ErrorResult; validate it too, since
        # a disk-backed result.json could be partially written or corrupted.
        try:
            validated = ErrorResult.model_validate(payload)
        except ValidationError as exc:
            return (
                _job_result_unreadable(
                    f"stored error result was malformed: {exc}", rec, payload, meta
                ),
                False,
            )
        validated.meta.job_id = job_id
        validated.meta.fingerprint = FINGERPRINT
        # server_version is PROVENANCE about the run that produced this payload — unlike
        # `fingerprint` (stamped above), it must NOT be normalized to this server. Validation
        # would otherwise fire Meta's default_factory and stamp the CURRENT version onto a
        # pre-upgrade run's error, misattributing old failures to the newest release.
        # Absent stays absent: an honest unknown beats a plausible-but-wrong value.
        validated.meta.server_version = stored_version
        # `ErrorResult.model_validate` above deliberately does NOT enforce the
        # invalid_arguments non-empty-list invariant (errors.make_error does, but a
        # model_validator here would make a pre-existing legacy record unreadable) — so
        # this read site trusts the worker's own persistence-boundary guard
        # (_worker._guard_invalid_arguments, #419) to have normalized any nonconformant
        # envelope before it ever reached disk.
        # Boundary redact (#186/F10): a schema-valid payload written by a pre-fix worker
        # (still within its TTL) could carry unredacted exception text in its message. Scope
        # this belt-and-braces pass to `internal_error` — the code every raw-exception sink
        # emits — so domain errors (already redacted at write time) aren't re-run through the
        # heuristic redactor and can't be over-redacted.
        if validated.error.code == "internal_error":
            validated.error.message = redaction.redact_text(validated.error.message) or ""
        return serialize_error(validated), True
    code, message = _STATE_TO_ERROR.get(state, ("job_failed", "The job did not complete."))
    # A still-running job is the one recoverable case: point at the poll tool with
    # the concrete job_id and a backoff so the agent can act without parsing prose.
    # Echo the caller's workspace_root so the poll targets the same workspace.
    running = state == "running"
    poll_params: dict[str, Any] = {"job_id": job_id}
    if workspace_root:
        poll_params["workspace_root"] = workspace_root
    # Reuse the record's already-computed poll_after_ms (the growing backoff
    # kimi_job_status returns) as the retry hint, so polling via job_result on a long
    # run backs off the same way without recomputing the backoff in two places.
    retry_after = rec.get("poll_after_ms") if running else None
    return (
        serialize_error(
            ErrorResult(
                error=make_error(
                    cast("ErrorCode", code),
                    message,
                    repair_arguments=poll_params if running else None,
                    retry_after_ms=retry_after,
                ),
                meta=meta,
            )
        ),
        False,
    )


@mcp.tool(
    annotations=_JOB_READ,
    output_schema=JOB_RESULT_SCHEMA,
    title="Fetch job result (free)",
    meta=_tool_meta("kimi_job_result"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_job_result(
    job_id: JobIdParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    detail: DetailParam = "summary",
) -> dict:
    """Fetch a finished background Kimi job's result WITHOUT deleting the record.

    Works for any async job or sync consult/review/delegate (whose `meta.job_id` names
    its record) — kimi_delegate_async (a `diff`), kimi_consult_async (a consult
    answer), or kimi_review_changes_async (a review with `verdict`). Use when
    kimi_job_status reports result_available=true; the envelope matches the job's
    kind, so branch on `tool`. meta.job_id is set. A still-running/cancelled/timed-
    out/failed job returns an error envelope — as does a done job whose stored result
    this release cannot read (job_result_incompatible). To fetch and delete, use
    kimi_job_consume_result. Free — no model call.

    `detail="summary"` (default) omits the raw model text; pass `detail="full"` for
    the complete raw output and metadata (#56)."""
    return await _job_result_impl(job_id, ctx, workspace_root, consume=False, detail=detail)


@mcp.tool(
    annotations=_JOB_MUTATE,
    output_schema=JOB_RESULT_SCHEMA,
    title="Fetch and delete job result (free)",
    meta=_tool_meta("kimi_job_consume_result"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_job_consume_result(
    job_id: JobIdParam,
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    detail: DetailParam = "summary",
) -> dict:
    """Fetch a finished background Kimi job's result and delete the stored record.

    Same envelope as kimi_job_result (matching the job's kind — branch on `tool`),
    then removes completed job state — but only once the stored result has been read
    intact and validated (a success or the job's own error envelope): a stored result
    this release cannot read (job_result_incompatible or a corruption internal_error)
    is NOT deleted, so it stays inspectable via kimi_job_result. Deletion precedes
    the response, so a response lost in transit does not restore the record; a failed
    removal retains the record until its TTL (kimi_job_status still shows it). Use
    only when you no longer need to poll or re-read the job. Non-done jobs are not
    deleted. Free — no model call.

    `detail` works as in kimi_job_result (#56)."""
    return await _job_result_impl(job_id, ctx, workspace_root, consume=True, detail=detail)


@mcp.tool(
    annotations=_JOB_CANCEL,
    output_schema=JOB_STATUS_SCHEMA,
    title="Cancel a job (free)",
    meta=_tool_meta("kimi_job_cancel"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_job_cancel(
    job_id: JobIdParam, ctx: Context | None = None, workspace_root: WorkspaceRootParam = None
) -> dict:
    """Cancel a running background Kimi job.

    Asks the worker to shut down gracefully so it tears down its throwaway worktree,
    then force-kills it if it overstays, and marks the job cancelled (cancelled jobs
    cannot be resumed). If the worktree could not be removed, `cleanup_warnings`
    names the leftover path. Already-terminal jobs are returned unchanged, so cancel
    is idempotent — a retry after a lost response is safe. Free — no model call."""
    cwd, source, roots_source, err = await _resolve_job_workspace(ctx, workspace_root)
    if err is not None:
        return err
    store = config.job_store()
    data = await asyncio.to_thread(store.cancel, cwd, job_id)
    if data is None:
        return _job_not_found(
            job_id, _job_meta(cwd, source, roots_source=roots_source), workspace_root
        )
    return _job_status_model(data, _job_workspace(cwd, source)).model_dump(mode="json")


@mcp.tool(
    annotations=_JOB_READ,
    output_schema=JOB_LIST_SCHEMA,
    title="List background jobs (free)",
    meta=_tool_meta("kimi_job_list"),
)
@_guard(tier="consult", sandbox="read-only")
async def kimi_job_list(
    ctx: Context | None = None,
    workspace_root: WorkspaceRootParam = None,
    limit: JobLimitParam = None,
    status: JobStatusFilterParam = None,
) -> dict:
    """List the background jobs known for this workspace, newest first.

    Free — no model call. Use to recover job_ids lost across context compaction or
    interruption. Returns each job's id, kind, status, start time, result_available,
    `result_ok` (a done job's outcome — true/false/null; see kimi_job_status), and expiry,
    so a stored failure is triageable without fetching each result.

    Returns every retained job by default; pass `limit` (1-1000) or `status` to narrow. They
    narrow independently — omitting `limit` returns every job matching `status`, not every
    job. Only an explicit `limit` truncates: when more jobs match, the response sets
    `truncated: true` with a `truncation_hint` — the extra rows are dropped, not paged, so
    omit `limit` to get them all rather than looking for a cursor.

    Read a job's result promptly — a finished record can silently drop off. This list is
    not permanent storage: terminal records expire after the TTL (default 24h), and a
    per-workspace soft cap (default 50, clamped 1-1000) evicts the oldest terminal records
    as new jobs start, so a finished job can disappear even before its `expires_at`.
    Running jobs are never evicted, so a busy workspace can hold more than the cap — and
    more than `limit`'s 1000 ceiling.
    Includes sync-originated records (any sync consult/review/delegate call); the cap/TTL
    eviction covers both."""
    # `_roots_source` is unused here alone: a successful listing returns a Workspace, not
    # a Meta, so it has no member to report it on. The workspace-error envelope built
    # inside the resolver still carries it.
    cwd, source, _roots_source, err = await _resolve_job_workspace(ctx, workspace_root)
    if err is not None:
        return err
    store = config.job_store()
    rows = await asyncio.to_thread(store.list_jobs, cwd)
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    # limit=None means "no tool-side cap" — the store's own retention is the only bound
    # (#396), so the caller is the only source of truncation. Compute it before the cut, and
    # skip the cut entirely when uncapped: rows[:None] would return the same rows but as a
    # fresh shallow copy, which is pure waste on the path that now carries every retained row.
    truncated = limit is not None and len(rows) > limit
    if limit is not None:
        rows = rows[:limit]
    jobs = [
        JobSummary(
            job_id=r["job_id"],
            kind=r["kind"],
            status=r["status"],
            started_at=r["started_at"],
            elapsed_ms=r["elapsed_ms"],
            result_available=r["result_available"],
            result_ok=r["result_ok"],  # strict, same contract as kimi_job_status (#335)
            expires_at=r["expires_at"],
        )
        for r in rows
    ]
    result = JobListResult(
        jobs=jobs,
        workspace=_job_workspace(cwd, source),
        truncated=truncated,
        truncation_hint=(
            f"showing the {limit} newest of more matching jobs; omit `limit` for every "
            "retained match, or narrow with `status`"
            if truncated
            else None
        ),
    ).model_dump(mode="json")
    # exclude_none=True at the model level would also strip nested nullable-but-required
    # fields (e.g. JobSummary.result_ok, legitimately None for a running job) — so omit
    # just truncation_hint by hand rather than reaching for the blanket dump flag.
    if result["truncation_hint"] is None:
        del result["truncation_hint"]
    return result


def _make_signal_handler(log: logging.Logger, previous: Any) -> Callable[[int, object], None]:
    """A signal handler that logs which signal arrived, then defers to the prior
    disposition — so we add a "who killed it" breadcrumb without changing shutdown
    behavior (we do not attempt graceful cleanup; AnyIO/FastMCP own that)."""

    def handler(signum: int, frame: object) -> None:
        name = signal.Signals(signum).name
        log.info("moonbridge %s: received %s, shutting down", __version__, name)
        if callable(previous):
            previous(signum, frame)  # e.g. default SIGINT handler raises KeyboardInterrupt
        else:  # SIG_DFL: restore and re-raise so the OS default (terminate) still happens
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    return handler


def _install_signal_logging(log: logging.Logger) -> None:
    """Log a breadcrumb on SIGINT/SIGTERM, chaining to the existing handler. Best
    effort: AnyIO may replace these once the loop starts — we don't fight it."""
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            # ValueError: not the main thread; AttributeError: signal absent on this OS.
            previous = signal.getsignal(signum)
            if previous == signal.SIG_IGN:
                continue  # inherited as ignored — leave it truly ignored, install nothing
            signal.signal(signum, _make_signal_handler(log, previous))


def _enforce_posix_platform(os_name: str | None = None) -> None:
    """Refuse to serve on a non-POSIX platform.

    The async-job safety layer — ``fcntl`` advisory locks (pid-reuse / zombie-worker
    guards), process-group teardown (``os.killpg``/``start_new_session``), and
    ``SIGTERM``-driven graceful cancellation — is POSIX-only; elsewhere it silently
    degrades to owned-children-only locking and direct-PID kills that orphan kimi's
    child processes. Rather than ship a half-safe server, fail loudly before the
    transport loop starts. WSL2 reports ``os.name == "posix"`` and is unaffected.

    ``MOONBRIDGE_ALLOW_UNSUPPORTED_PLATFORM=1`` downgrades the hard exit to a
    stderr warning for operators who knowingly accept consult-only, unsupported use
    (#232)."""
    platform_name = os.name if os_name is None else os_name
    if platform_name == "posix":
        return
    if os.environ.get("MOONBRIDGE_ALLOW_UNSUPPORTED_PLATFORM") == "1":
        sys.stderr.write(
            "WARNING: MOONBRIDGE_ALLOW_UNSUPPORTED_PLATFORM=1 set on a non-POSIX "
            f"platform (os.name={platform_name}); the async-job safety layer (fcntl locks, process "
            "groups, signal handlers) cannot hold. Consult-only, unsupported; do not "
            "use delegate/review against untrusted work.\n"
        )
        return
    wsl2_hint = "On Windows, run it under WSL2. " if platform_name == "nt" else ""
    sys.stderr.write(
        f"moonbridge requires a POSIX platform (macOS or Linux); got os.name={platform_name}. "
        f"{wsl2_hint}Set "
        "MOONBRIDGE_ALLOW_UNSUPPORTED_PLATFORM=1 to override "
        "(consult-only, unsupported).\n"
    )
    raise SystemExit(1)


def main() -> None:
    """Console-script entrypoint: run the MCP server over stdio.

    A stdio MCP server cannot be transparently auto-restarted — the client owns the
    pipe and the `initialize` handshake — so the goal here is to fail *legibly*: a
    fatal error out of the transport loop leaves an actionable stderr breadcrumb
    (name, version, reconnect hint) instead of a silent exit, and clean disconnects
    are logged as shutdown rather than crashes (#76)."""
    _enforce_posix_platform()
    log = obs.configure()
    _install_signal_logging(log)
    log.info("moonbridge %s starting (stdio)", __version__)
    try:
        mcp.run()
    except (KeyboardInterrupt, EOFError, BrokenPipeError) as exc:
        # Client closed the pipe or interrupted us — an ordinary disconnect, not a crash.
        log.info("moonbridge %s: clean shutdown (%s)", __version__, type(exc).__name__)
    except SystemExit:
        raise  # honor an explicit exit code (e.g. from our own signal path)
    except Exception as exc:
        log.exception(
            "moonbridge %s crashed out of the stdio transport loop; the MCP server "
            "has stopped and will not recover on its own. Reconnect with the /mcp command "
            "(or restart the client).",
            __version__,
        )
        raise SystemExit(1) from exc
    else:
        log.info("moonbridge %s: stdio transport closed, shutting down", __version__)


if __name__ == "__main__":  # pragma: no cover
    main()
