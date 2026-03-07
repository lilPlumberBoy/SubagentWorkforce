# Company Orchestrator

Local-first scaffolding for a company-style subagent orchestration system. The repo implements the initial runtime contracts, prompt-layer assembly, phase gating, bundle review, collaboration requests, change re-entry, and a smoke-test workflow.

## What Is Implemented

- A Python CLI for creating runs, decomposing goals, suggesting teams, generating role files, rendering prompts, executing tasks through Codex CLI, running objective/phase manager flows, reviewing bundles, creating phase reports, and managing change requests.
- A Codex CLI executor adapter that renders task prompts, runs `codex exec --json`, validates structured output, writes completion reports, and creates collaboration requests when an agent blocks on another team.
- On-disk JSON contracts for phase plans, objective maps, team registries, task assignments, completion reports, review bundles, collaboration requests, phase reports, change requests, and change proposals.
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
company-orchestrator plan-phase run-001 --sandbox read-only
company-orchestrator run-phase run-001 --sandbox read-only
```

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
5. `orchestrator/phase-overlays/<current-phase>.md`
6. The rendered `task-assignment.v1` JSON

Every render writes a prompt log under `runs/<run-id>/prompt-logs/`.

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
- Manager summaries are written under `runs/<run-id>/manager-runs/`.

## Objective Planning

`plan-objective` and `plan-phase` run an objective-manager through Codex to generate structured task decomposition.

- The manager returns `objective-plan.v1`.
- Python validates the plan, writes it under `runs/<run-id>/manager-plans/`, and materializes the generated `task-assignment.v1` files.
- The objective manager also defines `bundle_plan`, and `run-phase` now honors that bundle structure during acceptance review.
- Planning prompts are intended to be self-contained. The objective manager should use the injected runtime context and planning inputs directly rather than exploring the repository.
- Use `--replace` if you want a new manager plan to overwrite the current objective's tasks for the active phase.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## Current Scope

This repo now includes a live Codex CLI executor path, Codex-powered objective-manager planning for task decomposition, and deterministic manager orchestration for task scheduling, bundle assembly, acceptance review, and phase-report generation. Acceptance remains deterministic Python logic; it is not yet a live Codex reviewer.
