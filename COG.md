---
type: cog [0.1]
name: cog-turn-harness
description: Runs one JSON interaction against an independently bound model.
version: "0.1.0"
license: Apache-2.0
publisher: OpenTeams
manifest: cog.yaml
manifest_schema: openteams/cog-manifest [0.1]
---

# cog-turn-harness

Runs one JSON interaction against an independently bound model.

Composition: **harness**. Binding candidates require independent host admission.
Turns accept explicit context, object output schema and exact binding references.
No session continuation or external tool grants. Packaged consumer checks run in
the workbench context bridge before and after the turn; this provider does not
advertise contract-checks/packaged itself.

Model identity is requested, not independently attested or weights-pinned.
Subscription harness results are unsuitable as bare-model evaluation evidence.
See README.md for use, limitations and deterministic verification.
