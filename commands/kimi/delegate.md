---
description: (Deprecated: use amicus) Delegate a coding task to Kimi; get back a reviewable diff (not applied)
argument-hint: "<task description>"
---

**Deprecated:** moonbridge is superseded by amicus. If amicus's tools are available,
use `amicus_delegate` with `backend="kimi"` instead, following amicus's
`collaborating-with-amicus` skill, and stop here. Otherwise continue below.

Delegate a coding task to Kimi Code using the `kimi_delegate` MCP tool from the
moonbridge server.

Task: $ARGUMENTS

Pass the absolute repository path as `workspace_root`. Kimi implements the task in
a throwaway git worktree and returns a `diff` — it does NOT touch the working tree.

For a substantial or multi-file task that can exceed the synchronous deadline
(built-in default 300s), use `kimi_delegate_async` instead and poll for the result —
a sync call whose deadline expires loses the paid run.

When the result returns:
1. Show the proposed `diff` and Kimi's `summary`.
2. Review the diff for correctness yourself.
3. Apply it to the working tree (using your own edit tools) only if it is correct —
   and tell the user you are about to, or ask first if it is a significant change.
Do not apply a diff you have not reviewed.
