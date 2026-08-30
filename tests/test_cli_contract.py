"""The external `kimi` CLI contract.

Replaces the Codex-era contract suite: the two CLIs share no flags, no sandbox vocabulary,
and no failure phrasings, so the old assertions described a contract that does not exist.

Every failure string below is a real message captured from kimi-code 0.35.0 (see
docs/kimi-help/0.35.0/), not an invented one — a classifier tuned to invented phrasings
would pass its tests and misclassify in production.
"""

from __future__ import annotations

import pytest

from moonbridge import cli_contract


# --------------------------------------------------------------------------- #
# Flag classes
# --------------------------------------------------------------------------- #
def test_always_send_and_help_gated_are_disjoint():
    """A guarantee-bearing flag must never be droppable. Overlap would let a depth-gate
    silently remove a flag the safety design depends on."""
    assert not set(cli_contract.ALWAYS_SEND_FLAGS) & set(cli_contract.HELP_GATED_FLAGS)


def test_the_agent_file_flag_is_guarantee_bearing():
    """It is the only thing enforcing read-only, so it cannot be help-gated."""
    assert cli_contract.AGENT_FILE_FLAG in cli_contract.ALWAYS_SEND_FLAGS


def test_the_output_format_flag_is_guarantee_bearing():
    """Without stream-json there is no event surface, and for a read-only tier no answer
    channel at all."""
    assert cli_contract.OUTPUT_FORMAT_FLAG in cli_contract.ALWAYS_SEND_FLAGS


def test_the_prompt_flag_is_guarantee_bearing():
    assert cli_contract.PROMPT_FLAG in cli_contract.ALWAYS_SEND_FLAGS


def test_every_help_gated_flag_declares_whether_it_takes_a_value():
    # The gate skips `i += 2` for value-taking flags; a wrong entry would strand a value
    # in argv as a positional.
    assert all(isinstance(v, bool) for v in cli_contract.HELP_GATED_FLAGS.values())


# --------------------------------------------------------------------------- #
# The read-only tool allowlist
# --------------------------------------------------------------------------- #
def test_the_read_only_tools_exclude_every_write_and_shell_tool():
    forbidden = {"Bash", "Write", "Edit", "MultiEdit", "Shell", "Task"}
    assert not set(cli_contract.READ_ONLY_AGENT_TOOLS) & forbidden


def test_the_read_only_tools_are_not_empty():
    # An empty list would be trivially "safe" but useless — a consult could read nothing.
    assert cli_contract.READ_ONLY_AGENT_TOOLS


# --------------------------------------------------------------------------- #
# argv bound
# --------------------------------------------------------------------------- #
def test_the_argv_bound_is_far_below_the_observed_crash_point():
    """Observed: ~950k chars works, and past it kimi dies with a Node RangeError (exit 7)
    rather than erroring cleanly. The bound is for a short pointer prompt, so it should sit
    orders of magnitude below the cliff."""
    assert cli_contract.MAX_ARGV_PROMPT_CHARS < 100_000


# --------------------------------------------------------------------------- #
# Failure signatures — all strings captured from kimi-code 0.35.0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "error: unknown option '--output-format'",
        "error: option '-p, --prompt <prompt>' argument missing",
        "error: Cannot combine --prompt with --yolo.",
        "error: Cannot use --session without an id in prompt mode.",
        "Output format is only supported in prompt mode.",
        "error: unknown command 'frobnicate'",
    ],
)
def test_is_contract_drift_true(text):
    assert cli_contract.is_contract_drift(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "the model returned an unhelpful answer",
        "Read the file and continue",
        'Model "nope" is not configured in config.toml.',  # the caller's argument, not drift
    ],
)
def test_is_contract_drift_false(text):
    assert not cli_contract.is_contract_drift(text)


def test_invalid_model_matches_the_captured_message():
    blob = (
        'error: failed to run prompt: Model "nonexistent-model" is not configured in config.toml.'
    )
    assert cli_contract.is_invalid_model(blob)


def test_invalid_model_matches_the_unresolved_default_model_message():
    """The second captured invalid-model phrasing, from kimi-code 0.39.1.

    Emitted when config.toml's `default_model` names an alias with no `[models."..."]`
    section: kimi fails while resolving the default, before any -m override applies.
    Captured verbatim (docs/kimi-help/0.39.1/M0-FINDINGS.md); without it the failure
    classified as neither invalid_model nor drift and surfaced as a generic error.
    """
    blob = (
        "error: failed to run prompt: model runpod/kimi-k3 "
        "does not resolve to a configured provider"
    )
    assert cli_contract.is_invalid_model(blob)


def test_unresolved_default_model_is_not_reported_as_contract_drift():
    """It is the user's config, not an upstream change — misreporting it as drift would
    tell the operator to file an upgrade bug instead of fixing default_model."""
    blob = (
        "error: failed to run prompt: model runpod/kimi-k3 "
        "does not resolve to a configured provider"
    )
    assert not cli_contract.is_contract_drift(blob)


def test_invalid_model_does_not_match_unrelated_model_talk():
    assert not cli_contract.is_invalid_model("the model is configured and working")


def test_invalid_model_does_not_match_a_successful_provider_resolution():
    """Guard the widened pattern: "resolve"/"provider" prose alone must not trip it."""
    assert not cli_contract.is_invalid_model("model runpod/kimi-k3 resolved to provider runpod")


@pytest.mark.parametrize(
    "text",
    ["401 Unauthorized", "invalid_api_key", "authentication failed", "run `kimi login`"],
)
def test_is_auth_failure_true(text):
    assert cli_contract.is_auth_failure(text)


@pytest.mark.parametrize("text", ["", "everything is fine", "logged in as someone"])
def test_is_auth_failure_false(text):
    assert not cli_contract.is_auth_failure(text)


@pytest.mark.parametrize("text", ["429 Too Many Requests", "rate limit exceeded", "quota exceeded"])
def test_is_rate_limited_true(text):
    assert cli_contract.is_rate_limited(text)


@pytest.mark.parametrize("text", ["", "all good", "rate of progress is fine"])
def test_is_rate_limited_false(text):
    assert not cli_contract.is_rate_limited(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("retry-after: 30", 30_000),
        ("retry-after: 500ms", 500),
        ("try again in 2 minutes", 120_000),
        ("retry-after: 0", 0),  # 'retry now' must survive, not collapse to the default
        ("nothing useful here", None),
    ],
)
def test_parse_retry_after_ms(text, expected):
    assert cli_contract.parse_retry_after_ms(text) == expected


def test_parse_retry_after_distinguishes_zero_from_absent():
    """A falsey check would coalesce a legitimate 'retry now' into the 60s default."""
    assert cli_contract.parse_retry_after_ms("retry-after: 0") is not None
    assert cli_contract.parse_retry_after_ms("") is None


# --------------------------------------------------------------------------- #
# Versions and models
# --------------------------------------------------------------------------- #
def test_supported_versions_tracks_the_verified_release():
    assert (0, 35) in cli_contract.SUPPORTED_VERSIONS


def test_supported_versions_includes_the_reverified_0_39_release():
    """0.39.1 was re-verified probe-by-probe; captures in docs/kimi-help/0.39.1/."""
    assert (0, 39) in cli_contract.SUPPORTED_VERSIONS


def test_model_slug_pattern_accepts_a_real_alias():
    # kimi aliases are provider-prefixed and contain a slash.
    assert cli_contract.MODEL_SLUG_PATTERN.match("acme/model-one")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 200, "semi;colon"])
def test_model_slug_pattern_rejects_junk(bad):
    assert not cli_contract.MODEL_SLUG_PATTERN.match(bad)


def test_no_bundled_model_slugs():
    """`-m` takes an alias from the user's own config.toml, so a bundled list would be
    fiction; the catalog is read live instead."""
    assert cli_contract.KNOWN_MODEL_SLUGS == ()


# --------------------------------------------------------------------------- #
# Disclosure
# --------------------------------------------------------------------------- #
def test_the_skills_isolation_note_states_the_built_in_limit():
    """Verified: --skills-dir replaces only the user/project dirs; kimi's built-in skills
    always load. The note must not overstate what isolation buys."""
    assert "built-in" in cli_contract.SKILLS_ISOLATION_NOTE.lower()


def test_the_skills_discovery_fact_mentions_out_of_workspace_dirs():
    assert "extra_skill_dirs" in cli_contract.SKILLS_DISCOVERY_FACT


def test_the_full_disclosure_extends_the_short_one():
    assert cli_contract.SKILLS_DISCOVERY_FACT_FULL.startswith(cli_contract.SKILLS_DISCOVERY_FACT)
