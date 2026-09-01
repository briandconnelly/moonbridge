"""Build and run the `kimi` CLI invocation; probe version/providers; classify failures.

Shape mirrors the Codex adapter this project forked from, but the mechanics differ — see
cli_contract for the three differences that drive everything (no sandbox, argv-only prompt,
no --output-last-message).

The two guarantees this module is responsible for:

* **Read-only tiers really are read-only.** A read-only run is given an --agent-file whose
  `tools:` list omits Bash and Write. That file is generated per run and is the ONLY thing
  standing between a consult and an unrestricted agent — the worktree is not a boundary
  (see cli_contract). `build_exec_command` therefore refuses to build a read-only command
  without it rather than silently emitting an unrestricted one.
* **Gathered context never rides argv.** The prompt is written to a handshake file
  OUTSIDE the workspace and argv carries a short pointer, because kimi ignores stdin
  and crashes past ~950k argv chars.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING

from pontonier.backend.protocol import RunRequest
from pontonier.core import redaction, runtime

from moonbridge import cli_contract, config, normalize, preflight
from moonbridge.errors import make_error
from moonbridge.schemas import ErrorDetail

if TYPE_CHECKING:
    from collections.abc import Callable

    from pontonier.core.runtime import CommandRun

    from moonbridge.preflight import FlagSupport
    from moonbridge.schemas import ErrorInfo, Meta


@dataclass
class KimiRunResult:
    """Outcome of a `kimi -p` run: the raw process result plus the extracted final
    assistant message and the stream-json event text (for tolerant metadata parsing)."""

    run: CommandRun
    last_message: str | None
    events: str = ""
    dropped_flags: list[str] = field(default_factory=list)


def _gate_optional(tokens: list[str], fs: FlagSupport) -> tuple[list[str], list[str]]:
    """Drop any HELP_GATED flag (and its value) the installed `kimi` does not advertise.

    Returns (kept_tokens, dropped_flags). ALWAYS_SEND flags are never in HELP_GATED_FLAGS,
    so they always survive and a rejection surfaces as cli_contract_changed.
    """
    kept: list[str] = []
    dropped: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        takes_value = cli_contract.HELP_GATED_FLAGS.get(token)
        if takes_value is not None and not preflight.is_supported(token, fs):
            dropped.append(token)
            i += 2 if takes_value else 1
            continue
        kept.append(token)
        i += 1
    return kept, dropped


def reconcile_dropped_model(result: KimiRunResult, meta: Meta) -> None:
    """Reset meta.model when -m was dropped by help-gating, so reported provenance matches
    the model actually used rather than the unfulfilled request. The drop is already
    surfaced in meta.compat_warnings."""
    if cli_contract.MODEL_FLAG in result.dropped_flags:
        meta.model = None


def read_only_agent_document() -> str:
    """The generated agent profile that makes a run read-only.

    Its `tools:` list is guarantee-bearing (cli_contract.READ_ONLY_AGENT_TOOLS): Bash and
    Write are absent, so the model has no way to mutate anything. Verified on kimi-code
    0.35.0 — an agent declaring these three reported exactly these three.
    """
    tools = "\n".join(f"  - {t}" for t in cli_contract.READ_ONLY_AGENT_TOOLS)
    return (
        "---\n"
        f"name: {cli_contract.READ_ONLY_AGENT_NAME}\n"
        "description: Read-only consultant with no shell or write tools.\n"
        "tools:\n"
        f"{tools}\n"
        "---\n"
        "You are a read-only consultant. Answer using only the tools you have. "
        "You cannot modify files, and you must not ask for permission to do so.\n"
    )


def build_exec_command(
    *,
    cwd: str,
    sandbox: str,
    isolation: str,
    prompt_pointer: str,
    model: str | None = None,
    agent_file_path: str | None = None,
    skills_dir: str | None = None,
    extra_args: tuple[str, ...] = (),
    flag_support: FlagSupport | None = None,
) -> tuple[list[str], list[str]]:
    """Build the `kimi -p` invocation. Returns (cmd, dropped_optional_flags).

    `prompt_pointer` is the SHORT argv prompt — typically an instruction to read the
    handshake prompt file. Gathered context must never be inlined here: kimi ignores stdin
    and dies with a Node RangeError past ~950k argv chars, so the caller writes the real
    prompt to a handshake file outside the workspace.

    `agent_file_path` is REQUIRED when sandbox is read-only. Without it the run would hold
    the full tool set, and since the worktree is not a boundary that would silently turn a
    read-only consult into an unrestricted agent — so this raises rather than degrades.
    """
    if sandbox == cli_contract.SANDBOX_READ_ONLY and not agent_file_path:
        raise ValueError(
            "read-only runs require an agent file: the tools allowlist is the only thing "
            "enforcing read-only, and a worktree does not contain kimi"
        )
    if len(prompt_pointer) > cli_contract.MAX_ARGV_PROMPT_CHARS:
        raise ValueError(
            f"argv prompt exceeds {cli_contract.MAX_ARGV_PROMPT_CHARS} chars; "
            "write it to the handshake prompt file instead"
        )

    # cwd is not a kimi flag (there is no --cd); the caller sets it on the subprocess.
    # Accepted here so the command builder and the runner agree on one call shape.
    _ = cwd
    fs = flag_support if flag_support is not None else preflight.flag_support()
    tokens = [cli_contract.KIMI_BIN, *cli_contract.EXEC_SUBCOMMAND]
    tokens += [cli_contract.PROMPT_FLAG, prompt_pointer]
    tokens += [cli_contract.OUTPUT_FORMAT_FLAG, cli_contract.OUTPUT_FORMAT_JSON]
    if agent_file_path:
        tokens += [cli_contract.AGENT_FILE_FLAG, agent_file_path]
    if skills_dir:
        tokens += [cli_contract.SKILLS_DIR_FLAG, skills_dir]
    if model:
        tokens += [cli_contract.MODEL_FLAG, model]
    # Deliberately never sent: --add-dir (would defeat worktree isolation) and the
    # prompt-mode-incompatible flags (-y/--auto/--plan/--session/--continue), which kimi
    # rejects outright. isolation is realized by skills_dir + the agent file, not by flags
    # of its own — kimi has no --ignore-user-config equivalent.
    _ = isolation
    cmd, dropped = _gate_optional(tokens, fs)
    cmd += list(extra_args)
    return cmd, dropped


def build_run_env(reasoning_effort: str | None) -> dict[str, str]:
    """Child environment for a run.

    Reasoning effort rides an env var (kimi has no flag for it), so it is set here rather
    than in argv. Everything else is inherited: the provider credentials in the user's
    config are what authenticate the run.
    """
    env = dict(os.environ)
    if reasoning_effort is not None:
        env[cli_contract.REASONING_EFFORT_ENV] = reasoning_effort
    # Pin the output format so a user's KIMI_MODEL_OUTPUT_FORMAT=text cannot silently
    # strip the event stream out from under the parser.
    env[cli_contract.MODEL_OUTPUT_FORMAT_ENV] = cli_contract.OUTPUT_FORMAT_JSON
    return env


def _write_exclusive(path: Path, text: str) -> None:
    """Create `path` and write `text`, refusing to follow a symlink or reuse an existing file.

    O_EXCL|O_NOFOLLOW is the whole point: the handshake directory is created fresh by this
    process, so anything already at the target is an attacker's plant, not our file.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def create_handshake_dir() -> str:
    """A fresh, private, server-owned directory for one run's handshake files.

    Deliberately NOT inside the worktree. The worktree is seeded from repository content,
    so a repo that tracks `.moonbridge` as a symlink would redirect these writes outside
    the tree — verified: with such a symlink checked out, the prompt and agent files landed
    in the symlink's target. Worse, an agent file redirected to /dev/null would present kimi
    with an empty profile, which may silently fall back to the unrestricted default and
    void the read-only guarantee.

    Placing the directory outside the repository removes repository control over the path
    entirely. kimi reads the files by absolute path, which works because its Read tool
    accepts absolute paths (verified on 0.35.0).
    """
    return tempfile.mkdtemp(prefix="moonbridge-handshake-")


def write_handshake(run_dir: str, prompt_text: str, *, read_only: bool) -> dict[str, str]:
    """Write one run's handshake files into `run_dir` and return their ABSOLUTE paths.

    `run_dir` must be a server-owned directory from `create_handshake_dir`, never a path
    under the worktree — see that function for the symlink attack this avoids.

    Returns keys: prompt (always), answer (propose tier only), agent (read-only tier only).
    The answer file is only meaningful when the run has a Write tool, so it is not offered
    to a read-only run — that tier recovers its answer from the event stream instead.
    """
    base = Path(run_dir)
    base.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    prompt_path = base / cli_contract.PROMPT_FILE_NAME
    _write_exclusive(prompt_path, prompt_text)
    paths["prompt"] = str(prompt_path)

    if read_only:
        agent_path = base / "readonly-agent.md"
        _write_exclusive(agent_path, read_only_agent_document())
        # Read the profile back before launching. The agent file is the ONLY thing enforcing
        # read-only, so shipping one whose bytes we have not confirmed would make the
        # guarantee unverified at exactly the moment it matters.
        if agent_path.read_text(encoding="utf-8") != read_only_agent_document():
            raise ValueError("read-only agent file did not read back as written")
        paths["agent"] = str(agent_path)
    else:
        paths["answer"] = str(base / cli_contract.ANSWER_FILE_NAME)
    return paths


def build_prompt_pointer(paths: dict[str, str], *, read_only: bool) -> str:
    """The short argv prompt pointing kimi at the handshake files by absolute path."""
    prompt = paths["prompt"]
    if read_only:
        return (
            f"Read the file {prompt} and follow it exactly. "
            "Reply with your answer as your final message."
        )
    return (
        f"Read the file {prompt} and follow it exactly. "
        f"When you are done, write your final answer to {paths['answer']}."
    )


async def run_kimi_exec(
    prompt: str,
    *,
    kind: str,
    cwd: str,
    sandbox: str,
    isolation: str,
    timeout_seconds: int,
    model: str | None = None,
    reasoning_effort: str | None = None,
    output_schema: dict | None = None,
    on_event: Callable[[str], None] | None = None,
) -> KimiRunResult:
    """Run `kimi -p` in `cwd` through the KimiBackend adapter lifecycle.

    `cwd` MUST already be an isolated worktree: every tier runs in one, because kimi has
    no sandbox. The adapter's `prepare()` stages everything — the out-of-workspace
    handshake dir (prompt file, read-only agent profile or answer file), the argv
    pointer, the effort environment, the prompt-appended schema instruction, help-gate
    drops — and tears it down on context exit, so the answer file is resolved inside the
    context. This function owns only the execution step: `runtime.run_async` with this
    bridge's timeout/byte caps, event streaming, and the orphan-sweep marker. `kind` is
    the canonical verb ("consult" | "review_changes" | "delegate").
    """
    # Runtime import: backend.py imports this module's builders, so a top-level
    # import here would be a cycle. Cached by the import system after first use.
    from moonbridge.backend import BACKEND  # noqa: PLC0415

    request = RunRequest(
        kind=kind,
        prompt=prompt,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        schema=output_schema,
        model=model,
        reasoning_effort=reasoning_effort,
        access=sandbox,
        isolation=isolation,
    )
    async with BACKEND.prepare(request) as prepared:
        run = await runtime.run_async(
            list(prepared.argv),
            cwd=prepared.cwd,
            timeout_seconds=timeout_seconds,
            stdin_text=prepared.stdin_text,
            on_stdout_line=on_event,
            max_output_bytes=config.max_output_bytes(),
            env=prepared.env,
            orphan_marker=prepared.orphan_marker,
        )
        # Inside the context on purpose: the handshake dir (and with it the answer
        # file) is torn down when prepare() exits.
        last_message = _resolve_answer(prepared.artifact_paths.get("answer"), run.stdout)
    return KimiRunResult(
        run=run,
        last_message=last_message,
        events=run.stdout,
        dropped_flags=list(prepared.dropped_flags),
    )


# The answer file is written by a full-tool agent, so its path is model-controlled at the
# moment we read it. Bound the read: a symlink to /dev/zero, a FIFO, or a huge file would
# otherwise hang the server or exhaust memory, and a symlink to a host file would launder
# arbitrary content into the result.
MAX_ANSWER_BYTES = 1_000_000


def _read_answer_file(answer_path: str) -> str:
    """Read the answer file safely, or return "" if it is anything but a plain small file."""
    try:
        # O_NONBLOCK is load-bearing, not defensive: opening a FIFO for reading BLOCKS
        # until a writer appears, which would hang before fstat could reject it — the very
        # denial of service this function exists to prevent. O_NOFOLLOW rejects a
        # substituted symlink.
        fd = os.open(answer_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        # ELOOP (a symlink was substituted), ENOENT, or a permissions problem.
        return ""
    try:
        stat = os.fstat(fd)
        # Regular files only: a FIFO or device would block the read indefinitely.
        if not S_ISREG(stat.st_mode) or stat.st_size > MAX_ANSWER_BYTES:
            return ""
        raw = os.read(fd, MAX_ANSWER_BYTES)
    except OSError:
        return ""
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace").strip()


def _resolve_answer(answer_path: str | None, events: str) -> str | None:
    """Recover the final answer: answer file first (propose tier), event stream otherwise.

    kimi has no --output-last-message, so for a read-only run the stream is the only
    channel. Returning None makes the caller fail loudly rather than report an empty
    success.
    """
    if answer_path:
        text = _read_answer_file(answer_path)
        if text:
            return text
    return normalize.extract_final_message(events)


def kimi_version(timeout_seconds: int = 10) -> str | None:
    """Probe `kimi --version`. Returns the trimmed version string, or None."""
    run = runtime.run_sync_capture(
        [cli_contract.KIMI_BIN, *cli_contract.VERSION_ARGS], timeout_seconds=timeout_seconds
    )
    if run.binary_missing or run.exit_code != 0:
        return None
    return run.stdout.strip() or None


def login_status(timeout_seconds: int = 10) -> tuple[bool | None, str | None]:
    """Probe provider configuration without a model call.

    kimi has no `login status`. Readiness is instead "at least one provider is configured",
    read from `kimi provider list --json`. Returns (configured, detail); configured is None
    when the probe could not run.

    The detail is NON-identifying and derived only from the provider COUNT — never the raw
    output, which carries base URLs and may carry an API key.
    """
    run = runtime.run_sync_capture(
        [cli_contract.KIMI_BIN, *cli_contract.PROVIDER_LIST_ARGS],
        timeout_seconds=timeout_seconds,
    )
    if run.binary_missing or run.timed_out:
        return None, None
    if run.exit_code != 0:
        return False, "Kimi reports no usable provider configuration; run `kimi login`."
    count = _provider_count(run.stdout)
    if count is None:
        # Parsed nothing: report indeterminate rather than claiming either way.
        return None, None
    if count == 0:
        return False, "Kimi has no configured provider; run `kimi login`."
    return True, f"Kimi reports {count} configured provider(s)."


def _provider_count(stdout: str) -> int | None:
    """Count configured providers from `kimi provider list --json`, tolerantly.

    Never returns any provider detail — base URLs and API keys live in that payload.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("providers", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, dict):
                return len(value)
    return None


def _auth_error() -> ErrorInfo:
    return make_error("kimi_auth_required", "kimi is not authenticated.")


def _rate_limit_error(retry_after_ms: int) -> ErrorInfo:
    return make_error(
        "kimi_rate_limited", "kimi hit a usage/rate limit.", retry_after_ms=retry_after_ms
    )


def contract_changed_error() -> ErrorInfo:
    """Shared cli_contract_changed error, reused across every failure path so a drift is
    reported identically wherever `kimi` surfaces it."""
    return make_error(
        "cli_contract_changed",
        "kimi rejected a flag or value this plugin sent — its CLI "
        "contract likely changed for your installed version.",
    )


def _invalid_model_error() -> ErrorInfo:
    """Error for a model alias absent from the user's config.toml.

    Value-free by policy: the rejected alias is caller input, which the caller already holds.
    """
    return make_error(
        "invalid_model",
        "Kimi does not have that model alias configured. `-m` takes an alias defined in "
        "config.toml, not a raw provider model id.",
        details=ErrorDetail(field="model"),
    )


def _extra_args_rejected_error(matched: list[str]) -> ErrorInfo:
    named = ", ".join(matched) if matched else config.EXTRA_ARGS_ENV
    return make_error(
        "extra_args_rejected",
        f"kimi rejected an argument from {config.EXTRA_ARGS_ENV} ({named}).",
        repair_alternative=(
            f"Fix or remove the offending entry ({named}) in {config.EXTRA_ARGS_ENV}; "
            "this is operator config, NOT a plugin contract drift. Verify the option "
            "against `kimi --help` for your installed version."
        ),
    )


def _unresolved_default_model_error() -> ErrorInfo:
    """Error for an unresolvable `default_model` in the user's config.toml.

    Shares the invalid_model code with _invalid_model_error (no new code, so the discovered
    error-code set is unchanged), but deliberately carries NO `field="model"`: this failure
    reaches the classifier even when the caller's `model` argument was valid, so blaming
    that argument would send the caller round a repair loop it cannot exit.
    """
    return make_error(
        "invalid_model",
        "Kimi could not resolve the `default_model` in its config.toml — the alias names "
        'no `[models."..."]` section. This is the CONFIGURED DEFAULT, not the `model` you '
        "passed, so overriding or omitting `model` will not help; the fix is to correct "
        '`default_model` in config.toml (or add the matching `[models."..."]` section).',
    )


def classify_failure(
    run: CommandRun,
    *,
    last_message: str | None = None,
    events: str | None = None,
    extra_args: config.ExtraArgs | None = None,
    reasoning_effort: str | None = None,
    sanitize: Callable[[str], str] | None = None,
) -> ErrorInfo:
    """Classify a non-success `kimi -p` run into a recoverable ErrorInfo.

    The precedence order is pontonier's shared one, which its classifier documents as the
    part every bridge must agree on: drift -> auth -> rate limit -> invalid model. Drift
    first, so a genuine contract change is never masked as a retryable transient or as a
    caller mistake. An earlier version tested invalid_model first, citing an unknown `-m`
    alias whose message "also trips the drift patterns"; it does not — neither captured
    invalid-model message matches any drift pattern (checked against the 0.35.0 and 0.39.1
    captures), so the shared order costs nothing on real kimi output (#11).
    `tests/test_backend.py` holds this function to parity with `pontonier.backend.classify`
    on stderr-only input.

    This function searches wider than the shared classifier — `stdout`, `last_message`
    and the stream-json error event as well as `stderr` — which is a separate, unresolved
    divergence (#11); the order is the part reconciled here.

    There is deliberately NO backend reasoning-effort branch here, unlike the Codex
    adapter. Verified on kimi-code 0.35.0: kimi silently IGNORES an unrecognized
    KIMI_MODEL_THINKING_EFFORT and exits 0, so a rejection never reaches this function.
    A branch for it would be dead code advertising a check that never runs; the effort is
    validated locally before spend instead (server._reasoning_effort_unsupported_error).
    `reasoning_effort` is still accepted so call sites keep one shape, and is used only to
    attribute an operator passthrough below.
    """
    if run.binary_missing:
        return make_error("kimi_not_found", "The `kimi` CLI was not found on PATH.")
    if run.timed_out:
        return make_error("timeout", "kimi exceeded the timeout.")
    # Accepted for call-site uniformity; see the docstring on why kimi has no backend
    # effort rejection to classify.
    _ = reasoning_effort
    event_error = normalize.extract_error_message(events) if events else None
    if cli_contract.is_contract_drift(run.stderr, run.stdout, event_error):
        matched = _extra_args_drift_match(extra_args, run.stderr, run.stdout, event_error)
        if matched is not None:
            return _extra_args_rejected_error(matched)
        return contract_changed_error()
    if cli_contract.is_auth_failure(run.stderr, run.stdout, last_message, event_error):
        return _auth_error()
    if cli_contract.is_rate_limited(run.stderr, run.stdout, last_message, event_error):
        retry_after = cli_contract.parse_retry_after_ms(
            run.stderr, run.stdout, last_message, event_error
        )
        # Explicit None check: a parsed "retry after 0" is a valid delay (retry now) and
        # must survive rather than being coalesced to the default by a falsey test.
        if retry_after is None:
            retry_after = cli_contract.RATE_LIMIT_DEFAULT_BACKOFF_MS
        return _rate_limit_error(retry_after)
    if cli_contract.is_invalid_model(run.stderr, run.stdout, last_message, event_error):
        if cli_contract.is_unresolved_default_model(
            run.stderr, run.stdout, last_message, event_error
        ):
            return _unresolved_default_model_error()
        return _invalid_model_error()
    # Sanitize (or redact) the full text BEFORE truncating: a secret or worktree path
    # straddling the cut would otherwise lose the tail the patterns need, leaking a prefix.
    raw = (event_error or run.stderr or run.stdout).strip()
    detail = (sanitize(raw) if sanitize is not None else (redaction.redact_text(raw) or ""))[:300]
    return make_error("nonzero_exit", f"kimi exited {run.exit_code}: {detail}")


def _descriptor_in_blob(descriptor: str, blob: str) -> bool:
    """Whether `descriptor` appears in `blob` at flag/token boundaries.

    A bare substring test is too loose: a short descriptor would match inside an unrelated
    word, misattributing a genuine plugin-flag drift to the operator's passthrough.
    """
    pattern = rf"(?<![\w-]){re.escape(descriptor)}(?![\w-])"
    return re.search(pattern, blob, re.IGNORECASE) is not None


def _extra_args_drift_match(extra: config.ExtraArgs | None, *texts: str | None) -> list[str] | None:
    """Descriptors of `extra` that kimi named in a rejection blob, or None.

    Returns None when no extra args are configured/valid, so a genuine plugin-flag drift
    stays cli_contract_changed and the fail-loud guarantee holds.
    """
    ea = config.extra_args() if extra is None else extra
    if not ea.configured or not ea.valid or not ea.descriptors:
        return None
    blob = "\n".join(t for t in texts if t)
    matched = [d for d in ea.descriptors if _descriptor_in_blob(d, blob)]
    return matched or None
