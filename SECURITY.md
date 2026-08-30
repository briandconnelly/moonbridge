# Security

## Reporting a vulnerability

Please report security issues privately via GitHub's "Report a vulnerability" (Security advisories)
on this repository rather than opening a public issue.

## Security model

Everything below was verified by running `kimi-code 0.35.0`, and re-verified against `0.39.1`
(every property below still holds). Where a property does not hold, this document says so rather
than describing an intention.

### What actually constrains a run

**The `kimi` CLI has no sandbox and no approval prompts.** Prompt mode forces autonomous mode; it
refuses to be combined with `--yolo`, `--auto`, or `--plan` because it already implies them. There
is no `--sandbox read-only` to hand it. Two controls stand in:

- **The read-only agent profile (the enforcing control).** `kimi_consult` and
  `kimi_review_changes` run under a generated `--agent-file` whose `tools:` list contains only
  `Read`, `Glob`, and `Grep`. Verified: an agent so declared reports exactly those three tools, and
  a shell write attempt produces no file. The tools are absent from the model's schema, so this is
  enforcement rather than instruction.
- **A throwaway git worktree (defense in depth only).** Every tier runs in one, seeded from your
  tracked state. It is **not a boundary**: asked to write to an absolute path outside its working
  directory, Kimi does so. Verified directly. Treat the worktree as keeping honest runs away from
  your tree, not as containment.

**`kimi_delegate` never modifies your working tree.** It runs in the worktree and returns a diff
for you to review and apply yourself.

### Limits you must not assume away

- **Read-only prevents modification, not disclosure.** The allowlist keeps `Read`, and kimi's Read
  tool accepts absolute paths — a read-only run asked for a file in an unrelated directory returned
  its contents. A prompt-injected repository can therefore make a consult read host files and send
  them to your configured provider.
- **Delegate is not network-isolated.** There is no sandbox, so a delegated task can push, fetch,
  install dependencies, or call out, with your own user's privileges. The returned diff reflects
  worktree changes only — not other side effects.
- **Background processes can outlive a run.** Kimi's shell tool spawns each command in its own
  process group, so a plain process-group kill does not reclaim them. This server sweeps for
  survivors by the run's unique worktree path after every run, but a process that rewrites its
  own command line or daemonizes can evade an argv-based sweep.

### Handling of your inputs

- **Prompts do not appear in process listings.** kimi ignores stdin and takes its prompt from
  argv, so this server writes the prompt — including any gathered diff — to a private file
  (mode 0600) in a server-owned temporary directory, and passes only a short pointer on the command
  line. That directory is outside the repository, so a repository cannot redirect those writes with
  a tracked symlink; the files are created `O_EXCL|O_NOFOLLOW` and removed after the run.
- **The read-only agent profile is read back before launch**, so a run never proceeds under a
  profile whose bytes were not confirmed.

## Secret redaction is best-effort

The server redacts secret-looking files and inline values from diffs it gathers
(`_core/redaction.py`). This is **defense in depth, not a guarantee**:

- It covers only the diff text the server gathers, and Kimi's returned output. It does **not**
  cover what you type, or files Kimi reads for itself.
- During any active call, Kimi auto-loads the workspace's `AGENTS.md` and discovers skills from its
  own user/project directories and from the `extra_skill_dirs` entries in its `config.toml` — which
  may point anywhere on disk, outside any workspace. Its built-in skills always load. Anything
  private in those locations is eligible for egress on any active call, whatever workspace you
  target.
- `isolation=ignore-skills` replaces the discovered user/project skill directories with an empty
  one. It does **not** suppress built-in skills or `AGENTS.md`. It reduces exposure; it does not
  eliminate it.
- `kimi provider list --json`, which this server reads to discover model aliases, contains your
  provider `apiKey` in plaintext. The parser reads only the `models` map, and a test asserts the
  key never reaches a result envelope.

## Untrusted content

The question, task, diff, and any context sent to Kimi are framed as untrusted data, with explicit
instructions not to follow embedded directives or exfiltrate secrets. This mitigates but does not
eliminate prompt-injection risk — and given the read and network limits above, a hostile repository
is the threat this design most depends on you thinking about. Treat Kimi's output as claims to
verify.
