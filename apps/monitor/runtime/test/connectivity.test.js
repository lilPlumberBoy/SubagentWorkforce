const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { startMonitorRuntime } = require('../src/runtime');
const { loadMonitorRuntimePage } = require('./browser-helpers');

const PYTHON_COMMAND =
  process.env.MONITOR_API_PYTHON || process.env.PYTHON || 'python3';
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

test('runtime connectivity serves the monitor frontend and renders live run data from the API', async () => {
  const projectRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-runtime-project-'));
  createFixtureProject(projectRoot);

  const runtime = await startMonitorRuntime({
    apiPort: 0,
    frontendPort: 0,
    projectRoot,
    pythonCommand: PYTHON_COMMAND,
  });

  try {
    const app = await loadMonitorRuntimePage(runtime.frontend.url);

    try {
      await app.waitFor(() => {
        assert.ok(app.getByText('Run Monitor'));
        assert.ok(app.getByText('monitor-ui'));
        assert.ok(app.getByText('Next Action'));
        assert.ok(app.getByText('Objective Progress'));
        assert.ok(app.getByText('Activity History'));
        assert.ok(app.getByText('App A smoke task'));
      });

      await app.clickButton('App A smoke task');

      await app.waitFor(() => {
        assert.ok(app.getByText('Final Response'));
        assert.ok(app.getByText('Structured Output'));
        assert.ok(app.getByText('Collect evidence for the smoke task.'));
        assert.ok(app.getByText('Task completed successfully.'));
      });
    } finally {
      app.cleanup();
    }
  } finally {
    await runtime.close();
    fs.rmSync(projectRoot, { force: true, recursive: true });
  }
});

function createFixtureProject(projectRoot) {
  const result = spawnSync(
    PYTHON_COMMAND,
    [
      '-c',
      `
from pathlib import Path
import sys

from company_orchestrator.autonomy import update_autonomy_state
from company_orchestrator.filesystem import write_json, write_text
from company_orchestrator.live import ensure_activity, initialize_live_run, record_event
from company_orchestrator.planner import initialize_run
from company_orchestrator.smoke import smoke_task

project_root = Path(sys.argv[1])
run_id = "monitor-ui"
(project_root / "orchestrator").symlink_to(Path(sys.argv[2]) / "orchestrator", target_is_directory=True)

run_dir = initialize_run(
    project_root,
    run_id,
    "# Monitor UI Goal\\n\\n## Objectives\\n- App A context verification\\n- App B context verification\\n",
)
write_json(
    run_dir / "objective-map.json",
    {
        "schema": "objective-map.v1",
        "run_id": run_id,
        "objectives": [
            {
                "objective_id": "app-a",
                "title": "App A context verification",
                "summary": "App A context verification",
                "status": "approved",
                "capabilities": ["frontend"],
            },
            {
                "objective_id": "app-b",
                "title": "App B context verification",
                "summary": "App B context verification",
                "status": "approved",
                "capabilities": ["backend"],
            },
        ],
        "dependencies": [],
    },
)
for task in [
    smoke_task(run_id, "app-a", "frontend", "APP-A-SMOKE-001"),
    smoke_task(run_id, "app-b", "backend", "APP-B-SMOKE-001"),
]:
    write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
initialize_live_run(project_root, run_id)
prompt_path = f"runs/{run_id}/prompt-logs/APP-A-SMOKE-001.prompt.md"
write_text(project_root / prompt_path, "# Prompt\\nCollect evidence for the smoke task.")
write_text(
    project_root / "runs" / run_id / "executions" / "APP-A-SMOKE-001.last-message.json",
    '{"status":"ready_for_bundle_review","summary":"Task completed successfully."}\\n',
)
write_json(
    project_root / "runs" / run_id / "executions" / "APP-A-SMOKE-001.json",
    {
        "task_id": "APP-A-SMOKE-001",
        "last_message_path": f"runs/{run_id}/executions/APP-A-SMOKE-001.last-message.json",
        "report_path": f"runs/{run_id}/reports/APP-A-SMOKE-001.json",
    },
)
write_json(
    project_root / "runs" / run_id / "reports" / "APP-A-SMOKE-001.json",
    {
        "status": "ready_for_bundle_review",
        "summary": "Smoke task report",
        "artifacts": [],
    },
)

ensure_activity(
    project_root,
    run_id,
    activity_id="plan:discovery:app-a",
    kind="objective_plan",
    entity_id="app-a",
    phase="discovery",
    objective_id="app-a",
    display_name="Discovery planning for app-a",
    assigned_role="objectives.app-a.objective-manager",
    status="running",
    current_activity="Drafting the objective plan.",
)
ensure_activity(
    project_root,
    run_id,
    activity_id="APP-A-SMOKE-001",
    kind="task_execution",
    entity_id="APP-A-SMOKE-001",
    phase="discovery",
    objective_id="app-a",
    display_name="App A smoke task",
    assigned_role="objectives.app-a.frontend-worker",
    status="running",
    current_activity="Collecting context evidence.",
    prompt_path=prompt_path,
    warnings=[{"code": "parallel_fallback", "message": "Task was serialized after a safety check."}],
    parallel_execution_requested=True,
    parallel_execution_granted=False,
    parallel_fallback_reason="Parallel safety classifier denied concurrent execution.",
)
record_event(
    project_root,
    run_id,
    phase="discovery",
    activity_id="APP-A-SMOKE-001",
    event_type="task.started",
    message="Smoke task execution started.",
)
ensure_activity(
    project_root,
    run_id,
    activity_id="APP-B-SMOKE-001",
    kind="task_execution",
    entity_id="APP-B-SMOKE-001",
    phase="discovery",
    objective_id="app-b",
    display_name="App B smoke task",
    assigned_role="objectives.app-b.backend-worker",
    status="blocked",
    current_activity="Waiting on a cross-capability handoff.",
    status_reason="Missing source deliverable.",
)
ensure_activity(
    project_root,
    run_id,
    activity_id="APP-A-RECOVERY-001",
    kind="task_execution",
    entity_id="APP-A-RECOVERY-001",
    phase="discovery",
    objective_id="app-a",
    display_name="Recovered smoke replay",
    assigned_role="objectives.app-a.frontend-worker",
    status="recovered",
    current_activity="Recovered after workspace recreation.",
    status_reason="Recovered from an interrupted workspace.",
    recovery_action="recreated_workspace",
)
write_json(
    project_root / "runs" / run_id / "collaboration-plans" / "HOF-001.json",
    {
        "schema": "collaboration-handoff.v1",
        "run_id": run_id,
        "phase": "discovery",
        "objective_id": "app-a",
        "handoff_id": "HOF-001",
        "from_capability": "frontend",
        "to_capability": "backend",
        "from_task_id": "APP-A-SMOKE-001",
        "to_role": "objectives.app-b.backend-worker",
        "handoff_type": "artifact",
        "reason": "Backend task depends on frontend output.",
        "deliverables": [
            {
                "kind": "artifact",
                "output_id": "frontend.discovery.brief",
                "path": "runs/monitor-ui/reports/APP-A-SMOKE-001.json",
                "asset_id": None,
                "description": "Smoke artifact placeholder.",
                "evidence": {
                    "validation_ids": [],
                    "artifact_paths": [],
                },
            }
        ],
        "blocking": True,
        "shared_asset_ids": [],
        "status": "blocked",
        "to_task_ids": ["APP-B-SMOKE-001"],
        "satisfied_by_task_ids": [],
        "missing_deliverables": ["frontend.discovery.brief"],
        "status_reason": "Missing required handoff deliverables.",
        "last_checked_at": "2026-04-13T12:00:00Z",
    },
)
update_autonomy_state(
    project_root,
    run_id,
    enabled=True,
    status="running",
    approval_scope="planning-only",
    stop_before_phases=["polish"],
    active_phase="discovery",
    last_action="run-phase",
    last_action_status="working",
)
`,
      projectRoot,
      REPO_ROOT,
    ],
    {
      cwd: REPO_ROOT,
      encoding: 'utf8',
    }
  );

  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || 'Fixture creation failed.');
  }
}
