---
description: (Deprecated: use amicus) Ask Kimi (a different model) for a read-only second opinion
argument-hint: "<question>"
---

**Deprecated:** moonbridge is superseded by amicus. If amicus's tools are available,
use `amicus_consult` with `backend="kimi"` instead, following amicus's
`collaborating-with-amicus` skill, and stop here. Otherwise continue below.

Ask Kimi Code for an independent second opinion using the `kimi_consult` MCP
tool from the moonbridge server.

Question: $ARGUMENTS

Pass the absolute repository path as `workspace_root` so Kimi reasons about the
right project, and include any specific files or context the question needs as
`extra_context`. When the result comes back, treat Kimi's findings as claims to
verify against the actual code — summarize what is worth acting on and flag
anything you disagree with.

For a high-reasoning-effort or broad repo-grounded consult that can exceed the synchronous
deadline (built-in default 300s), use `kimi_consult_async` instead and poll for the
result — a sync call whose deadline expires loses the paid run.
