# Monitor Frontend Design

## Purpose

Build a basic local React application that replaces the current terminal-only dashboard for monitoring orchestrator runs.

The goal of the first version is not feature completeness. The goal is to prove the full data path works:

1. Run data exists on disk.
2. A local API can read and normalize that data.
3. A React app can request the data.
4. The UI can render it on localhost.
5. The UI can refresh and show live changes over time.

This document only covers the minimal first version needed to prove that flow.

## First-Version Scope

The first version should be read-only and intentionally small.

It should show:

- run list
- selected run summary
- live run state
- activities
- recent events
- observability summary

It should not include:

- command buttons like retry, resume, approve, or reconcile
- auth
- charts
- prompt inspection
- multi-page navigation beyond a single dashboard page
- websockets
- advanced styling or design-system work

## Existing Repo Reality

Current relevant structure:

- root Node package: [package.json](/Users/mike/projects/personal/SubagentWorkforce/package.json)
- orchestrator runtime logic: [company_orchestrator](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator)
- current terminal dashboard logic: [monitoring.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitoring.py)
- live run readers: [live.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/live.py)
- observability data logic: [observability.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/observability.py)
- existing app folder pattern: [apps](/Users/mike/projects/personal/SubagentWorkforce/apps)

Important repo constraints:

- There is no existing frontend workspace tooling at the repo level beyond the root package.
- The browser should not read `runs/*` files directly.
- The Python orchestrator already knows how to read and interpret run state, so the local API should stay in Python.

## Recommended Repo Layout

Add a new React app here:

- [apps/monitor](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor)

Add a minimal local monitor API here:

- [company_orchestrator/monitor_api.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitor_api.py)

Use the root package only for helper scripts:

- [package.json](/Users/mike/projects/personal/SubagentWorkforce/package.json)

## Architecture

The first version should use a very simple split:

- React app renders the UI
- Python API reads run data from disk and returns normalized JSON

Recommended localhost setup:

- monitor API: `http://127.0.0.1:8765`
- React app: `http://127.0.0.1:5173`

Recommended first-version update model:

- polling every 2 to 5 seconds
- no streaming yet

Reason:

- polling is easier to build and debug
- the data is already file-based
- this is enough to prove live updates work

## Minimal API Surface

Define these endpoints first and keep them small:

- `GET /api/runs`
- `GET /api/runs/:runId/summary`
- `GET /api/runs/:runId/state`
- `GET /api/runs/:runId/activities`
- `GET /api/runs/:runId/events?limit=50`
- `GET /api/runs/:runId/observability`

First-version response guidance:

- `runs`
  - `run_id`
  - `current_phase`
  - `updated_at`
  - `status`

- `summary`
  - phase
  - controller status
  - run status
  - key counts

- `state`
  - current phase
  - active activity ids
  - queued activity ids
  - counts by status
  - counts by kind

- `activities`
  - activity id
  - display name
  - kind
  - status
  - current activity
  - updated at

- `events`
  - timestamp
  - activity id
  - event type
  - message

- `observability`
  - total calls
  - completed calls
  - failed calls
  - timed out calls
  - token totals
  - latency totals
  - active processes

Do not return giant raw file payloads in the first version.

## Minimal UI Shape

The first version should be a single page.

Recommended layout:

- top bar
  - title
  - selected run id

- left sidebar
  - run list

- main top row
  - summary panel
  - state panel
  - observability panel

- main middle
  - activities table

- main bottom
  - recent events log

UI should stay plain and utilitarian.

The first success criterion is functional visibility, not polish.

## Ordered Development Plan

### Phase 1: Define the Contract

Task 1. Write down the exact first-version scope.

- Confirm the UI is read-only.
- Confirm the only required views are run list, summary, state, activities, events, and observability.
- Confirm that advanced features are intentionally deferred.

Test checkpoint:

- You can describe the first version in one paragraph without adding any extra features.

Task 2. Define the API contract before building anything.

- Decide the exact endpoint list.
- Decide the exact top-level JSON shape for each endpoint.
- Keep the responses compact and stable.

Test checkpoint:

- You can explain what every endpoint returns without opening the code.

### Phase 2: Build the Local API First

Task 3. Create the API entry module.

- Add a new API module in [company_orchestrator/monitor_api.py](/Users/mike/projects/personal/SubagentWorkforce/company_orchestrator/monitor_api.py).
- Keep it read-only.
- Reuse existing run readers from the orchestrator instead of re-parsing files from scratch.

Test checkpoint:

- The API module can start locally and return one hardcoded JSON response.

Task 4. Implement `GET /api/runs`.

- Read the available runs from the repo.
- Return a compact run list.
- Do not include deep details yet.

Test checkpoint:

- Opening the endpoint in a browser or with `curl` shows valid JSON.

Task 5. Implement `GET /api/runs/:runId/summary`.

- Return basic summary data for one run.
- Use the orchestrator’s current run-reading logic rather than inventing new interpretation rules.

Test checkpoint:

- A known run id returns a compact summary object.
- A missing run id returns a clear error response.

Task 6. Implement `GET /api/runs/:runId/state`.

- Return current phase, activity ids, and count summaries.

Test checkpoint:

- You can compare the API response to the existing terminal dashboard output and they roughly match.

Task 7. Implement `GET /api/runs/:runId/activities`.

- Return activities as a flat list of displayable objects.

Test checkpoint:

- A run with interrupted or recovered activities returns readable activity rows.

Task 8. Implement `GET /api/runs/:runId/events`.

- Return only the most recent events.
- Add a limit parameter.

Test checkpoint:

- The endpoint does not dump huge output by default.

Task 9. Implement `GET /api/runs/:runId/observability`.

- Return totals only.
- Keep it compact.

Test checkpoint:

- The response is readable without scrolling through raw logs.

### Phase 3: Verify the API Against Real Run Data

Task 10. Test the API against at least two real runs.

- Use one mostly completed run.
- Use one interrupted or partially recovered run.

Test checkpoint:

- Both run types load successfully.
- Missing optional files do not crash the API.

Task 11. Confirm the API is the only thing touching raw run files.

- The future React app should depend only on HTTP responses.

Test checkpoint:

- No frontend planning depends on direct filesystem reads.

### Phase 4: Bootstrap the React App

Task 12. Create the React app in [apps/monitor](/Users/mike/projects/personal/SubagentWorkforce/apps/monitor).

- Keep the app simple.
- Use plain React.
- Prefer plain JavaScript over TypeScript for the first version if simplicity is the priority.

Test checkpoint:

- `localhost:5173` shows the empty app shell.

Task 13. Add a small API client module in the React app.

- Centralize all fetch calls in one place.
- Do not spread raw fetch logic across many components.

Test checkpoint:

- The app can request `/api/runs` and print the result to the page.

### Phase 5: Build the UI in the Easiest Order

Task 14. Render the run list only.

- Show run id and current phase.
- Add click selection.

Test checkpoint:

- Clicking a run updates selected state in the UI.

Task 15. Render the selected run summary.

- Add the summary panel first.
- Keep it text-only and compact.

Test checkpoint:

- Changing selected run updates the summary panel.

Task 16. Render the state panel.

- Show phase, active activities, queued activities, and status counts.

Test checkpoint:

- The state panel matches the API response cleanly.

Task 17. Render the observability panel.

- Show call totals, tokens, latency, and active processes.

Test checkpoint:

- Observability values load without breaking layout.

Task 18. Render the activities table.

- Show display name, kind, status, current activity, and updated time.

Test checkpoint:

- Long activity text wraps and remains readable.

Task 19. Render the recent events panel.

- Show recent events in a scrollable area.
- Keep it plain text.

Test checkpoint:

- Events can be read as a timeline.

### Phase 6: Add Live Refresh

Task 20. Add polling after the static dashboard works.

- Poll summary, state, activities, events, and observability.
- Do not poll the run list as often.

Test checkpoint:

- Updating run files causes the UI to refresh without a browser reload.

Task 21. Keep the polling behavior stable.

- Avoid flicker.
- Avoid duplicate rows.
- Avoid resetting scroll unexpectedly.

Test checkpoint:

- The page stays usable during repeated refreshes.

### Phase 7: Smooth Out Local Development

Task 22. Add frontend proxy configuration.

- Route `/api/*` from the React dev server to the local Python API.

Test checkpoint:

- The frontend calls `/api/...` without hardcoding the full backend URL.

Task 23. Add root helper scripts in [package.json](/Users/mike/projects/personal/SubagentWorkforce/package.json).

- Add a script for the API.
- Add a script for the frontend.
- Optionally add a combined dev script later, but that is not required for the first successful version.

Test checkpoint:

- A new developer can start both services from the repo without guessing commands.

## Recommended Testing Order

Use this exact order to reduce confusion:

1. API returns hardcoded JSON.
2. API returns real run list.
3. API returns one real run summary.
4. React app loads.
5. React app fetches run list.
6. React app selects one run.
7. React app renders summary.
8. React app renders state.
9. React app renders activities.
10. React app renders events.
11. React app renders observability.
12. Polling is added last.

This order matters because it proves the backend first, then selection, then rendering, then live behavior.

## Common Mistakes To Avoid

- Do not start with advanced features.
- Do not build the whole UI before the API is proven.
- Do not parse raw run files in React.
- Do not start with streaming.
- Do not combine write actions into the first version.
- Do not spend time on styling before data is flowing end-to-end.

## Definition of Done for Version 1

The first version is done when:

- the API starts locally
- the React app starts locally
- the browser shows a real run list
- selecting a run shows summary, state, activities, events, and observability
- the page refreshes that data on a polling interval
- no terminal dashboard is required to understand the selected run at a basic level

## Next Step After This Document

Once the minimal monitor is planned and the implementation begins, the next document should expand this baseline into:

- full feature list
- component breakdown
- richer API surface
- interaction design
- live streaming model if needed
- action endpoints if needed
