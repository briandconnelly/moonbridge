"""Tests for the shared parameter-contract registry (issue #333).

The registry is the single source of truth for parameters whose inline
`tools/list` description is a compressed summary and whose full semantics live in
the `kimi://params` resource. These tests guard that (a) every registry entry is
well-formed, (b) each tool's inline Field description is drawn from the registry
summary (so the inline and the resource cannot drift), and (c) the compressed
inline summary still carries the selection-critical facts and guarantees a client
needs on a first call.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

import pytest

from moonbridge import param_contracts, server


def test_registry_is_nonempty_and_well_formed():
    contracts = param_contracts.PARAMETER_CONTRACTS
    assert contracts, "PARAMETER_CONTRACTS is empty"
    for name, c in contracts.items():
        assert c.name == name, f"{name}: contract.name mismatch"
        assert c.summary.strip(), f"{name}: empty summary"
        assert c.full.strip(), f"{name}: empty full"
        # The summary is the compressed inline form; the full is authoritative and
        # never shorter than the summary.
        assert len(c.summary) <= len(c.full), f"{name}: summary longer than full"


def test_resource_uri_is_stable():
    assert param_contracts.PARAMS_RESOURCE_URI == "kimi://params"


def test_resource_body_serializes_every_contract():
    body = param_contracts.resource_body()
    assert isinstance(body, dict)
    params = body["params"]
    assert set(params) == set(param_contracts.PARAMETER_CONTRACTS)
    for name, entry in params.items():
        assert entry["summary"] == param_contracts.PARAMETER_CONTRACTS[name].summary
        assert entry["full"] == param_contracts.PARAMETER_CONTRACTS[name].full


@pytest.mark.parametrize("name", sorted(param_contracts.PARAMETER_CONTRACTS))
def test_inline_summary_points_at_the_resource(name):
    """A compressed inline summary must reference its findable full home (#333)."""
    summary = param_contracts.PARAMETER_CONTRACTS[name].summary
    assert param_contracts.PARAMS_RESOURCE_URI in summary, (
        f"{name} summary does not reference {param_contracts.PARAMS_RESOURCE_URI}"
    )


# Which server param alias each registry entry feeds. The inline description on
# these aliases must be exactly the registry summary — the anti-drift guarantee.
_ALIAS_FOR = {
    "idempotency_key": "IdempotencyKeyParam",
    "reasoning_effort": "ReasoningEffortParam",
    "extra_context": "ExtraContextParam",
    "isolation": "IsolationParam",
}


def test_registry_covers_exactly_the_wired_aliases():
    assert set(_ALIAS_FOR) == set(param_contracts.PARAMETER_CONTRACTS)


@pytest.mark.parametrize(("name", "alias"), sorted(_ALIAS_FOR.items()))
def test_alias_description_is_the_registry_summary(name, alias):
    """The wire description IS the registry summary — one source, no drift (#333)."""
    desc = getattr(server, alias).__metadata__[0].description
    assert desc == param_contracts.PARAMETER_CONTRACTS[name].summary


def test_idempotency_summary_keeps_selection_critical_facts():
    """Compressing idempotency_key must keep what a first call needs (#333)."""
    s = param_contracts.PARAMETER_CONTRACTS["idempotency_key"].summary.lower()
    assert "workspace" in s, "dropped the tool+workspace scoping"
    assert "conflict" in s, "dropped the different-args conflict behavior"
    assert "async" in s, "dropped the sync/async-are-separate-tools fact"
    assert "bounded" in s or "not indefinite" in s, "dropped bounded-retention warning"


def test_idempotency_full_keeps_moved_lifecycle_detail():
    """The moved detail must survive in its new home, not vanish (#333)."""
    full = param_contracts.PARAMETER_CONTRACTS["idempotency_key"].full.lower()
    assert "idempotency_in_progress" in full
    assert "idempotency_result_unavailable" in full
    assert "ttl" in full
    assert "idempotency_replayed" in full


class TestAuditTwoCompaction:
    """F2: the two parameters whose inline text is long enough to be worth compressing.

    `workspace_root` and `model` are deliberately NOT registered: their current inline
    descriptions are already shorter than any summary carrying the required `kimi://params`
    pointer, so registering them would grow `tools/list` (measured 2026-07-26: +70 and +248
    bytes respectively).

    `isolation` WAS on that list (+88 bytes) and came off it on 2026-08-13. The original
    measurement was sound for the summary it tested — one that kept the whole description and
    only appended the pointer. A later audit re-cut it against the *content* instead: with
    AGENTS.md-always-loads, the skill-name egress path, and `extra_skill_dirs` moved to the
    resource, the summary is 225 chars against 295, and the measured `tools/list` delta for
    registering it was 600 bytes (83,466 -> 82,866, isolation-only). Re-measured, not assumed.

    The lesson generalizes, and `test_high_fanout_parameters_are_registered` encodes it:
    "already terse" is a claim about a specific candidate summary, not a permanent property
    of a parameter."""

    # What registration BUYS, measured against current state only: inline you ship `summary`;
    # without it you would ship `full`. Deliberately not a historical delta — the previous
    # formulation stored per-parameter "previous inline" character counts that went stale the
    # moment a summary was retuned (a Codex review caught this class citing 253/296 numbers
    # left behind by an abandoned draft). Nothing here can go stale: both sides are read live.
    #
    # Bytes, not characters: these strings carry em-dashes, so one character is three UTF-8
    # bytes on the wire, and character arithmetic understates the saving.
    MIN_REGISTRATION_SAVING_BYTES: ClassVar[int] = 500

    @staticmethod
    def _serialized_bytes(text: str) -> int:
        """Bytes this description costs in one tool's inputSchema, as JSON on the wire."""
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))

    def test_extra_context_is_registered(self):
        assert "extra_context" in param_contracts.PARAMETER_CONTRACTS

    @pytest.mark.parametrize("name", ["workspace_root", "model"])
    def test_terse_parameters_stay_out_of_the_registry(self, name):
        # Guard the measurement that drove this decision: registering these grows the wire.
        assert name not in param_contracts.PARAMETER_CONTRACTS

    def test_isolation_registration_actually_shrank_the_wire(self):
        """The claim that reversed the prior decision, kept checkable.

        A summary that merely appended the pointer to the full text would fail this — the
        case the 2026-07-26 measurement correctly rejected."""
        c = param_contracts.PARAMETER_CONTRACTS["isolation"]
        assert self._serialized_bytes(c.summary) < self._serialized_bytes(c.full)

    @pytest.mark.parametrize("name", sorted(param_contracts.PARAMETER_CONTRACTS))
    def test_registration_pays_for_itself_in_wire_bytes(self, name):
        c = param_contracts.PARAMETER_CONTRACTS[name]
        fanout = _wire_parameter_costs()[name][0]
        saved = (self._serialized_bytes(c.full) - self._serialized_bytes(c.summary)) * fanout
        assert saved >= self.MIN_REGISTRATION_SAVING_BYTES, (
            f"{name}: shipping the summary instead of the full text across {fanout} tools "
            f"saves {saved} B, under the {self.MIN_REGISTRATION_SAVING_BYTES} B floor — "
            "this registration costs more indirection than it buys"
        )

    @pytest.mark.parametrize("name", sorted(param_contracts.PARAMETER_CONTRACTS))
    def test_summary_is_shorter_than_its_full_text(self, name):
        c = param_contracts.PARAMETER_CONTRACTS[name]
        assert len(c.summary) < len(c.full)

    def test_extra_context_summary_keeps_the_safety_facts(self):
        # UNTRUSTED framing and the redaction gap are safety-critical (contract-checklist §3)
        # and must survive compression.
        summary = param_contracts.PARAMETER_CONTRACTS["extra_context"].summary
        assert "UNTRUSTED" in summary
        # Whole-word, case-insensitive: a bare substring like "edaction" would also match
        # a mangled/misspelled word that merely happens to end the same way.
        assert re.search(r"\bredaction\b", summary, re.IGNORECASE)

    def test_idempotency_summary_keeps_the_spend_guarantee(self):
        summary = param_contracts.PARAMETER_CONTRACTS["idempotency_key"].summary
        # Whole-word matches only — a bare substring would also match unrelated words
        # like "suspend" or "expend" that contain "spend" but don't carry the guarantee.
        assert re.search(r"\bspend\b", summary, re.IGNORECASE) or re.search(
            r"\bunpaid\b", summary, re.IGNORECASE
        )


# --- The fan-out rule (audit M2) -------------------------------------------------------
# MCP has no $ref across tools: a parameter's description is inlined into EVERY tool that
# takes it, so cost is `len(description) x fan-out`. An audit measured 17.6 KB of
# tools/list — 21% — spent on repeated shared-parameter prose, and the two worst offenders
# (`workspace_root` at 13 tools, `isolation` at 8) were not in the registry at all: #333
# built the mechanism but only registered the params whose *individual* text was longest,
# which is the wrong axis.
#
# This guard is on total bytes, not on either factor alone. A long description on a
# single-tool parameter is cheap and stays free; a short one repeated 13 times is not.
_FANOUT_BUDGET_BYTES = 1_500

# Over budget, but registering it is measured to be a LOSS — the exemption a fan-out rule
# needs so it forces a measurement rather than a reflex. `workspace_root` spans 13 tools at
# 180 chars; the shortest summary that still states "absolute", the cwd fallback, and
# meta.workspace_warning came to 178 chars, so registering it saved 26 bytes and bought an
# indirection. Re-measure before adding an entry here; "it looked terse" is not evidence.
# Each exemption PINS the cost it was granted at. An exemption keyed only by name would
# survive its own justification: the description could grow without bound and stay exempt,
# which is the exact stale-exemption regression this block claims to prevent (Codex review).
# Exceeding the ceiling fails and forces a re-measurement, not a silent pass.
_FANOUT_EXEMPT: dict[str, tuple[int, str]] = {
    # 13 tools x 180 chars = 2392 B on the wire (UTF-8, as the transport sends it). The
    # shortest summary still stating "absolute", the cwd fallback, and
    # meta.workspace_warning came to 178 chars, so registering saved 26 B.
    "workspace_root": (2550, "measured 2026-08-13: registering saves 26 B (178 vs 180 chars)"),
}


def _wire_parameter_costs() -> dict[str, tuple[int, int, int]]:
    """name -> (tool count, description chars, total inlined description BYTES).

    Bytes are counted the way the transport actually sends them: the MCP SDK serializes
    through pydantic's ``model_dump_json``, which emits raw UTF-8 rather than backslash-u
    escapes, so an em-dash costs 3 bytes and not 6. This used ``json.dumps`` at its
    ``ensure_ascii=True`` default, which disagreed with
    ``TestAuditTwoCompaction._serialized_bytes`` in this same file and inflated every
    non-ASCII description (Copilot review). Both now use one convention, and it is the
    one that matches the wire.
    """
    import asyncio

    from fastmcp import Client

    async def collect():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(collect())
    seen: dict[str, tuple[int, int, int]] = {}
    for t in tools:
        for name, prop in (t.input_schema or {}).get("properties", {}).items():
            desc = prop.get("description") or ""
            count, _, total = seen.get(name, (0, 0, 0))
            wire = TestAuditTwoCompaction._serialized_bytes(desc)
            seen[name] = (count + 1, len(desc), total + wire)
    return seen


def test_high_fanout_parameters_are_registered():
    """Any parameter whose inlined description costs more than the budget must be split.

    Registration is the fix: the summary ships inline, the full text ships once from
    kimi://params. The alternative fix — shortening the description until it fits — is
    equally acceptable and this test accepts it too.
    """
    over = {
        name: (n, chars, total)
        for name, (n, chars, total) in _wire_parameter_costs().items()
        if total > _FANOUT_BUDGET_BYTES
        and name not in param_contracts.PARAMETER_CONTRACTS
        and name not in _FANOUT_EXEMPT
    }
    assert not over, (
        "unregistered parameters over the "
        f"{_FANOUT_BUDGET_BYTES}-byte fan-out budget: "
        + "; ".join(f"{k} ({n} tools x {c} chars = {t} B)" for k, (n, c, t) in sorted(over.items()))
        + " — split it into summary/full in PARAMETER_CONTRACTS, or shorten it."
    )


def test_fanout_measurement_sees_a_known_multi_tool_parameter():
    """Guard the guard: a clean result must mean the measurement actually found params."""
    costs = _wire_parameter_costs()
    assert costs, "no parameters measured at all"
    count, chars, total = costs["workspace_root"]
    assert count >= 10, f"workspace_root should span many tools, saw {count}"
    assert chars > 0 and total >= chars


def test_fanout_exemptions_stay_within_their_pinned_ceiling():
    """Guard the guard, in both directions.

    An exemption that fell under the budget is dead weight and hides the next regression.
    An exemption that OUTGREW the cost it was granted at is worse: the measurement that
    justified it no longer describes the parameter being exempted.
    """
    costs = _wire_parameter_costs()
    for name, (ceiling, why) in _FANOUT_EXEMPT.items():
        assert name in costs, f"{name} is exempted but no longer on the wire"
        actual = costs[name][2]
        assert actual > _FANOUT_BUDGET_BYTES, (
            f"{name} is now under the fan-out budget; drop its exemption"
        )
        assert name not in param_contracts.PARAMETER_CONTRACTS, (
            f"{name} is both exempted and registered; remove the exemption"
        )
        assert actual <= ceiling, (
            f"{name} now costs {actual} B, above the {ceiling} B ceiling its exemption was "
            f"granted at ({why}). Re-measure: a shorter summary may now pay for itself."
        )
