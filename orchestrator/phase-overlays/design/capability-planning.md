# Design Capability Planning Overlay

Allowed planning: decompose the lane into small tasks that produce contracts, interfaces, validation criteria, and approved design artifacts.

This prompt is for planning only. Do not describe implementation, file edits, or validation execution as current allowed work.

Prefer minimal task graphs and tight ownership. Default to one producing task for the lane. If you need more than one task, each task must emit a required lane output or required outbound handoff output.

Do not split reconciliation or synthesis from later package/materialization tasks when the producing design task can emit the same artifacts directly.

Use only real shell validation commands. Do not invent placeholder validators such as `check-design-package`.

Forbidden planning: broad implementation work, shell commands, and unrelated historical digging beyond injected inputs.
