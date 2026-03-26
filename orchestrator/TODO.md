# Orchestrator TODO

## Next Runtime / Dashboard Improvements

- TODO: Parallelize `plan-phase` across objectives so later objectives do not remain at `0%` while the first objective is still planning.
- TODO: Stream planner stdout/stderr artifacts to disk while planning processes are still running instead of writing them only at process exit.
- TODO: Shrink capability-manager prompts by passing compact summaries where possible instead of repeating the full goal and outline payloads.
- TODO: Reconcile stale live activities and recover cleanly when a planning or execution connection is interrupted so runs do not remain stuck in phantom `finalizing` states.
- TODO: Add a flag to not require human approval at all for a run. This mode should go through all phases end to end without the need for human interaction.
- TODO: Make autonomous runs use the same dashboard progress model and lifecycle visibility as manual runs. The only behavioral difference should be whether the controller stops for human approval; planning, execution, recovery, blocked/queued/running states, and progress reporting should otherwise look the same.
- TODO: Add monitoring for different statistics around the calls to the llm during a run, these should include things like response time, amount of tokens used, size of the prompt, and any other data that would be relevant for an engineer looking into the health of the system.
- TODO: Add a dedicated debug-prompt view for live processes that shows each prompt exactly as submitted, plus prompt metadata like submit time, queue wait, processing duration, token usage, and the currently running attempt/workspace.
- TODO: Recovery should ignore blocked, rejected, and otherwise stale bundles when checking for missing landing results, and bundle repair/replan should archive obsolete bundle incidents so `resume-phase` only reasons about active accepted landing work.
