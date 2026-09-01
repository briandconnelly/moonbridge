"""Wire-size budget for the discovery surface (audit 2, F2).

`tools/list` is what every client pays before its first useful call, and the least-capable
realistic client preloads all of it. This bounds the serialized size with a ceiling — growth
within the remaining headroom passes, and exceeding it forces a future description or schema
addition to be a deliberate, reviewed budget change rather than silent drift."""

import json

import pytest
from fastmcp import Client

from moonbridge.server import mcp

# Measured 2026-07-26 at schema-56: 79,242 bytes. Measured at schema-57 (idempotency_key and
# extra_context compressed into the registry): 76,911 bytes. Measured again at schema-57 after
# adding kimi_capabilities' `detail` parameter (F1): 77,439 bytes. Measured once more after
# giving kimi_capabilities its own accurate CapabilitiesDetailParam description (F1 review
# finding #2, replacing the shared DetailParam's inaccurate "omits the raw model text" wording
# for this no-model-call tool): 77,561 bytes — ~120 bytes for a materially more useful
# description on the tool that most needs it. Raising this number is a reviewed decision — say
# why in the PR body. Measured at schema-58 (N1: a PAID/spend or Free — no model call. marker
# added to every tool description, closing the 7-tool gap a keyword sweep found): 78,072 bytes —
# 511 bytes for a consistent, single-token cost signal readable from tools/list alone, budget
# raised accordingly. Measured once more, still at schema-58, after correcting the PAID block's
# dry-run pointer (kimi_delegate now names kimi_delegate_dry_run instead of kimi_dry_run;
# kimi_consult's now states no preview tool exists instead of naming the wrong one): 78,111
# bytes — still within budget, no further change. Measured 2026-07-26 (F3 + F9: every tool
# gained a `title` and a namespaced `_meta` stability tier, so tools/list alone shows which
# tools are experimental): 79,723 bytes — ~1,600 bytes for 17 titles plus 17 `_meta` blocks;
# budget raised to the next 500 above the measured value. Measured again 2026-07-26 (F7:
# JobStarted gained a required `follow_up: Repair` field, so the three `*_async` tools'
# outputSchema each grew the nested Repair shape): 81,739 bytes — budget raised to the next
# 500 above the measured value. Measured again 2026-07-26 (F5: kimi_job_list gained
# `limit`/`status` input params plus `truncated`/`truncation_hint` output fields, so its
# outputSchema and parameter list both grew): 82,537 bytes — budget raised to the next 500
# above the measured value. Measured again 2026-07-26 (F8 follow-on: `roots_source` was
# added to the flat `kimi_dry_run`/`kimi_delegate_dry_run` output schemas, ~222 bytes
# undocumented at the time): 82,759 bytes. Measured once more the same day (final-review fix
# wave: the three PAID-block descriptions changed "spends Kimi quota on every call" to
# "...every new call", resolving a contradiction with idempotency_key's "no new spend" replay
# wording): 82,771 bytes — still within budget, no further change. Measured once more the same
# day (Copilot review of #383, merged forward onto this branch: normalized every cost marker
# to the literal canonical token — `kimi_transfer`'s `FREE —` and `kimi_capabilities`'s
# line-wrapped `Free —` corrected, and the three async active tools gained their own `PAID —`
# block naming the right preview tool): 83,281 bytes — budget raised to the next 500 above the
# measured value. Measured once more after merging forward a Copilot review fix on #382
# clarifying that async_lifecycle appears only on the `*_async` tools, in both
# CapabilitiesDetailParam's description and the kimi_capabilities docstring: 83,354 bytes —
# still within budget, no further change. Measured again 2026-07-27 (#396: kimi_job_list's
# `limit` became nullable so a no-arg call returns every retained job — the integer-or-null
# schema costs ~30 bytes, the rest is the reworded param description, docstring, and the
# now-explicit "running jobs are never evicted" ceiling caveat): 83,895 bytes — budget raised
# to the next 500 above the measured value. Spent deliberately: the wording it buys is what
# tells a client the default is complete and that `truncated` is a cap with no cursor.
# Measured again 2026-07-29 (#411: `question`/`task` must be non-blank, so the three param
# descriptions — QuestionParam, TaskParam, TaskDryRunParam, across five tools — state the rule
# and when it is enforced): 84,292 bytes — budget raised to the next 500 above the measured
# value. Spent deliberately, and the cheapest wording that carries the rule was chosen (the
# first draft cost ~145 bytes more): the constraint is enforced at runtime rather than by a
# schema `minLength`, so these descriptions are the ONLY place a client can discover it before
# spending. It buys back a whole class of wasted paid calls — the reported repro burned 23,120
# tokens on a whitespace-only question.
# Measured again 2026-08-01 (#414: the three sync tools' Progress & recovery paragraphs now name
# `kimi_job_status` as the recovery chain's polling step and state that some MCP clients
# background a long call before the server's own deadline, so `timeout_seconds` bounds the run,
# not necessarily the client's inline wait — the job record covers that case too): 84,740 bytes —
# budget raised to the next 500 above the measured value.
# Measured again 2026-08-01 (#423: `CapabilitiesResult.protocol_revision` — this schema is not on
# the `tools/list` wire path (its description is stripped from the advertised `outputSchema` by
# `_strip_schema_noise`), but the tool's `outputSchema` embeds the bare `{"type":"string"}`
# property): 84,778 bytes (+38 B) — still within budget, no further change.
# Measured again 2026-08-01 (#426: `CapabilitiesResult.annotations_reading` — same treatment as
# `protocol_revision` above, a second bare `{"type":"string"}` property on `kimi_capabilities`'
# outputSchema; its description is likewise stripped from the advertised schema): 84,818 bytes
# (+40 B) — still within budget, no further change.
# Measured again 2026-08-02 (#427: the six egress tool docstrings converged onto one shared
# skills-discovery sentence pair — `cli_contract.SKILLS_DISCOVERY_FACT`/
# `SKILLS_DISCOVERY_FACT_FULL`. First pass (before tightening) grew this to 85,299 bytes, over
# budget — a
# tighter canonical pair (dropped "both"/"that workspace's"/redundant "your", kept
# "resolved") plus a minimal per-delegate addendum (the pre-existing "throwaway worktree
# seeded from tracked state" sentence earlier in each docstring already disclosed
# tracked/seeded; the only genuinely new fact needed here is that delegate's workspace IS
# the worktree, and that scrubbing it doesn't exclude $KIMI_CODE_HOME/skills/) recovered the
# rest): 84,818 -> 84,975 bytes (+157 B, closing wording gaps between sites without adding
# net new disclosure bulk) — fits under the existing budget, no raise.
# Measured again 2026-08-02 (#342: `kimi_dry_run`/`kimi_delegate_dry_run` both gained a
# `deadline_advisory` output field, described identically on both, plus one docstring
# sentence per tool — a genuinely new disclosure, unlike C4's wording-only tightening
# above: it tells a client BEFORE it spends that a previewed call's size or reasoning
# effort risks the synchronous deadline, and names the tool's own `_async` alternative):
# 86,418 bytes (+1,443 B, incl. an accuracy reword of the field description) — over
# budget; budget raised to the next 500 above the
# measured value.
# Measured again 2026-08-02 (#342 round-3, Kimi review concerns/1 medium, verified
# valid: the generic "this tool's `_async` variant" phrasing pointed a
# kimi_dry_run/kimi_delegate_dry_run caller at a nonexistent kimi_dry_run_async /
# kimi_delegate_dry_run_async — the field, both docstrings, and REFERENCE.md now name
# the previewed PAID tool's own `_async` counterpart verbatim instead, a longer but
# genuinely more correct disclosure, not flab): 86,749 bytes (+331 B) — over budget;
# budget raised to the next 500 above the measured value.
# Measured again 2026-08-02 (#433: `Coverage` gained `redaction: RedactionSummary |
# None`, a new nested object with three properties, on both `kimi_review_changes`'
# and `kimi_dry_run`'s outputSchema). Field descriptions on RedactionSummary are NOT
# in `_KEPT_DESCRIPTIONS`, so they cost nothing here — the whole delta is JSON Schema
# STRUCTURE: the object is INLINED directly into `coverage.redaction` (no `$ref`/`$defs`
# on the wire — verified against the live `tools/list` payload; `$defs` exists only in
# the result-format snapshot's separate pydantic-schema view), so the anyOf-null
# wrapper plus three typed properties are duplicated in full across both tools'
# outputSchema: 87,257 bytes (+508 B) — over budget; budget raised to the next 500 above
# the measured value.
# Measured again 2026-08-02 (#433 review C4: `RedactionSummary.inline_masks` gained a
# `Field(ge=0)` constraint — a validated invariant, not description prose, so it DOES
# reach the wire as a `"minimum":0` property, duplicated across the same two tools'
# outputSchema as the row above): 87,281 bytes (+24 B) — still within budget, no
# further change.
# Measured 2026-08-13 (fix!: remove Codex-port vestiges — the three `transfer_*` codes left
# every tool's outputSchema error enum, and `app_server_stderr_tail` left the error branch
# in all 16, while kimi_status shed its dead rate_limit prose): 83,466 bytes (-3,815 B).
# A DROP, so the budget is not lowered automatically — lowering it is its own deliberate
# act, taken below now that a second change has confirmed the new level.
# Measured again the same day (audit M2: `isolation` registered in PARAMETER_CONTRACTS, its
# 295-char inline description compressed to a 225-char summary with AGENTS.md-always-loads,
# the skill-name egress path, and extra_skill_dirs moved to kimi://params — 8 tools carry
# it, so ~560 B): 82,866 bytes (-600 B). Budget lowered to the next 500 above the measured
# value, banking both reductions so a regression cannot silently spend them.
# Measured 2026-08-13 (separating-context-from-constraints audit: kimi_status's spend rule
# gained a `Spend:` label matching the sibling directive blocks, and the RateLimit schema
# description dropped its Codex-provenance aside): 82,869 bytes (+3 B) — still within
# budget, no further change.
# Measured 2026-08-31 (ADR 0005, the FastMCP 4 / MCP SDK v2 upgrade): `RootsSource` gained
# the "unsupported" value, and that enum is duplicated into `meta.roots_source` on every
# tool's outputSchema, so a 15-char addition lands ~14 times: 83,075 bytes (+206 B), 75 B
# over. Budget raised to the next 500 above the measured value. The addition is not
# compressible — narrowing the enum instead (dropping the three now-unemitted values) would
# be a breaking change to a published value set, which is the more expensive trade.
# Measured again 2026-09-01 (PR #12 review): the `meta.roots_source` description was rewritten
# to define `unsupported` and mark the other three historical, and five tool descriptions that
# still promised MCP roots could pick the working directory were corrected — both duplicated
# across the same output schemas. Measured once more after a second review pass corrected the
# `workspace_root` parameter description and the cwd-fallback warning text — both duplicated the
# same way, and both still telling agents an MCP root could aim a call: 83,256 bytes. Still
# within budget, no further raise. (The parameter description was deliberately kept close to its
# original length: a longer one breached the fan-out ceiling its PARAMETER_CONTRACTS exemption
# was granted at, which `test_fanout_exemptions_stay_within_their_pinned_ceiling` caught.)
TOOLS_LIST_BYTE_BUDGET = 83_500

# The measured tools/list size as of the last deliberate review above — NOT a second gate.
# The budget assertion below is the only hard failure; this exists purely so the assertion's
# failure message can show how far a later change has drifted from the last reviewed
# measurement, without requiring a diff against this file's comment history. Revisit this
# value (re-measure and update it in the same commit) whenever a DELIBERATE change to
# tools/list's measured size lands — i.e. whenever a new row is added to the budget-history
# comment above, whether or not that row also moves TOOLS_LIST_BYTE_BUDGET (most of the
# history above is "still within budget, no further change" rows that grew the measured size
# without touching the budget; the target must track every one of those too, or it silently
# goes stale between the raises).
TOOLS_LIST_BYTE_TARGET = 83_256


def _budget_failure_message(measured: int) -> str:
    return (
        f"tools/list is {measured} bytes (target {TOOLS_LIST_BYTE_TARGET}), over the "
        f"{TOOLS_LIST_BYTE_BUDGET} budget. Compact a description or schema, or raise "
        "the budget deliberately."
    )


def test_budget_failure_message_reports_measured_budget_and_target():
    """The budget assertion's failure message must surface all three figures — measured,
    BUDGET, and TARGET — so the delta from the last deliberate measurement is visible on
    every run, not just on a failure. TARGET is informational only: the budget assertion
    below stays the ONLY hard gate."""
    msg = _budget_failure_message(99_999)
    assert "99999" in msg
    assert str(TOOLS_LIST_BYTE_BUDGET) in msg
    assert str(TOOLS_LIST_BYTE_TARGET) in msg


@pytest.mark.anyio
async def test_tools_list_wire_size_budget():
    async with Client(mcp) as c:
        tools = await c.list_tools()
    payload = [t.model_dump(mode="json", exclude_none=True) for t in tools]
    size = len(json.dumps(payload, separators=(",", ":")))
    assert size <= TOOLS_LIST_BYTE_BUDGET, _budget_failure_message(size)
