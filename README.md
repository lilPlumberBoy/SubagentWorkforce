# Company Orchestrator

Local-first scaffolding for a company-style subagent orchestration system. The repo implements the initial runtime contracts, prompt-layer assembly, phase gating, bundle review, collaboration requests, change re-entry, and a smoke-test workflow.

## What Is Implemented

- A Python CLI for creating runs, decomposing goals, suggesting teams, generating role files, rendering prompts, executing tasks through Codex CLI, running objective/phase manager flows, reviewing bundles, creating phase reports, and managing change requests.
- A Codex CLI executor adapter that renders task prompts, runs `codex exec --json`, validates structured output, writes completion reports, and creates collaboration requests when an agent blocks on another team.
- On-disk JSON contracts for phase plans, objective maps, team registries, task assignments, completion reports, review bundles, planned collaboration handoffs, collaboration requests, phase reports, change requests, and change proposals.
- Markdown role assets for base roles, capability overlays, phase overlays, and reusable templates.
- A smoke-test scaffold that creates two isolated objectives and verifies prompt inheritance, context echo reporting, and acceptance review.
- A test suite covering phase locks, objective isolation, bundle rejection on blocking collaboration, phase advancement gating, and change re-entry.

## Quick Start

Initialize a run from a goal file:

```bash
company-orchestrator init-run run-001 path/to/goal.md
company-orchestrator decompose-goal run-001
company-orchestrator suggest-teams run-001
company-orchestrator generate-roles run-001 --approve
company-orchestrator plan-phase run-001 --sandbox read-only --max-concurrency 3
company-orchestrator run-phase run-001 --sandbox read-only --max-concurrency 3
```

Or use the higher-level bootstrap path to initialize the run, generate roles, and immediately plan the first phase:

```bash
company-orchestrator bootstrap-run run-001 path/to/goal.md --sandbox read-only --max-concurrency 3 --watch
```

Or let the orchestrator keep going autonomously with policy guardrails:

```bash
company-orchestrator bootstrap-run run-001 path/to/goal.md --autonomous --approval-scope planning-only --stop-before-phase mvp-build --watch
company-orchestrator run-autonomous run-001 --approval-scope all --stop-before-phase polish --stop-on-recovery --watch
```

Start from the fill-in template at [orchestrator/templates/goal-template.md](/Users/mike/projects/personal/SubagentWorkforce/orchestrator/templates/goal-template.md). The example todo-app goal lives at [goal-draft.md](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/goal-draft.md).

- Keep the `## Objectives` heading exactly as written.
- Put one objective per bullet.
- Use concrete wording in those bullets so capability inference has something useful to work with.
- Add real detail in the later sections so discovery workers can identify boundaries, unknowns, risks, and dependencies without inventing facts.

Scaffold and verify the minimal communication test:

```bash
company-orchestrator scaffold-smoke-test --run-id smoke-demo
company-orchestrator run-phase smoke-demo --sandbox read-only
company-orchestrator approve-phase smoke-demo discovery
company-orchestrator advance-phase smoke-demo
```

Create and analyze a change request:

```bash
company-orchestrator create-change smoke-demo chg-001 "Need interface updates" --interface-changed
company-orchestrator analyze-change smoke-demo chg-001
company-orchestrator approve-change smoke-demo chg-001
company-orchestrator scaffold-delta smoke-demo chg-001
```

## Prompt Assembly

Prompts are assembled in this order:

1. `orchestrator/roles/base/company.md`
2. `orchestrator/roles/base/<manager|worker|acceptance-manager>.md`
3. `orchestrator/roles/capabilities/<capability>.md` when present
4. `orchestrator/roles/objectives/<objective-id>/approved/<role>.md` or `charter.md`
5. A prompt-kind-specific phase overlay:
   - `orchestrator/phase-overlays/<current-phase>/objective-planning.md`
   - `orchestrator/phase-overlays/<current-phase>/capability-planning.md`
   - `orchestrator/phase-overlays/<current-phase>/task-execution.md`
6. The rendered planning or task JSON payload

Every render writes a prompt log under `runs/<run-id>/prompt-logs/`.

Objective-specific roles can live either in the generic tree above or under an app-local tree such as `apps/<app>/orchestrator/roles/objectives/<objective-id>/...`.

## Executor Adapter

`execute-task` uses the local `codex exec` binary in non-interactive JSON mode.

- It removes `CODEX_API_KEY` and `OPENAI_API_KEY` from the subprocess environment so execution stays on the local ChatGPT-login CLI path.
- It passes the schema at `orchestrator/schemas/executor-response.v1.json` to Codex and converts the final structured response into `completion-report.v1`.
- If the response contains a `collaboration_request`, the adapter writes a new `collaboration-request.v1` file and links it from the completion report.
- Raw stdout and stderr from each Codex execution are logged under `runs/<run-id>/executions/`.

## Manager Runtime

`run-objective` and `run-phase` provide deterministic manager orchestration on top of the live executor.

- `run-objective` schedules all active-phase tasks for one objective, executes ready tasks, assembles the objective bundle, and runs acceptance review.
- `run-phase` does the same across every objective in the active phase, then writes the end-of-phase report automatically.
- Task dependencies declared in `depends_on` are respected before execution.
- Blocking collaboration handoffs are now treated as scheduler gates. Downstream tasks wait until the producing task/report satisfies the handoff deliverables.
- `--max-concurrency` controls how many safe tasks the controller may run at the same time. The default is `3`.
- Tasks that are not safe to parallelize fall back to serialized execution with a warning instead of failing the run.
- Code-writing tasks use run-scoped git worktree isolation. Accepted work lands on a dedicated run integration branch `codex/run-<run-id>`, not directly on your current branch.
- Manager summaries are written under `runs/<run-id>/manager-runs/`.

## Monitoring And Visualization

The orchestrator now writes live monitoring state under `runs/<run-id>/live/` while planning and execution are in progress.

- `run-state.json` tracks the active phase plus aggregate activity counts.
- `activities/<activity-id>.json` tracks the status, stage, progress, prompt path, and artifact paths for each planning or execution activity.
- `events.jsonl` captures normalized lifecycle events from the scheduler, planner, executor, bundle review, and phase reporting.
- `llm-calls.jsonl` records one structured observability entry per Codex call attempt, including latency, prompt size, queue wait, retries, timeout flags, and token usage.
- `observability.json` stores the aggregated run-level observability summary used by the dashboard and phase reports.
- `autonomy-history.jsonl` records one operator-readable audit entry per autonomous decision, including the applied policy, guidance snapshot, and any adaptive tuning decision.

Use the terminal monitor to inspect a run while it is active:

```bash
company-orchestrator watch-run run-001
company-orchestrator inspect-activity run-001 APP-A-DISC-001
company-orchestrator inspect-activity run-001 plan:discovery:app-a --follow
```

For convenience, the main execution commands can run with the dashboard attached in the same terminal and then print their normal JSON result when they finish:

```bash
company-orchestrator execute-task run-001 APP-A-DISC-001 --watch
company-orchestrator plan-phase run-001 --sandbox read-only --watch
company-orchestrator run-phase run-001 --sandbox read-only --watch
```

`watch-run` shows the run header, a `Next Action` panel, summary counts, objective progress, active planning activities, active task activities, queued work, blocked work, collaboration handoffs, parallelism warnings, and a phase-level progress bar.

The `Autonomy` panel now also shows:
- approval scope
- stop-before phases
- whether the run stops on recovery
- whether adaptive tuning is enabled
- the last tuning decision, when one was applied
- the autonomy audit-log path

It also includes an `LLM Observability` panel with:
- total/completed/failed/timed out calls
- input, cached-input, and output token totals
- total prompt chars and lines
- average/max latency
- average queue wait
- retry counts
- active process count
- call counts by activity kind

The `Next Action` panel and command JSON output both surface:
- `run_status`
- `run_status_reason`
- `next_action_command`
- `next_action_reason`
- `review_doc_path`

This is intended to tell the human exactly what to do next when a run is:
- actively working
- recoverable after interruption or stale planning
- ready for review
- ready to advance

Operator commands now print a compact human summary by default. Pass `--json` to any CLI command when you need the full machine-readable payload instead.

`inspect-activity` shows the activity metadata, full rendered prompt, latest live events, parallel fallback warnings, and the paths to stdout, stderr, workspace, branch, and the final output artifact.

Completed activity history is persisted under:

- `runs/<run-id>/live/activity-history.jsonl`

When an objective belongs to an app-local orchestrator tree under `apps/<app>/orchestrator/roles/objectives/...`, the same history entries are also mirrored to:

- `apps/<app>/orchestrator/activity-logs/<run-id>.jsonl`

Recovery-aware monitoring is also built in:

- activities can be marked `interrupted`, `recovering`, `recovered`, or `abandoned`
- interrupted and recovered work appears in dedicated dashboard sections
- activity detail views show attempt count, status reason, and recovery action
- normal long-running commands reconcile stale state before starting new work

Operator recovery commands:

```bash
company-orchestrator reconcile-run run-001
company-orchestrator reconcile-run run-001 --apply
company-orchestrator resume-phase run-001 --sandbox read-only --max-concurrency 3
company-orchestrator retry-activity run-001 APP-A-DISC-001 --sandbox read-only
```

`reconcile-run` is a dry run by default and reports what would be reclassified. `resume-phase` applies reconciliation and continues safe incomplete work for the active phase. `retry-activity` starts a new attempt for a specific interrupted activity while preserving attempt lineage in the live dashboard and event history.

## Autonomous Mode

`run-autonomous` uses the same planning/execution/recovery engine as the manual CLI, but it can move a run forward without manual approval steps when policy allows it.

Autonomy policy controls:
- `--approval-scope {all,planning-only,none}`
- `--stop-before-phase <phase>` (repeatable)
- `--stop-on-recovery`
- `--no-adaptive-tuning`
- `--max-iterations N`

Examples:

```bash
company-orchestrator run-autonomous run-001 --approval-scope planning-only --stop-before-phase mvp-build
company-orchestrator run-autonomous run-001 --approval-scope all --stop-before-phase polish --stop-on-recovery
```

Autonomous mode writes persistent state to:
- `runs/<run-id>/autonomy.json`
- `runs/<run-id>/live/autonomy-history.jsonl`

The audit history is intended to answer:
- what action autonomy took
- why it took that action
- which policy was active
- what the run guidance said at the time
- whether adaptive tuning changed concurrency

## Objective Planning

`plan-objective` and `plan-phase` now run a two-stage planning flow through Codex:

- The objective manager returns an `objective-outline.v1` describing capability lanes and cross-lane coordination.
- One capability manager per lane then returns `capability-plan.v1` task bundles for that lane.
- Python aggregates those capability plans into the final `objective-plan.v1`, validates it, writes it under `runs/<run-id>/manager-plans/`, and materializes the generated `task-assignment.v1` files.
- Capability managers are expected to emit concrete `owned_paths`, `shared_asset_ids`, and `collaboration_handoffs`, not just lane-local task lists.
- Planned cross-lane handoffs are materialized under `runs/<run-id>/collaboration-plans/` as `collaboration-handoff.v1` artifacts, consumed by the scheduler as real readiness gates, and summarized in phase reports.
- `plan-objective --max-concurrency N` can run multiple capability-manager planners at once for the same objective.
- `plan-phase --max-concurrency N` can run multiple objectives at once while sharing a bounded pool of planning slots across nested objective and capability managers.
- `run-phase` continues to honor the resulting `bundle_plan` during deterministic acceptance review.
- Planning prompts are intended to be self-contained. Objective and capability managers should use the injected runtime context and planning inputs directly rather than exploring the repository.
- Planning prompts now use a compact goal/context view for manager reasoning, while worker input resolution still retains the full run payload.
- Planning prompt compaction is observability-driven. When recent planning calls in the same phase are large, slow, or timed out, later prompts automatically switch from `standard` to `compact` or `aggressive` payload shaping.
- Autonomous mode uses the same observability to tune runtime behavior. If recent planning or execution calls in the active phase timed out or became slow, autonomy can reduce its effective concurrency before launching the next action.
- Use `--replace` if you want a new manager plan to overwrite the current objective's tasks for the active phase.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## Todo Runtime

Run the integrated todo MVP locally with one command:

```bash
npm run todo-runtime:start
```

The runtime starts the backend first, then a small frontend host that serves the React todo page and points it at the backend API without source edits. The wiring externalizes the live backend URL into the frontend runtime and applies the backend allow-origin setting for that frontend origin automatically.

Optional runtime environment variables:
- `TODO_BACKEND_HOST` defaults to `127.0.0.1`
- `TODO_BACKEND_PORT` defaults to `3000`
- `TODO_FRONTEND_HOST` defaults to `127.0.0.1`
- `TODO_FRONTEND_PORT` defaults to `4173`
- `TODO_BACKEND_DB_PATH` defaults to `apps/todo/backend/data/todos.sqlite`

The command prints one JSON line with the backend URL, frontend URL, and resolved database path, then keeps both servers running until interrupted.

Runtime-specific validation commands:

```bash
npm run validate:todo-e2e-smoke
npm run validate:todo-release-readiness
npm run validate:todo-review-evidence
npm run validate:todo-runtime-connectivity
npm run validate:todo-runtime-startup
```

Review bundle notes for the integrated todo MVP live in [mvp-integration-review-evidence.md](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/mvp-integration-review-evidence.md).

Polish-phase release handoff notes live in:
- [frontend release readiness](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/docs/polish/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/frontend-release-readiness.md)
- [backend operations handoff](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/docs/polish/simple-backend-api-and-persistence-layer-for-storing-todo-items/backend-operations-handoff.md)
- [integration release checklist](/Users/mike/projects/personal/SubagentWorkforce/apps/todo/docs/polish/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/release-checklist.md)

## App Layout

Generic orchestration assets stay at the repo root:
- `company_orchestrator/`
- `orchestrator/`
- `runs/`
- `tests/`

App-specific assets for the todo example now live under:
- `apps/todo/backend/`
- `apps/todo/frontend/`
- `apps/todo/runtime/`
- `apps/todo/scripts/`
- `apps/todo/docs/`
- `apps/todo/orchestrator/`
- `apps/todo/goal-draft.md`

## Current Scope

This repo now includes a live Codex CLI executor path, Codex-powered objective-manager and capability-manager planning for task decomposition, parallel-safe task execution with worktree isolation, live monitoring, and deterministic manager orchestration for task scheduling, bundle assembly, acceptance review, and phase-report generation. Acceptance remains deterministic Python logic; it is not yet a live Codex reviewer.
