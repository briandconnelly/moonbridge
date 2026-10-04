---
description: (Deprecated: use amicus) Preview what a Kimi review would send — scope, diff size, redactions (free)
argument-hint: "[working_tree|branch <base>|commit <sha>]"
---

**Deprecated:** moonbridge is superseded by amicus. If amicus's tools are available,
use `amicus_review_changes_dry_run` with `backend="kimi"` instead, following
amicus's `collaborating-with-amicus` skill, and stop here. Otherwise continue below.

Call the `kimi_dry_run` MCP tool from the moonbridge server (free — no model
call) to preview what a `kimi_review_changes` call would send.

Scope request: $ARGUMENTS

Map it to `scope`/`base`/`commit` as for /kimi:review, and pass the absolute repo
path as `workspace_root`. Report the context summary (files/lines changed), the
prompt size, whether the diff would be truncated, and any redacted secret paths.
