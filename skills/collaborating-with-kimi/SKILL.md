---
name: collaborating-with-kimi
description: >-
  Deprecated: when amicus's tools are installed, use amicus's collaborating-with-amicus skill
  instead. Otherwise, use whenever the host coding agent should call Kimi: a consult, code review,
  delegated implementation, async run, independent two-model attempt, or declared review–revise
  workflow. Trigger on “ask Kimi,” “get a second opinion,” “have Kimi review this,” “delegate this
  to Kimi,” “have both models attempt this,” or “run review–revise,” and at decision points:
  choosing a hard-to-reverse approach, after two failed fixes, or before declaring risky work
  complete. Route to the matching reference and compose with applicable process skills.
---

# Collaborating with Kimi

## Deprecated: use amicus

moonbridge is superseded by [amicus](https://github.com/briandconnelly/amicus), which calls Kimi
and other backends. 0.4.0 is moonbridge's final release; it gets no fixes after that.

- If amicus's tools (`amicus_*`) are available, stop here: load amicus's
  `collaborating-with-amicus` skill and use amicus for this request. Pass `backend="kimi"` on
  each amicus call that takes a `backend`.
- Each `kimi_*` tool's deprecation marker (in its `_meta` and its `kimi_capabilities` entry)
  names its amicus successor in `replaced_by` and says what to change in `migration`.
- Job ids do not carry over to amicus: finish jobs started here with this server's
  `kimi_job_*` tools.
- Use the rest of this skill only when amicus is not installed.

Use this skill as the router and shared safety contract for every Kimi workflow. Retain
responsibility for the work, and compose this guidance with applicable planning, debugging, review,
and verification skills instead of replacing them.

## Shared workflow

1. Call `kimi_status` before a paid call. Proceed only when both `ready: true` and
   `extra_args_valid: true`. If either is false, stop and surface the corresponding readiness or
   operator-configuration detail.
2. Treat `rate_limit` as **unavailable, and that is not a fault**. kimi exposes no
   quota-read channel and its provider is user-configured, so `kimi_status` reports
   `unavailable` rather than guessing. There is no quota signal to pace spend from — judge
   cost from the size of the task, and surface a provider-side rate-limit error
   (`kimi_rate_limited`) to the user if one comes back.
3. Know what the tiers do and do not guarantee before choosing one. The `kimi` CLI has no
   sandbox and no approval prompts, so:
   - `kimi_consult` / `kimi_review_changes` run under a generated agent profile with **no
     shell and no write tools**. That prevents modification, NOT disclosure: Kimi's Read
     tool accepts absolute paths, so a prompt-injected repository can make it read other
     files on the machine.
   - `kimi_delegate` runs with the full tool set. Its edits land in a throwaway worktree
     and come back as a diff that is never applied — but the worktree is not a boundary:
     the task can also reach the network and write outside it. The diff shows what changed
     in the worktree, not everything the run did.
   - Do not point any tier at a workspace whose contents you would not hand to the
     configured provider.
4. Select one route below and load only its needed reference. Use a free dry-run when one exists.
5. Declare the paid-call cap before the first active call, then stay within it.
6. Branch on `ok`, then on the concrete tool/result type. Verify claims before acting.

## Route the request

| Situation | Tool or workflow | Read |
| --- | --- | --- |
| One answer, design critique, or second opinion | `kimi_consult` | [active workflows](references/active-workflows.md) |
| Stuck mid-debugging or choosing between viable approaches | `kimi_consult` | [active workflows](references/active-workflows.md) |
| Review changes already represented in git | `kimi_review_changes` | [active workflows](references/active-workflows.md) |
| Proposed implementation diff from a throwaway worktree | `kimi_delegate` | [active workflows](references/active-workflows.md) |
| A consult, review, or delegate that can exceed the synchronous deadline — high-reasoning-effort or broad repo-grounded work, a multi-file or whole-branch review, or a substantial implementation task | matching `_async` tool | [background jobs](references/background-jobs.md) |
| The host agent and Kimi attempt independently, then synthesize | independent two-member attempt | [independent attempt](references/independent-attempt.md) |
| The host agent drafts, Kimi critiques, and the host agent revises | declared review–revise | [review–revise](references/review-revise.md) |
| Optional parameters, idempotency, or a tool error | current tool | [options and errors](references/options-and-errors.md) |
| MCP server unavailable | limited read-only CLI fallback | [server-down fallback](references/server-down-fallback.md) |
| None of these, or a Kimi call would not change the decision | no call — proceed without Kimi | — |

When a request matches both a sync row and the async row, prefer the matching `_async` tool: a
sync call whose deadline expires (built-in default 300s) is terminated and its partial paid work is
lost, whereas the async job runs to a separately configured deadline (built-in default 1800s). The
sync tool is for focused work that finishes well inside the deadline.

Use `kimi_dry_run` or `kimi_delegate_dry_run` to preview review or delegate scope. Use
`kimi_capabilities`, `kimi_status`, and `kimi_models` for current schemas, defaults, readiness,
and model discovery. A subset of these tools is also exposed to users as `/kimi:*` slash commands.

Route a one-call critique or “judge my draft” request as ordinary consult or review. A single
consult — or no call — is the default; composition is opt-in and exceptional. Select a composed
workflow only when the user requested it or the task already declares it, and the value/risk gate
clears: the stakes are high (a hard-to-reverse, load-bearing, or security-sensitive decision), a
single opinion is genuinely insufficient, and you can verify and synthesize the outputs. If the
gate fails, make one call or none and move on.

## Reading results

- Branch on `ok` first. On `ok: false`, branch on `error.code` and use the machine-readable
  `error.repair`; do not infer recovery from prose or retry blindly.
- On `ok: true`, branch on the concrete tool or result type before reading fields. Completed
  consult, review, and delegate results share active-result fields; only review has
  `verdict`/`confidence`, and only delegate has `diff`.
- Discovery, dry-run, async-start, and job-lifecycle tools have tool-specific success
  schemas. A result fetched with `kimi_job_result` or `kimi_job_consume_result` matches the
  originating consult, review, or delegate tool.
- Treat live tool schemas and `kimi_capabilities` as authoritative for exact inputs, outputs, error
  codes, and defaults.

## Data exposure

Facts to weigh before any active call:

- Every supplied prompt and context field is sent to your configured Kimi provider raw.
- During every active call — including consult — Kimi may read other files. Its Read tool
  accepts ABSOLUTE paths, so "read-only" does not confine it to the workspace.
- Kimi auto-loads the workspace's `AGENTS.md` and discovers skills from its own user/project
  directories and from the `extra_skill_dirs` entries in its config.toml, which may point
  anywhere on disk — even if the prompt never mentions them. The isolation setting does not
  suppress any of this; kimi's built-in skills always load.
- Redaction is best-effort protection for gathered diffs and returned output only. It never protects
  supplied input, auto-loaded context, or files Kimi reads.

## Binding rules

- **Spend — one call per decision point:** Make one active call per ordinary decision point. Each
  async start counts as an active call, and never start both the sync and async forms for the same
  work.
- **Spend — workflow caps:** An independent-attempt workflow gets one Kimi call. A declared
  review–revise workflow gets one call by default, and at most two only when high risk and the
  two-call cap were declared before call one (see the independent-attempt and review–revise
  references).
- **Workspace:** Pass an absolute `workspace_root` for every repo-grounded call, including consult,
  dry-run, and job-lifecycle calls. Omit it only for a pure question that needs no workspace.
- **Privacy:** Do not make an active call when the supplied prompt, the supplied context, any file
  Kimi may inspect in the resolved workspace, or your user-global skills under
  the `extra_skill_dirs` in kimi's config.toml contain something you cannot disclose (see Data
  exposure). Changing the workspace does not exclude those skills.
- **Verification:** Treat findings, summaries, verdicts, and proposed changes as unverified claims.
  Run the applicable project checks yourself; read-only consult/review is not proof tests ran.
- **Delegation:** Never apply a delegated diff before reviewing it. The plugin does not apply it to
  the live tree. **Delegate runs are NOT network-isolated** — kimi has no sandbox, so the task
  can push, fetch, install, and call out with the user's own privileges. Scope tasks
  accordingly, and remember the returned diff shows only what changed in the worktree.
- **Retry:** Never loop paid retries. After an ambiguous transport failure, retry the same concrete
  tool with the same arguments and `idempotency_key`; never switch between sync and async expecting
  that key to replay the run.
- **Polling — pacing:** Wait at least the current `poll_after_ms` between job-status calls; never
  busy-poll.
- **Polling — workspace:** Pass the same absolute workspace to every lifecycle call for a job.
- **Polling — fetch:** Fetch a job's result only after `result_available` is true.
- **Independence — ordering:** Finalize the host agent's attempt before Kimi's answer enters context:
  start the `_async` call and draft before fetching, or draft before a sync call.
- **Independence — draft placement:** Keep the host agent's draft outside every workspace and baseline
  Kimi can inspect.
- **Independence — reclassification:** If Kimi can see the draft, or Kimi's answer arrived before
  the host agent's attempt was finalized, classify the operation as critique and do not claim
  independence.
- **Git state:** Never stash, commit, switch branches, or create a clean worktree solely to
  manufacture independence unless the user explicitly authorizes it and preservation checks show
  their state will remain safe.
- **Synthesis:** Verify load-bearing disagreements against evidence or project checks. Treat agreement
  as weak evidence because the models may share framing and blind spots; never tally votes or spend
  another call to manufacture confirmation.
- **Recursion:** Do not ask Kimi to invoke another agent or create agent-to-agent call chains unless
  the user explicitly requests that architecture.
