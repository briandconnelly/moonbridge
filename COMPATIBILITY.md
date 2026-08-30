# Compatibility with the `kimi` CLI

This server shells out to the `kimi` (Kimi Code) CLI. Every assumption it makes lives in
`src/moonbridge/cli_contract.py`, so an upstream change is centralized and greppable —
though incorporating one takes the lockstep procedure in
[`docs/UPGRADING-KIMI.md`](docs/UPGRADING-KIMI.md), not a single edit.

Design goal: **fail loudly and safely, never silently weaken a guarantee.**

Verified against `kimi-code 0.35.0` on 2026-08-12 and re-verified against `0.39.1` on 2026-08-30,
both by running the binary. The captures are in [`docs/kimi-help/`](docs/kimi-help/) under each
version, including `M0-FINDINGS.md`, which records the probes and their results. The 0.39.1 pass
moved no guarantee.

## Platform support

**macOS or Linux (POSIX) only.** The async-job safety layer — `fcntl` advisory locks (pid-reuse /
zombie-worker guards), process-group teardown (`os.killpg` / `start_new_session`), and
`SIGTERM`-driven graceful cancellation — is POSIX-only. On a non-POSIX platform these degrade to
owned-children-only locking and direct-PID kills that orphan kimi's child processes, so the server
refuses to start rather than ship a half-safe process model.

`MOONBRIDGE_ALLOW_UNSUPPORTED_PLATFORM=1` downgrades that refusal to a warning.

## What we invoke

- `kimi --prompt <pointer> --output-format stream-json [--agent-file <file>] [--model <alias>]
  [--skills-dir <dir>]` — the whole model-bearing surface.
- `kimi --version`, `kimi --help`, `kimi provider list --json`, `kimi doctor config` — free local
  probes, no model call.

`KIMI_MODEL_THINKING_EFFORT` and `KIMI_MODEL_OUTPUT_FORMAT` are set in the child environment.

## Assumption → upstream source

| Assumption | Why it matters | If it changes |
|---|---|---|
| `--prompt` runs one prompt non-interactively | the entire headless path | server cannot function; fails as `cli_contract_changed` |
| `--output-format stream-json` emits JSONL on **stdout** | the only event surface, and the only answer channel for read-only tiers | `cli_contract_changed` |
| `--agent-file`'s `tools:` list removes tools from the model's schema | **the read-only guarantee** | consult/review silently regain shell and write access — the most severe possible drift |
| `--model` takes a config.toml **alias** | model selection | unknown alias already surfaces as `invalid_model` |
| an unrecognized `KIMI_MODEL_THINKING_EFFORT` is **ignored, not rejected** | effort must be validated locally, pre-spend | if kimi starts rejecting, the local guard becomes redundant but stays correct |
| stdin is ignored; the prompt is an argv value | why the prompt travels via a file | if stdin is accepted, the handshake could be simplified |
| a `{"role":"assistant","content":…}` line carries the answer | read-only tiers have no other channel | `empty_response`; pin the new shape in `cli_contract` |

## Deliberate non-guarantees

These are places where the Codex original made a promise that **cannot** be made here. They are
listed so nobody re-adds the reassuring wording.

- **No sandbox.** `kimi -p` forces autonomous mode and refuses `--yolo`/`--auto`/`--plan` because
  it already implies them. Nothing gates shell commands or writes.
- **The worktree is not a boundary.** Verified: asked to write to an absolute path outside its
  working directory, kimi complies. It is defense in depth only.
- **Read-only is not confidentiality.** kimi's Read tool accepts absolute paths, so a read-only run
  can read files anywhere on the machine.
- **No network isolation.** A delegated task can push, fetch, install, and call out.
- **No quota channel.** kimi exposes nothing equivalent to a rate-limit read, and its provider is
  user-configured, so `kimi_status` reports `rate_limit: unavailable` rather than guessing.
- **No session transfer.** kimi has no app-server import equivalent. (`kimi acp` is the plausible
  future route; it is not used.)
- **No operator passthrough.** kimi exposes no config-override, profile, or feature flags, and
  reuses `-p` for **prompt** and `-c` for **continue** — so `MOONBRIDGE_EXTRA_ARGS` refuses
  everything rather than risk overriding a run's real instructions.

## Implicit context (disclosure)

kimi auto-loads the resolved workspace's `AGENTS.md` and discovers skills from its own
**user/project** directories and from the `extra_skill_dirs` entries in its `config.toml`, which
may point anywhere on disk — outside any workspace. Skill names and descriptions reach the model up
front, so a selected skill's body can be sent even if your prompt never mentions it.

`isolation=ignore-skills` points `--skills-dir` at an empty directory, replacing the discovered
user/project directories. Verified: it does **not** suppress kimi's built-in skills, and it does not
affect `AGENTS.md`. It reduces exposure; it does not eliminate it.

This disclosure is mirrored in `README.md`, `SECURITY.md`, and the `collaborating-with-kimi` skill;
`tests/test_docs_disclosure.py` enforces that every site keeps it.

## Known upstream rough edges

- **argv ceiling.** Past roughly 950,000 characters, kimi dies with a Node
  `RangeError: Maximum call stack size exceeded` (exit 7) rather than a clean error. The server
  keeps argv tiny by writing the real prompt to a file.
- **Orphaned shell children.** kimi's shell tool spawns each command in its own process group,
  reparented to init, so a process-group kill on kimi does not reclaim them. The server sweeps by
  the run's unique worktree path after every run.
- **Non-unique `tool_call_id`.** The same id (`Read:0`) appeared twice in one session, so nothing
  may use it as a correlation key.
- **`goal.summary` lines carry no `role` field**, unlike every other stream line.
