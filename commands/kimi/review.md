---
description: "(Deprecated: use amicus) Have Kimi review your git changes and return structured findings"
argument-hint: "[working_tree|branch <base>|commit <sha>]"
---

**Deprecated:** moonbridge is superseded by amicus. If amicus's tools are available,
use `amicus_review_changes` with `backend="kimi"` instead, following amicus's
`collaborating-with-amicus` skill, and stop here. Otherwise continue below.

Use the `kimi_review_changes` MCP tool from the moonbridge server to get an
independent code review from Kimi.

Scope request: $ARGUMENTS

Map the request to the tool's parameters: default `scope=working_tree`; for a branch
review pass `scope=branch` and `base=<branch>`; for a single commit pass
`scope=commit` and `commit=<sha>`. Pass the absolute repo path as `workspace_root`.
Optionally call `kimi_dry_run` first (free) to preview the scope and diff size.

For a multi-file or whole-branch review that can exceed the synchronous deadline
(built-in default 300s), use `kimi_review_changes_async` instead and poll for the
result — a sync call whose deadline expires loses the paid run.

When findings come back, verify each one against the actual code before presenting
it — note which you confirm and which you think are false positives.
