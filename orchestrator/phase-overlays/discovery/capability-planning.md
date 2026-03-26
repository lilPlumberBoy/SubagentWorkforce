# Discovery Capability Planning Overlay

Allowed planning: decompose the lane into the smallest worker tasks needed to capture boundaries, unknowns, dependencies, and candidate handoffs for discovery.

This prompt is for planning only. Do not describe implementation, file edits, or validation execution as current allowed work.

Prefer one to three small tasks. Default to one producing task for the lane. If you need more than one task, each task must emit a required lane output or required outbound handoff output.

Do not split scope synthesis from brief/handoff writing, and do not create packaging/materialization-only follow-up tasks when the producing task can emit the same artifacts directly.

Use only real shell validation commands. Do not invent placeholder validators such as `check-discovery-bundle`.

Forbidden planning: detailed design, implementation, optimization, shell commands, and broad repo ownership.
