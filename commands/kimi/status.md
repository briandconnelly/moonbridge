---
description: (Deprecated: use amicus) Check that the Kimi CLI is installed, authenticated, and ready
---

**Deprecated:** moonbridge is superseded by amicus. If amicus's tools are available,
use `amicus_backends` with `detail="full"` instead, following amicus's
`collaborating-with-amicus` skill, and stop here. Otherwise continue below.

Call the `kimi_status` MCP tool from the moonbridge server (it is free — no
model call). Report whether Kimi is found, authenticated, and a supported version.
If it is not ready, show the `readiness_detail` and the repair guidance, and do not
call any paid Kimi tools until it is resolved.
