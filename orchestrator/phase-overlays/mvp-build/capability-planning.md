# MVP Build Capability Planning Overlay

Allowed planning: decompose the lane into very small worker tasks that implement approved design packages, integrate them into the MVP, and define only the minimum validation or handoff work needed to close the lane.

This prompt is for planning only. Do not describe implementation, file edits, shell commands, or runtime validation as current allowed work.

Implementation should be planned first. Keep review, evidence, and handoff artifacts attached to producing implementation tasks or their immediate validation step whenever possible.

If an upstream contract or handoff is contradictory, create one producing-lane reconciliation plan before any downstream implementation depends on the conflicting behavior.

For middleware/integration lanes in `mvp-build`, plan only connection proof, integration-owned artifacts, and cross-boundary validation over the approved frontend and backend outputs. Do not plan a replacement app runtime tree, duplicate frontend bundle, duplicate backend service, or standalone persistence implementation under a new root such as `apps/<app>/runtime`.

Forbidden planning: unapproved scope expansion, architecture changes, standalone reporting tasks, and execution instructions.
