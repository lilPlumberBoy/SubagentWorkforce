# Monitor Frontend Design

## Purpose

Build a local browser monitor for orchestrator runs that complements and eventually surpasses the current terminal dashboard.

The document that previously lived here was written when the monitoring surface was smaller. The current system now exposes:

- live run state under `runs/<run-id>/live/`
- run guidance and next-action recommendations
- autonomy controller state and audit history
- recovery-aware activity state
- handoff tracking
- activity history
- richer LLM observability
- prompt and artifact inspection views in the terminal tooling

This rewrite updates the frontend design to match the system as it exists now.

## Current System Reality

Relevant runtime and monitor code:

- live monitor and detail rendering: [company_orchestrator/monitoring.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitoring.py)
- live run readers: [company_orchestrator/live.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/live.py)
- observability rollups: [company_orchestrator/observability.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/observability.py)
- run guidance and next action logic: [company_orchestrator/management.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/management.py)
- CLI surfaces for watch, inspect, debug, retry, reconcile, resume: [company_orchestrator/cli.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/cli.py)

Relevant contracts:

- run state schema: [orchestrator/schemas/run-live-state.v1.json](/Users/mike/projects/personal/SubagentWorkforce/orchestrator/schemas/run-live-state.v1.json)
- activity state schema: [orchestrator/schemas/activity-live-state.v1.json](/Users/mike/projects/personal/SubagentWorkforce/orchestrator/schemas/activity-live-state.v1.json)
- live event schema: [orchestrator/schemas/live-event.v1.json](/Users/mike/projects/personal/SubagentWorkforce/orchestrator/schemas/live-event.v1.json)
- run observability schema: [orchestrator/schemas/run-observability.v1.json](/Users/mike/projects/personal/SubagentWorkforce/orchestrator/schemas/run-observability.v1.json)

Current operator-facing monitor behavior:

- `watch-run` renders run header, autonomy controller, activity counts, observability, objective progress, activity groups, handoffs, warnings, recovery summary, activity history, and next action
- `inspect-activity` renders activity metadata, prompt text, recent events, and artifact paths
- `debug-prompt` renders prompt-focused observability and artifact metadata
- normal commands also emit compact run guidance in their result summaries

See [README.md](/Users/mike/projects/personal/SubagentWorkforce/README.md) and the monitor implementation for the source of truth.

## Constraints

- The browser must not read `runs/*` files directly.
- The Python orchestrator already owns run interpretation; frontend code should not recreate scheduling or recovery logic.
- The Python package currently depends only on `rich`, not Flask or FastAPI: [pyproject.toml](/Users/mike/projects/personal/SubagentWorkforce/pyproject.toml)
- The repo does not currently use Vite, Next, or a frontend workspace toolchain.
- The existing frontend pattern is a repo-local React bundle served by a small Node host: [apps/todo/runtime/src/browser-bundle.js](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/runtime/src/browser-bundle.js), [apps/todo/runtime/src/frontend-server.js](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/runtime/src/frontend-server.js)

These constraints imply:

- the read API should stay in Python
- the browser UI should stay thin
- the frontend should reuse the existing no-build runtime pattern unless there is a strong reason to introduce a new toolchain

## Design Goal

The first browser version should make it possible to answer the same practical questions the terminal dashboard answers:

- What phase is the run in?
- Is the run actively working, recoverable, ready for review, ready to advance, or blocked?
- What is the next operator action?
- What is autonomy doing?
- Which activities are active, queued, blocked, interrupted, or recovered?
- Are handoffs blocking progress?
- Are there parallelism warnings or recovery actions?
- What does observability say about current and recent LLM behavior?

The browser should not start by implementing command execution or operator mutations.

## Recommended Architecture

Use a three-part split:

1. Python monitor API
2. React monitor frontend
3. Small Node frontend host, using the same pattern as the todo runtime

### Why this split

- Python already has the authoritative readers for run state, events, observability, and guidance.
- Node + React is already present in the repo and already has a tested localhost-serving pattern.
- This avoids teaching the browser about filesystem layout, schema normalization, or recovery semantics.

## Recommended Repo Layout

### Python API

- [company_orchestrator/monitor_api.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitor_api.py)

Optional supporting module if response shaping grows:

- [company_orchestrator/monitor_view_model.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitor_view_model.py)

### Frontend app

- [apps/monitor/frontend/src](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor/frontend/src)
- [apps/monitor/runtime/src/browser-bundle.js](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor/runtime/src/browser-bundle.js)
- [apps/monitor/runtime/src/frontend-server.js](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor/runtime/src/frontend-server.js)
- [apps/monitor/runtime/scripts/start.js](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor/runtime/scripts/start.js)
- [apps/monitor/runtime/test](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor/runtime/test)

### Root scripts

- [package.json](/Users/mike/projects/personal/SubagentWorkforce/package.json) for helper scripts only

## Backend Recommendation

Do not introduce a Python web framework for the first version.

Preferred first implementation:

- use the Python standard library `http.server` or a similarly small built-in HTTP surface
- expose read-only JSON endpoints only
- keep all response shaping in Python functions that can be unit-tested without starting the server

Reason:

- current Python dependencies are intentionally minimal
- the API surface is small
- this keeps monitor work aligned with the rest of the local-first repo style

If the API later needs auth, streaming, or more routing complexity, revisit the framework decision then.

## Frontend Recommendation

Do not start by adding Vite or another new frontend stack.

Preferred first implementation:

- mirror the todo runtime structure
- bundle browser modules with a simple local bundler
- serve the monitor page from a small Express host
- keep React in plain JavaScript

Reason:

- this matches the repo’s existing runtime pattern
- it avoids introducing a second frontend workflow
- it keeps test and startup behavior consistent with the todo app

## Product Scope

The prior version of this document described a narrower dashboard than the terminal monitor now provides. The browser monitor should reflect the current monitoring model in phases.

### Phase 1: Dashboard Parity For Core Operator Decisions

The first browser release should be read-only and should include:

- run list
- selected run header
- next action panel
- autonomy panel
- activity counts
- observability panel
- objective progress
- grouped activity tables
- handoff table
- parallelism warnings
- recovery actions
- recent activity history

Grouped activity tables should mirror the terminal monitor:

- active planning activities
- active task activities
- queued tasks
- blocked tasks
- interrupted or recovered activities

### Phase 2: Detail Views

After the main dashboard works, add browser equivalents of the current terminal detail tools:

- activity detail view
- prompt debug view
- event timeline for a selected activity
- artifact path summary

These are already operator workflows in the terminal monitor, so they are not speculative features.

### Explicit Non-Goals For The First Browser Release

- retry, resume, approve, reconcile, or other write actions
- auth
- websockets or streaming
- charts
- role prompt editing
- direct raw file browsing
- deep prompt body rendering on the landing dashboard
- replacing every terminal-only workflow on day one

## Data Sources The API Must Understand

The browser monitor should be derived from these existing run artifacts:

- `runs/<run-id>/live/run-state.json`
- `runs/<run-id>/live/activities/*.json`
- `runs/<run-id>/live/events.jsonl`
- `runs/<run-id>/live/activity-history.jsonl`
- `runs/<run-id>/live/observability.json`
- `runs/<run-id>/live/autonomy-history.jsonl`
- `runs/<run-id>/autonomy.json`
- `runs/<run-id>/manager-runs/phase-<phase>.json`
- phase reports and review docs when guidance references them
- handoff state from existing handoff readers

Do not interpret these independently in the browser. Reuse the current Python readers and helpers:

- [read_run_state](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/live.py:512)
- [list_activities](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/live.py:502)
- [read_events](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/live.py:523)
- [read_run_observability](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/observability.py:168)
- [run_guidance](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/management.py:1675)

## API Shape

The old endpoint list was too thin for the current monitor surface. The updated browser API should distinguish between:

- lightweight run-list endpoints
- a composite dashboard endpoint for one selected run
- detail endpoints for activity and prompt inspection

### Minimal Endpoint Set

- `GET /api/runs`
- `GET /api/runs/:runId/dashboard`
- `GET /api/runs/:runId/events?limit=50`
- `GET /api/runs/:runId/activities/:activityId`
- `GET /api/runs/:runId/activities/:activityId/prompt-debug`

Optional later split if payload sizes become awkward:

- `GET /api/runs/:runId/guidance`
- `GET /api/runs/:runId/autonomy`
- `GET /api/runs/:runId/observability`
- `GET /api/runs/:runId/handoffs`
- `GET /api/runs/:runId/history`

### `GET /api/runs`

Return a compact list suitable for the sidebar.

Each item should include:

- `run_id`
- `current_phase`
- `updated_at`
- `run_status`
- `run_status_reason`
- `controller_status`
- `active_activity_count`
- `queued_activity_count`

The list view must be cheap to poll and must not include raw activity payloads.

### `GET /api/runs/:runId/dashboard`

Return a composite view model for the selected run.

Recommended top-level shape:

```json
{
  "run": {},
  "guidance": {},
  "autonomy": {},
  "counts": {},
  "observability": {},
  "objective_progress": [],
  "activities": {
    "active_planning": [],
    "active_tasks": [],
    "queued_tasks": [],
    "blocked_tasks": [],
    "interrupted_or_recovered": []
  },
  "handoffs": [],
  "warnings": [],
  "recovery": [],
  "history": [],
  "events": []
}
```

This endpoint should be the browser equivalent of the current `watch-run` assembly path.

### Guidance Payload

The browser should treat run guidance as a first-class data block, not a derived string.

Include:

- `run_status`
- `run_status_reason`
- `next_action_command`
- `next_action_reason`
- `review_doc_path`
- `phase_recommendation`

These values already drive terminal summaries and the `Next Action` panel.

### Autonomy Payload

Include the fields currently surfaced in the terminal monitor:

- controller status
- approval scope
- stop-before phases
- stop-on-recovery
- adaptive tuning enabled flag
- sandbox mode
- max concurrency
- timeout
- active phase
- last action
- last action status
- stop reason
- last tuning decision
- autonomy audit log path

### Activity Rows

Activity rows in the main dashboard should remain compact but should reflect current live-state richness.

Each row should include:

- `activity_id`
- `display_name`
- `objective_id`
- `kind`
- `status`
- `attempt`
- `progress_fraction`
- `current_activity`
- `latest_event`
- `warnings`
- compact observability summary
- elapsed and updated timestamps

The detail endpoint can return the full normalized activity payload.

### Handoff Rows

Include:

- `handoff_id`
- `objective_id`
- `status`
- `status_reason`
- `from_task_id`
- `to_task_ids`
- `blocking`

### History Rows

Include only recent terminal activity history for the selected run.

Each entry should include:

- `activity_id`
- `objective_id`
- `status`
- `timestamp`
- `attempt`

### Event Rows

Include recent run-level events for the selected run.

Each entry should include:

- `timestamp`
- `activity_id`
- `event_type`
- `message`

### Detail Endpoints

`GET /api/runs/:runId/activities/:activityId` should return:

- normalized activity payload
- recent events
- artifact paths

`GET /api/runs/:runId/activities/:activityId/prompt-debug` should return:

- prompt-focused observability
- prompt path
- optional prompt body
- artifact paths

The browser does not need prompt bodies on the main dashboard, but it should support them in detail mode because the terminal already does.

## UI Shape

### Main Dashboard Layout

- top bar
  - page title
  - selected run id
  - current phase
  - run status badge

- left sidebar
  - run list
  - compact status and phase per run

- top information row
  - next action card
  - autonomy card
  - observability card

- second row
  - activity counts
  - objective progress

- main body
  - active planning table
  - active tasks table
  - queued tasks table
  - blocked tasks table
  - handoffs table
  - interrupted and recovered table

- lower diagnostics
  - parallelism warnings
  - recovery actions
  - recent activity history
  - recent run events

### Detail Views

Recommended detail affordances:

- click an activity row to open activity detail
- from activity detail, open prompt debug
- keep detail UI on the same page in a drawer or panel before adding routing complexity

## Refresh Model

Do not start with websockets.

Recommended polling:

- run list: every 10 to 15 seconds
- selected dashboard: every 2 to 3 seconds while visible
- detail panels: every 2 to 3 seconds only when open

Design the API so the main dashboard can refresh from one composite call for the selected run. This reduces flicker, mismatch between panels, and client-side merge logic.

## Testing Strategy

The previous design doc did not reflect the repo’s testing style. The implementation should follow existing patterns:

- Python unit tests for API response shaping and missing-file behavior
- Node built-in tests for the frontend runtime host and page boot
- no assumption of Jest, Vite, or browser-only test frameworks

### API tests

Cover:

- completed run
- active run
- interrupted or recovered run
- run with missing optional files
- run with blocked handoffs
- run with approved feedback or approved changes affecting guidance

### Frontend tests

Cover:

- frontend runtime starts locally
- browser shell renders
- run list loads
- selecting a run updates the dashboard
- polling updates the dashboard without a full reload
- detail panel renders activity metadata and prompt debug data

## Development Plan

### Phase 1: Contract And View Model

Task 1. Define the browser monitor around current monitor parity.

- Treat `watch-run` as the reference for the landing dashboard.
- Treat `inspect-activity` and `debug-prompt` as phase-2 detail parity targets.

Task 2. Define the API view models in Python before building UI.

- Create serializer functions for run list items.
- Create one serializer for the selected-run dashboard payload.
- Create detail serializers for activity detail and prompt debug.

Checkpoint:

- every UI panel maps to one explicit field in the API response

### Phase 2: Python API

Task 3. Add [company_orchestrator/monitor_api.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitor_api.py).

- use a minimal stdlib HTTP server
- keep endpoints read-only
- reuse current readers and guidance helpers

Task 4. Implement `GET /api/runs`.

- scan available runs
- return compact list items only

Task 5. Implement `GET /api/runs/:runId/dashboard`.

- build one coherent payload from run state, guidance, autonomy, activities, handoffs, history, and observability

Task 6. Implement detail endpoints.

- activity detail
- prompt debug
- recent events with limit support

Checkpoint:

- API responses match current monitor behavior for at least two real runs

### Phase 3: Frontend Runtime

Task 7. Create the monitor runtime tree under [apps/monitor](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor).

- mirror the todo app structure
- keep React in plain JavaScript
- keep the runtime host simple

Task 8. Add a small API client module.

- centralize fetch logic
- support polling
- keep browser code unaware of filesystem details

Checkpoint:

- frontend shell boots locally and prints live run data from the API

### Phase 4: Main Dashboard

Task 9. Render run list and selection.

Task 10. Render next action, autonomy, observability, and counts.

Task 11. Render objective progress and grouped activity tables.

Task 12. Render handoffs, warnings, recovery actions, history, and recent events.

Checkpoint:

- a user can diagnose most run state from the browser without opening the terminal dashboard

### Phase 5: Detail Panels

Task 13. Add activity detail panel.

Task 14. Add prompt debug panel.

Checkpoint:

- browser detail views expose the same practical inspection surface as `inspect-activity` and `debug-prompt`

## Common Mistakes To Avoid

- Do not build the browser by re-parsing raw run files in JavaScript.
- Do not hardcode a status model that differs from `run_guidance`.
- Do not split the selected-run dashboard across too many independently polled endpoints in the first version.
- Do not add write actions before the read-only monitor is reliable.
- Do not introduce a frontend toolchain just because browser UI exists; the repo already has a workable runtime pattern.
- Do not reduce observability to only totals; the current monitor also cares about active process state, stream volume, latency, retries, and call counts by kind.
- Do not ignore handoffs, recovery, or approved external-input states; they now materially affect what an operator should do next.

## Definition Of Done For Browser Monitor V1

Version 1 is done when:

- the Python monitor API starts locally
- the browser monitor starts locally
- the run list shows real runs
- selecting a run shows current run status and next action
- the browser shows autonomy, observability, grouped activities, handoffs, warnings, recovery, and history
- the selected run refreshes on polling without page reload
- a user can determine whether to wait, review, resume, rerun, or advance by using the browser monitor alone

## Future Work After V1

After the read-only browser monitor is stable, consider:

- activity detail drawers with prompt body and artifact links
- operator action endpoints for retry, resume, approve, and reconcile
- event filtering and search
- comparison across runs
- stream transport if polling becomes too coarse
- richer visual summaries once parity and correctness are proven
