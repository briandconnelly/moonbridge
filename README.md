# Moonbridge

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Moonbridge is a local MCP server that lets compatible MCP clients invoke **Kimi Code** for
independent second opinions, structured code reviews, and delegated coding tasks.

It gives your primary coding agent another model to ask—one that can challenge a plan, inspect a
diff, or try an implementation while you keep control of the result. Moonbridge ships plugin
integrations for Claude Code and Codex; other clients can use its local stdio server.

**Contents:** [Why Moonbridge?](#why-moonbridge) · [Requirements](#requirements) ·
[Quick start](#quick-start) · [Safety model](#safety-model) · [Tools](#tools) ·
[Configuration](#configuration) · [Development](#development) · [Documentation](#documentation)

## Why Moonbridge?

Different models notice different things. Moonbridge lets one coding agent bring Kimi into the
conversation without making you switch tools or manually shuttle context between terminals.

| Workflow | What Kimi does | What you get |
|---|---|---|
| Consult | Reasons under a read-only agent profile | An answer or independent second opinion |
| Review | Inspects your git changes without shell or write tools | Structured findings with coverage and verdict |
| Delegate | Implements a task inside a throwaway worktree | A reviewable diff that is **never applied automatically** |

Every workflow returns a structured result to the calling client. Long-running work can run in the
background and be recovered after a dropped connection.

Before using Moonbridge with sensitive code, read the [safety model](#safety-model). The Kimi CLI
has no sandbox or approval prompts, and a throwaway worktree is not a security boundary.

## Requirements

- `kimi` (Kimi Code) **0.35.x** or **0.39.x** — the supported minors. Probed on `0.35.0`
  and `0.39.1`; other patch releases in those minors are assumed compatible, not verified
- Python ≥ 3.11, `uv`, and `git`
- macOS or Linux
- Claude Code, Codex, or another MCP client that can launch a local stdio server

Make sure Kimi is installed and has at least one configured provider and model alias:

```sh
kimi --version
kimi doctor config
```

See the [Kimi Code documentation](https://moonshotai.github.io/kimi-code/) for CLI installation and
authentication.

## Quick start

No clone is required. The plugin integrations launch the server from the release tag pinned in
[`.mcp.json`](.mcp.json).

### Codex

```sh
codex plugin marketplace add briandconnelly/moonbridge
codex plugin add moonbridge@moonbridge
```

Start a new Codex session so it loads the bundled skill and tools.

### Claude Code

Run these commands inside Claude Code:

```text
/plugin marketplace add briandconnelly/moonbridge
/plugin install moonbridge@moonbridge
```

### Other MCP clients

Moonbridge can work with another client if it supports local stdio servers. Adapt the `moonbridge`
entry in [`.mcp.json`](.mcp.json) to that client's configuration format. Claude Code and Codex are
the integrations currently packaged and documented by this project.

### Try it

Ask your coding agent:

- `Check whether Kimi is ready.` — a free readiness check with no model call
- `Get Kimi's second opinion on this approach.`
- `Have Kimi review my current changes.`
- `Delegate this task to Kimi and show me the proposed diff.`

Claude Code also provides `/kimi:status`, `/kimi:consult`, `/kimi:review`, and `/kimi:delegate`
shortcuts. In Codex, `/plugins` lets you browse, enable, or disable the installed plugin.

## Safety model

Read this before pointing it at anything sensitive. Every statement below was verified by running
`kimi-code 0.35.0` and again on `0.39.1`, not inferred from its documentation.

**The `kimi` CLI has no sandbox and no approval prompts.** Prompt mode (`kimi -p`) forces
autonomous mode and runs shell commands and file writes with your own user's privileges. Unlike
Codex, there is no `--sandbox` flag to hand it. So this server constrains runs itself:

- **consult and review get an agent profile whose `tools:` list omits every shell and write
  tool.** This is the real control, and it works: an agent declaring `Read, Glob, Grep` reports
  exactly those three, and a shell write attempt produces nothing.
- **every run uses a throwaway git worktree.** This is defense in depth, *not* a boundary —
  asked to write outside its working directory, Kimi will do it. Treat the worktree as keeping
  honest runs tidy, not as containment.

Three limits that follow, stated plainly because they are easy to assume away:

1. **Read-only prevents modification, not disclosure.** Kimi's Read tool accepts absolute paths, so
   a prompt-injected repository can make a consult read files elsewhere on your machine and send
   them to your provider. Do not point any workflow at a workspace whose contents you would not hand
   to that provider.
2. **Delegate is not network-isolated.** A delegated task can push, fetch, install dependencies,
   and call out. The returned diff shows what changed *in the worktree* — not everything the run
   did.
3. **Kimi loads context you did not mention.** It auto-loads the workspace's `AGENTS.md` and
   discovers skills from its own user/project directories and from the `extra_skill_dirs` entries
   in its `config.toml`, which may point anywhere on disk. Its built-in skills always load. The
   `isolation` setting reduces this but cannot eliminate it.

Secret redaction covers gathered diffs and Kimi's returned output. It does **not** cover what you
type, or files Kimi reads for itself.

For the complete threat model and disclosure policy, see [SECURITY.md](SECURITY.md) and
[COMPATIBILITY.md](COMPATIBILITY.md).

## Tools

“Paid” means the tool makes a Kimi model call and consumes quota from your configured provider;
Moonbridge itself is not a paid service.

| Tool | Cost | Notes |
|---|---|---|
| `kimi_status` | free | readiness, version, provider configuration, resolved defaults |
| `kimi_capabilities` | free | full inventory, schemas, per-tool error codes |
| `kimi_models` | free | model **aliases** from your `config.toml`, with each alias's declared efforts |
| `kimi_consult` / `_async` | paid | read-only Q&A |
| `kimi_review_changes` / `_async` | paid | structured review of `working_tree`, `branch`, or `commit` |
| `kimi_delegate` / `_async` | paid | returns a reviewable diff, never applied |
| `kimi_dry_run`, `kimi_delegate_dry_run` | free | preview scope, diff size, redactions before spending |
| `kimi_job_{status,result,consume_result,cancel,list}` | free | background job lifecycle |

Two details prevent surprising runs:

- **`model` takes an alias**, not a provider model id — whatever you defined as
  `[models."<alias>"]` in `config.toml`. An unknown alias is rejected as `invalid_model`.
- **`reasoning_effort` is validated locally.** Kimi silently *ignores* an effort it does not
  recognize rather than rejecting it, so this server refuses one the alias does not declare — a run
  that quietly used the default while reporting your requested effort would be worse than an error.

## Configuration

Environment variables, all prefixed `MOONBRIDGE_`:

| Variable | Default | Meaning |
|---|---|---|
| `TIMEOUT_SECONDS` | 300 | per-call wall clock, clamped 10–600 |
| `MODEL` | unset | default model alias |
| `REASONING_EFFORT` | unset | default effort |
| `ISOLATION` | `inherit` | `inherit` or `ignore-skills` |
| `MAX_INPUT_BYTES` | 200000 | bound on gathered context |
| `MAX_DELEGATE_DIFF_BYTES` | 200000 | bound on a returned diff |
| `JOB_TTL` / `JOB_MAX_SECONDS` / `JOB_MAX_COUNT` | 86400 / 1800 / 50 | background job limits |
| `STATE_DIR` | `~/.cache/moonbridge/jobs` | job records |
| `LOG_LEVEL` / `LOG_FILE` | `WARNING` / unset | logging |
| `SUPPORTED_VERSIONS` | `0.35,0.39` | supported `kimi` minors (probed at `0.35.0`, `0.39.1`) |
| `EXTRA_ARGS` | unset | **no safe passthrough exists** — any value is refused, see below |

`MOONBRIDGE_EXTRA_ARGS` accepts nothing today, deliberately. Kimi exposes no config-override,
profile, or feature flags, and reuses two short flags for other purposes: `-p` is **prompt** and
`-c` is **continue**. Passing them through would override the run's real instructions or resume an
unrelated session, so the allowlist is empty and a configured value fails loudly rather than being
silently ignored.

## Development

Set up the project and run the focused test suites from the repository root:

```sh
uv sync
uv run pytest
uv run pytest -m integration --no-cov  # optional: calls the real Kimi CLI
```

The authoritative quality gate and contribution workflow live in
[AGENTS.md](AGENTS.md#tooling) and [CONTRIBUTING.md](CONTRIBUTING.md).

To exercise the plugin manifest from a checkout, register that checkout as a marketplace:

```sh
codex plugin marketplace add <path to this checkout>
codex plugin add moonbridge@moonbridge
```

In Claude Code, run `/plugin marketplace add <path to this checkout>`, then install as above. This
tests the plugin files in your checkout, but the MCP server still comes from the **released** tag
pinned in `.mcp.json`; Python edits in the checkout do not affect it.

To run the working tree in Codex, register a separate development server:

```sh
codex mcp add moonbridge-dev -- uv run --directory <path to this checkout> moonbridge-mcp
```

In Claude Code, override the server in the consuming project's own `.mcp.json`:

```json
{
  "mcpServers": {
    "moonbridge": {
      "command": "uv",
      "args": ["run", "--directory", "<path to this checkout>", "moonbridge-mcp"]
    }
  }
}
```

## Documentation

- [Tool reference](docs/REFERENCE.md) — the full caller-facing contract: envelopes, error codes,
  detail levels, idempotency, jobs. Read it when calling the MCP tools directly.
- [Compatibility](COMPATIBILITY.md) — what the `kimi` CLI does and does not guarantee, and every
  deliberate non-guarantee.
- [Security policy](SECURITY.md) — the verified security model, and how to report a vulnerability.
- [Contributing](CONTRIBUTING.md) — set up a checkout and prepare a pull request.
- [Agent conventions](AGENTS.md) — the authoritative working rules for humans and agents.
- [Upgrading kimi](docs/UPGRADING-KIMI.md) — the probes to re-run before supporting a new `kimi`
  version.
- [Releasing](docs/RELEASING.md) — the ordered release runbook.
- [Changelog](CHANGELOG.md) · [Architecture decision records](docs/adr/README.md) — decision
  history, not current policy.
