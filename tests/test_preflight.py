"""Flag-support probe: parsing, fail-open, missing-flag diagnostics."""

from __future__ import annotations

from pontonier.core.runtime import CommandRun
from pontonier.core.runtime import run_sync_capture as _real_run_sync_capture

from moonbridge import cli_contract, preflight

_HELP = """Usage: kimi [options] [command]

The Starting Point for Next-Gen Agents

Options:
  -V, --version                 output the version number
  -S, --session [id]            Resume a session. With ID: resume that session. Without ID:
                                interactively pick.
  -c, --continue                Continue the previous session for the working directory. (default:
                                false)
  -y, --yolo                    Auto-approve regular tool calls; the agent may still ask questions.
                                (default: false)
  --auto                        Start in auto permission mode: fully autonomous, the agent will not
                                ask questions. (default: false)
  -m, --model <model>           LLM model alias to use for this invocation. Defaults to
                                default_model in config.toml.
  -p, --prompt <prompt>         Run one prompt non-interactively and print the response.
  --output-format <format>      Output format for prompt mode. Defaults to text. (choices: "text",
                                "stream-json")
  --skills-dir <dir>            Load skills from this directory instead of auto-discovered user and
                                project directories. Can be repeated. (default: [])
  --agent <name>                Agent profile to start the new session with. Custom profiles are
                                discovered from agent directories or loaded via --agent-file. Cannot
                                be combined with --session/--continue.
  --agent-file <path>           Load an agent definition from a Markdown file and select it for the
                                new session. Cannot be combined with --session/--continue. (default:
                                [])
  --add-dir <dir>               Add an additional workspace directory for this session. Can be
                                repeated. (default: [])
  --plan                        Start in plan mode. (default: false)
  -h, --help                    Show help.

Commands:
  export [options] [sessionId]  Export a session as a ZIP archive.
  provider                      Manage LLM providers non-interactively.
  acp [options]                 Run kimi-code as an Agent Client Protocol (ACP) server over stdio.
  web [options]                 Run the local Kimi server and open the web UI.
  server                        Deprecated — use `kimi web` instead.
  login                         Authenticate with Kimi Code CLI via the device-code flow.
  doctor                        Validate Kimi Code configuration files.
  vis [options] [sessionId]     Launch the session visualizer in your browser.
  migrate                       Migrate data from a legacy kimi-cli installation into kimi-code.
  upgrade|update                Upgrade Kimi Code to the latest version.

Documentation:        https://moonshotai.github.io/kimi-code/

"""


def _patch_help(monkeypatch, text: str | None):
    def fake(cmd, timeout_seconds):
        if text is None:
            return CommandRun("", preflight.runtime.BINARY_NOT_FOUND, 127, 1, False)
        return CommandRun(text, "", 0, 1, False)

    monkeypatch.setattr(preflight.runtime, "run_sync_capture", fake)


def test_flag_support_parses(monkeypatch):
    _patch_help(monkeypatch, _HELP)
    fs = preflight.flag_support(force=True)
    assert fs.help_parsed
    assert "--model" in fs.supported
    # kimi has no --sandbox; these are the flags this server actually depends on.
    assert "--agent-file" in fs.supported
    assert "--output-format" in fs.supported


def test_is_supported_present(monkeypatch):
    _patch_help(monkeypatch, _HELP)
    fs = preflight.flag_support(force=True)
    assert preflight.is_supported("--model", fs)


def test_is_supported_fail_open_when_probe_fails(monkeypatch):
    _patch_help(monkeypatch, None)
    fs = preflight.flag_support(force=True)
    assert not fs.help_parsed
    # Fail open: unknown flags treated as supported.
    assert preflight.is_supported("--anything", fs)


def test_flag_support_fails_open_when_kimi_is_not_executable(monkeypatch, tmp_path):
    # Spawns for real: the conftest guard would stub this KIMI_BIN as missing. Under
    # pontonier < 0.8.0 the unexecutable binary raised PermissionError here instead.
    binary = tmp_path / "kimi"
    binary.write_text("#!/bin/sh\necho hi\n")
    binary.chmod(0o644)
    monkeypatch.setattr(cli_contract, "KIMI_BIN", str(binary))
    monkeypatch.setattr(preflight.runtime, "run_sync_capture", _real_run_sync_capture)
    fs = preflight.flag_support(force=True)
    assert not fs.help_parsed
    assert preflight.is_supported("--anything", fs)


def test_missing_expected_flags_none_when_all_present(monkeypatch):
    _patch_help(monkeypatch, _HELP)
    fs = preflight.flag_support(force=True)
    assert preflight.missing_expected_flags(fs) == []


def test_missing_expected_flags_detects_gap(monkeypatch):
    _patch_help(monkeypatch, "Run Kimi\n  --json\n  --cd <DIR>\n")
    fs = preflight.flag_support(force=True)
    missing = preflight.missing_expected_flags(fs)
    assert "--agent-file" in missing
    assert all(f in cli_contract.ALWAYS_SEND_FLAGS for f in missing)


def test_missing_expected_flags_empty_on_failed_probe(monkeypatch):
    _patch_help(monkeypatch, None)
    fs = preflight.flag_support(force=True)
    assert preflight.missing_expected_flags(fs) == []


def test_cache_reused(monkeypatch):
    calls = {"n": 0}

    def fake(cmd, timeout_seconds):
        calls["n"] += 1
        return CommandRun(_HELP, "", 0, 1, False)

    monkeypatch.setattr(preflight.runtime, "run_sync_capture", fake)
    preflight.reset_cache()
    preflight.flag_support()
    preflight.flag_support()
    assert calls["n"] == 1  # second call served from cache
