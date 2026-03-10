# Orchestrator TODO

## Next Runtime / Dashboard Improvements

- TODO: Parallelize `plan-phase` across objectives so later objectives do not remain at `0%` while the first objective is still planning.
- TODO: Stream planner stdout/stderr artifacts to disk while planning processes are still running instead of writing them only at process exit.
- TODO: Shrink capability-manager prompts by passing compact summaries where possible instead of repeating the full goal and outline payloads.
- TODO: Prefix dashboard activity and objective labels with stable unique ids so long names are easier to identify in the TUI.
- TODO: Add a completed activity history section at the bottom of the CLI dashboard.
- TODO: Persist an activity log somewhere inside the app directory for human review outside the live dashboard.
- TODO: Reconcile stale live activities and recover cleanly when a planning or execution connection is interrupted so runs do not remain stuck in phantom `finalizing` states.
- TODO: Add a flag to not require human approval at all for a run. This mode should go through all phases end to end without the need for human interaction.
- TODO: Add monitoring for different statistics around the calls to the llm during a run, these should include things like response time, amount of tokens used, size of the prompt, and any other data that would be relevant for an engineer looking into the health of the system.
