"""The `kimi` CLI adapter: command construction, the file handshake, and classification.

Replaces the Codex-era adapter suite wholesale — the two CLIs share no invocation surface
(no sandbox flag, no stdin prompt, no --output-last-message), so porting those assertions
would have tested a contract that does not exist.

Every expectation here traces to behavior verified against kimi-code 0.35.0; the captures
live in docs/kimi-help/0.35.0/.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from pontonier.core.runtime import CommandRun

from moonbridge import cli_contract, kimi
from moonbridge.preflight import FlagSupport
from moonbridge.schemas import Meta

ALL_FLAGS = FlagSupport(
    supported=frozenset({cli_contract.MODEL_FLAG, cli_contract.SKILLS_DIR_FLAG}),
    help_parsed=True,
)
NO_FLAGS = FlagSupport(supported=frozenset(), help_parsed=True)


def _build(**kwargs):
    params = {
        "cwd": "/repo",
        "sandbox": cli_contract.SANDBOX_WORKSPACE_WRITE,
        "isolation": "inherit",
        "prompt_pointer": "read the prompt file",
        "flag_support": ALL_FLAGS,
    }
    params.update(kwargs)
    return kimi.build_exec_command(**params)


# --------------------------------------------------------------------------- #
# The read-only guarantee
# --------------------------------------------------------------------------- #
def test_read_only_without_an_agent_file_raises_rather_than_degrading():
    """The tools allowlist is the ONLY thing enforcing read-only — a worktree does not
    contain kimi. Emitting a read-only command without it would silently hand a consult an
    unrestricted agent, so this must fail loudly."""
    with pytest.raises(ValueError, match="agent file"):
        _build(sandbox=cli_contract.SANDBOX_READ_ONLY, agent_file_path=None)


def test_read_only_sends_the_agent_file():
    cmd, _ = _build(
        sandbox=cli_contract.SANDBOX_READ_ONLY, agent_file_path="/tmp/wt/.moonbridge/a.md"
    )
    assert cmd[cmd.index(cli_contract.AGENT_FILE_FLAG) + 1] == "/tmp/wt/.moonbridge/a.md"


def test_the_agent_file_flag_is_never_help_gated_away():
    """A guarantee-bearing flag must survive even when `kimi --help` advertises nothing;
    if it could be dropped, read-only would silently downgrade to unrestricted."""
    cmd, dropped = _build(
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        agent_file_path="/tmp/a.md",
        flag_support=NO_FLAGS,
    )
    assert cli_contract.AGENT_FILE_FLAG in cmd
    assert cli_contract.AGENT_FILE_FLAG not in dropped
    assert cli_contract.AGENT_FILE_FLAG not in cli_contract.HELP_GATED_FLAGS


def test_the_read_only_agent_grants_no_write_or_shell_tool():
    doc = kimi.read_only_agent_document()
    tools_block = doc.split("tools:")[1].split("---")[0]
    for forbidden in ("Bash", "Write", "Edit", "MultiEdit", "Shell"):
        assert forbidden not in tools_block
    assert set(cli_contract.READ_ONLY_AGENT_TOOLS) == {
        line.strip("- ").strip() for line in tools_block.strip().splitlines()
    }


def test_the_read_only_agent_document_is_valid_frontmatter():
    doc = kimi.read_only_agent_document()
    assert doc.startswith("---\n")
    assert doc.count("---") >= 2
    assert f"name: {cli_contract.READ_ONLY_AGENT_NAME}" in doc


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #
def test_the_output_format_is_always_stream_json():
    cmd, _ = _build()
    assert cmd[cmd.index(cli_contract.OUTPUT_FORMAT_FLAG) + 1] == cli_contract.OUTPUT_FORMAT_JSON


def test_the_prompt_rides_argv_not_stdin():
    # kimi ignores stdin entirely (`echo x | kimi -p` errors on a missing argument).
    cmd, _ = _build(prompt_pointer="POINTER")
    assert cmd[cmd.index(cli_contract.PROMPT_FLAG) + 1] == "POINTER"
    assert "-" not in cmd[-1:]


def test_an_oversized_argv_prompt_is_refused():
    """argv is capped near 950k chars and kimi CRASHES past it with a Node RangeError
    rather than erroring cleanly, so the caller must never inline gathered context."""
    with pytest.raises(ValueError, match="handshake prompt file"):
        _build(prompt_pointer="x" * (cli_contract.MAX_ARGV_PROMPT_CHARS + 1))


def test_add_dir_is_never_sent():
    # --add-dir would grant kimi a workspace root outside the throwaway worktree.
    cmd, _ = _build()
    assert cli_contract.ADD_DIR_FLAG not in cmd


def test_prompt_mode_incompatible_flags_are_never_sent():
    cmd, _ = _build()
    for flag in cli_contract.PROMPT_MODE_INCOMPATIBLE_FLAGS:
        assert flag not in cmd


def test_model_is_help_gated_and_reported_when_dropped():
    cmd, dropped = _build(model="acme/model-one", flag_support=NO_FLAGS)
    assert cli_contract.MODEL_FLAG not in cmd
    assert dropped == [cli_contract.MODEL_FLAG]


def test_a_gated_flag_drops_its_value_too():
    """Dropping the flag but leaving its value would make the value a positional argument."""
    cmd, _ = _build(model="acme/model-one", flag_support=NO_FLAGS)
    assert "acme/model-one" not in cmd


def test_reconcile_dropped_model_clears_reported_provenance():
    meta = Meta(
        cwd="/x",
        tier="consult",
        sandbox="read-only",
        isolation="inherit",
        timeout_seconds=60,
        elapsed_ms=0,
    )
    meta.model = "acme/model-one"
    result = kimi.KimiRunResult(
        run=CommandRun("", "", 0, 1, False),
        last_message=None,
        dropped_flags=[cli_contract.MODEL_FLAG],
    )
    kimi.reconcile_dropped_model(result, meta)
    assert meta.model is None, "meta claimed a model the run never used"


def test_extra_args_are_appended_after_gating():
    cmd, _ = _build(extra_args=("--some-operator-flag",), flag_support=NO_FLAGS)
    assert cmd[-1] == "--some-operator-flag"


# --------------------------------------------------------------------------- #
# Reasoning effort rides the environment, not argv
# --------------------------------------------------------------------------- #
def test_reasoning_effort_is_set_in_the_environment():
    env = kimi.build_run_env("high")
    assert env[cli_contract.REASONING_EFFORT_ENV] == "high"


def test_no_effort_leaves_the_variable_unset():
    env = kimi.build_run_env(None)
    assert cli_contract.REASONING_EFFORT_ENV not in env


def test_the_output_format_env_is_pinned():
    """A user's KIMI_MODEL_OUTPUT_FORMAT=text would otherwise strip the event stream out
    from under the parser, leaving no answer channel at all for a read-only run."""
    env = kimi.build_run_env(None)
    assert env[cli_contract.MODEL_OUTPUT_FORMAT_ENV] == cli_contract.OUTPUT_FORMAT_JSON


# --------------------------------------------------------------------------- #
# The prompt/answer handshake
# --------------------------------------------------------------------------- #
def test_handshake_writes_the_prompt_and_agent_for_a_read_only_run(tmp_path):
    paths = kimi.write_handshake(str(tmp_path / "h"), "THE PROMPT", read_only=True)
    assert Path(paths["prompt"]).read_text() == "THE PROMPT"
    assert "Read" in Path(paths["agent"]).read_text()
    # A read-only agent has no Write tool, so offering it an answer file would be a lie.
    assert "answer" not in paths


def test_handshake_offers_an_answer_file_only_to_the_propose_tier(tmp_path):
    paths = kimi.write_handshake(str(tmp_path / "h"), "P", read_only=False)
    assert paths["answer"].endswith(cli_contract.ANSWER_FILE_NAME)
    assert "agent" not in paths


def test_the_handshake_dir_is_outside_any_repository(tmp_path):
    """It must not live in the worktree. The worktree is seeded from repository content, so
    a repo tracking `.moonbridge` as a symlink would redirect these writes outside the
    tree — and an agent file redirected to /dev/null could void the read-only guarantee."""
    d = kimi.create_handshake_dir()
    try:
        assert not Path(d).is_relative_to(tmp_path)
        assert Path(d).is_dir()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_handshake_refuses_to_follow_a_planted_symlink(tmp_path):
    """Regression for the escape Codex found: write_text followed a symlink at the target,
    so a tracked `.moonbridge` symlink made the SERVER write outside the worktree."""
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    d = tmp_path / "handshake"
    d.mkdir()
    (d / cli_contract.PROMPT_FILE_NAME).symlink_to(victim)
    with pytest.raises(OSError):
        kimi.write_handshake(str(d), "ATTACKER CONTROLLED", read_only=True)
    assert victim.read_text() == "ORIGINAL", "the server wrote through a symlink"


def test_handshake_files_are_private(tmp_path):
    paths = kimi.write_handshake(str(tmp_path / "h"), "P", read_only=True)
    assert Path(paths["prompt"]).stat().st_mode & 0o077 == 0


def test_the_pointer_prompt_stays_small_enough_for_argv(tmp_path):
    paths = kimi.write_handshake(str(tmp_path / "h"), "P", read_only=False)
    for read_only in (True, False):
        assert len(kimi.build_prompt_pointer(paths, read_only=read_only)) < 2000


def test_the_read_only_pointer_does_not_ask_for_an_answer_file(tmp_path):
    paths = kimi.write_handshake(str(tmp_path / "h"), "P", read_only=True)
    pointer = kimi.build_prompt_pointer(paths, read_only=True)
    assert cli_contract.ANSWER_FILE_NAME not in pointer


# --------------------------------------------------------------------------- #
# The answer file is model-controlled at read time
# --------------------------------------------------------------------------- #
def test_a_symlinked_answer_file_is_not_followed(tmp_path):
    """A full-tool delegate can replace answer.md with a symlink to a host file, which
    would launder arbitrary content into the result envelope."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    answer = tmp_path / "answer.md"
    answer.symlink_to(secret)
    assert kimi._read_answer_file(str(answer)) == ""


def test_an_oversized_answer_file_is_refused(tmp_path):
    answer = tmp_path / "answer.md"
    answer.write_text("x" * (kimi.MAX_ANSWER_BYTES + 10))
    assert kimi._read_answer_file(str(answer)) == ""


def test_a_fifo_answer_file_does_not_block(tmp_path):
    """A FIFO would hang the read — and with it the server — indefinitely."""
    fifo = tmp_path / "answer.md"
    os.mkfifo(fifo)
    assert kimi._read_answer_file(str(fifo)) == ""


def test_a_normal_answer_file_still_reads(tmp_path):
    # Negative control: the hardening above must not break the ordinary path.
    answer = tmp_path / "answer.md"
    answer.write_text("  the answer  ")
    assert kimi._read_answer_file(str(answer)) == "the answer"


# --------------------------------------------------------------------------- #
# Answer resolution
# --------------------------------------------------------------------------- #
STREAM = (
    '{"role":"meta","type":"system.version","version":"0.35.0"}\n'
    '{"role":"assistant","tool_calls":[{"type":"function","id":"Read:0",'
    '"function":{"name":"Read","arguments":"{}"}}]}\n'
    '{"role":"tool","tool_call_id":"Read:0","content":"file contents"}\n'
    '{"role":"assistant","content":"the streamed answer"}\n'
    '{"role":"meta","type":"session.resume_hint","session_id":"session_x"}\n'
)


def test_the_answer_file_wins_when_present(tmp_path):
    answer = tmp_path / "answer.md"
    answer.write_text("the file answer")
    assert kimi._resolve_answer(str(answer), STREAM) == "the file answer"


def test_the_stream_is_the_fallback_when_no_answer_file(tmp_path):
    assert kimi._resolve_answer(None, STREAM) == "the streamed answer"


def test_an_empty_answer_file_falls_back_to_the_stream(tmp_path):
    answer = tmp_path / "answer.md"
    answer.write_text("   \n")
    assert kimi._resolve_answer(str(answer), STREAM) == "the streamed answer"


def test_a_missing_answer_file_falls_back_to_the_stream(tmp_path):
    assert kimi._resolve_answer(str(tmp_path / "nope.md"), STREAM) == "the streamed answer"


def test_no_answer_anywhere_returns_none():
    """None makes the caller fail loudly; a placeholder string would launder an empty run
    into a usable-looking result."""
    only_tools = '{"role":"assistant","tool_calls":[{"type":"function","id":"a"}]}\n'
    assert kimi._resolve_answer(None, only_tools) is None


def test_tool_call_ids_are_not_used_as_correlation_keys():
    """Verified on 0.35.0: `tool_call_id` repeats within one run ("Read:0" appeared twice),
    so nothing may key on it."""
    duplicated = STREAM + '{"role":"tool","tool_call_id":"Read:0","content":"again"}\n'
    assert kimi._resolve_answer(None, duplicated) == "the streamed answer"


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #
def _run(stdout="", stderr="", exit_code=1, timed_out=False):
    return CommandRun(stdout, stderr, exit_code, 10, timed_out)


def test_missing_binary():
    from pontonier.core.runtime import BINARY_NOT_FOUND

    err = kimi.classify_failure(_run(stderr=BINARY_NOT_FOUND, exit_code=127))
    assert err.code == "kimi_not_found"


def test_timeout():
    err = kimi.classify_failure(_run(timed_out=True))
    assert err.code == "timeout"


def test_an_unknown_model_alias_is_the_callers_argument_not_drift():
    # Captured verbatim from kimi-code 0.35.0.
    blob = 'error: failed to run prompt: Model "nope" is not configured in config.toml.'
    err = kimi.classify_failure(_run(stderr=blob))
    assert err.code == "invalid_model"


def test_an_unresolvable_default_model_does_not_blame_the_model_argument():
    """The two invalid-model causes need different repair guidance.

    Captured on 0.39.1: this message fires while kimi resolves config.toml's
    `default_model`, BEFORE any -m override applies — so it reaches the classifier on a
    run whose `model` argument was perfectly valid. Blaming that argument sends the caller
    into a repair loop: the generic hint says "omit model to use the configured
    default_model", which is precisely the thing that is broken. The error must name
    config.toml's default_model and must NOT pin the blame on the `model` field.
    """
    blob = (
        "error: failed to run prompt: model runpod/kimi-k3 "
        "does not resolve to a configured provider"
    )
    err = kimi.classify_failure(_run(stderr=blob))
    assert err.code == "invalid_model"
    assert "default_model" in err.message
    # The caller's `model` argument may have been valid, so it must not be fingered.
    assert err.details is None or err.details.field != "model"


def test_an_unknown_alias_still_blames_the_model_argument():
    """The other cause is unchanged: there the `model` argument really is at fault."""
    blob = 'error: failed to run prompt: Model "nope" is not configured in config.toml.'
    err = kimi.classify_failure(_run(stderr=blob))
    assert err.code == "invalid_model"
    assert err.details is not None and err.details.field == "model"


def test_an_unknown_option_is_contract_drift():
    err = kimi.classify_failure(_run(stderr="error: unknown option '--output-format'"))
    assert err.code == "cli_contract_changed"


def test_prompt_mode_incompatibility_is_drift():
    err = kimi.classify_failure(_run(stderr="error: Cannot combine --prompt with --yolo."))
    assert err.code == "cli_contract_changed"


def test_auth_failure():
    err = kimi.classify_failure(_run(stderr="401 Unauthorized"))
    assert err.code == "kimi_auth_required"


def test_rate_limit_with_retry_after():
    err = kimi.classify_failure(_run(stderr="429 rate limit exceeded, retry-after: 30"))
    assert err.code == "kimi_rate_limited"
    assert err.retry_after_ms == 30_000


def test_rate_limit_without_a_delay_uses_the_default():
    err = kimi.classify_failure(_run(stderr="429 too many requests"))
    assert err.retry_after_ms == cli_contract.RATE_LIMIT_DEFAULT_BACKOFF_MS


def test_a_zero_retry_after_is_preserved():
    """'retry now' is a real answer; coalescing 0 to the default with a falsey check would
    silently delay a retry by a minute."""
    err = kimi.classify_failure(_run(stderr="429 rate limited, retry-after: 0"))
    assert err.retry_after_ms == 0


def test_drift_is_checked_before_rate_limiting():
    """A genuine contract change must never be masked as a retryable transient."""
    both = "error: unknown option '--output-format'\n429 rate limit"
    assert kimi.classify_failure(_run(stderr=both)).code == "cli_contract_changed"


def test_drift_is_checked_before_invalid_model():
    """Drift outranks invalid_model, matching pontonier's shared precedence order (#11).

    Neither captured invalid-model message trips a drift pattern (verified against both
    0.35.0 and 0.39.1 captures), so this only matters when a run carries BOTH — and then
    the contract change is the signal that must stay loud.
    """
    both = (
        "error: unknown option '--output-format'\n"
        'error: failed to run prompt: Model "nope" is not configured in config.toml.'
    )
    assert kimi.classify_failure(_run(stderr=both)).code == "cli_contract_changed"


def test_an_unrecognized_failure_is_a_bounded_nonzero_exit():
    err = kimi.classify_failure(_run(stderr="something went sideways", exit_code=3))
    assert err.code == "nonzero_exit"
    assert "3" in err.message


def test_a_secret_in_the_failure_text_is_redacted():
    blob = f"boom api_key={'A' * 32} while running"
    err = kimi.classify_failure(_run(stderr=blob))
    assert "A" * 32 not in err.message


def test_there_is_no_backend_effort_rejection_branch():
    """kimi SILENTLY IGNORES an unrecognized reasoning effort (verified: exit 0, answers at
    the model's default). A classifier branch would be dead code claiming a check that
    never runs; the effort is validated locally before spend instead. If a future kimi
    starts rejecting efforts, this test should be replaced, not deleted."""
    blob = "[reasoning.effort] [invalid_enum_value] Invalid value: 'bogus'."
    err = kimi.classify_failure(_run(stderr=blob), reasoning_effort="bogus")
    assert err.code != "invalid_reasoning_effort"


# --------------------------------------------------------------------------- #
# Free probes
# --------------------------------------------------------------------------- #
def test_provider_count_parses_the_real_payload_shape():
    payload = json.dumps(
        {
            "providers": {"acme": {"baseUrl": "https://x", "apiKey": "sk-secret"}},
            "models": {"acme/model-one": {}},
        }
    )
    assert kimi._provider_count(payload) == 1


def test_provider_count_is_none_on_garbage():
    assert kimi._provider_count("not json") is None


def test_login_status_detail_never_echoes_provider_details(monkeypatch):
    """`kimi provider list --json` carries apiKey in plaintext and baseUrl, which can name
    private infrastructure. The status detail is derived from the COUNT alone."""
    payload = json.dumps(
        {
            "providers": {"acme": {"baseUrl": "https://internal.example", "apiKey": "sk-secret"}},
            "models": {"a/b": {}},
        }
    )
    monkeypatch.setattr(
        "pontonier.core.runtime.run_sync_capture",
        lambda *a, **k: CommandRun(payload, "", 0, 5, False),
    )
    configured, detail = kimi.login_status()
    assert configured is True
    assert "sk-secret" not in (detail or "")
    assert "internal.example" not in (detail or "")


def test_login_status_is_indeterminate_when_the_probe_cannot_run(monkeypatch):
    from pontonier.core.runtime import BINARY_NOT_FOUND

    monkeypatch.setattr(
        "pontonier.core.runtime.run_sync_capture",
        lambda *a, **k: CommandRun("", BINARY_NOT_FOUND, 127, 1, False),
    )
    assert kimi.login_status() == (None, None)


def test_kimi_version_returns_none_when_absent(monkeypatch):
    from pontonier.core.runtime import BINARY_NOT_FOUND

    monkeypatch.setattr(
        "pontonier.core.runtime.run_sync_capture",
        lambda *a, **k: CommandRun("", BINARY_NOT_FOUND, 127, 1, False),
    )
    assert kimi.kimi_version() is None


async def test_run_kimi_exec_surfaces_help_gate_drops_from_the_adapter(monkeypatch, tmp_path):
    """The dropped-flags channel rides PreparedRun.dropped_flags since the re-plumb.
    A wiring bug (e.g. always-empty) would silently kill compat warnings and
    model-provenance reconciliation, so a real gated drop is pinned end to end."""
    from pontonier.core.runtime import CommandRun

    from moonbridge import preflight

    async def fake_run_async(
        cmd,
        *,
        cwd,
        timeout_seconds,
        stdin_text=None,
        env=None,
        on_stdout_line=None,
        max_output_bytes=None,
        orphan_marker=None,
    ):
        assert cli_contract.MODEL_FLAG not in cmd  # the gate really dropped it from argv
        return CommandRun("", "", 0, 1, False)

    monkeypatch.setattr(kimi.runtime, "run_async", fake_run_async)
    monkeypatch.setattr(preflight, "flag_support", lambda force=False: NO_FLAGS)
    result = await kimi.run_kimi_exec(
        "q",
        kind="consult",
        cwd=str(tmp_path),
        sandbox=cli_contract.SANDBOX_READ_ONLY,
        isolation="inherit",
        timeout_seconds=10,
        model="acme/model-one",
    )
    assert result.dropped_flags == [cli_contract.MODEL_FLAG]
