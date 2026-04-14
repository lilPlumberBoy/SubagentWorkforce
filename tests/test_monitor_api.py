from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path

from company_orchestrator.autonomy import update_autonomy_state
from company_orchestrator.filesystem import write_json, write_text
from company_orchestrator.live import ensure_activity, initialize_live_run, record_event, update_activity
from company_orchestrator.monitor_api import (
    build_activity_detail_payload,
    build_prompt_debug_payload,
    build_run_dashboard_payload,
    list_runs_payload,
    select_recent_history_entries,
    summarize_repair_context,
    summarize_stdout_failure,
    start_monitor_api_server,
)
from company_orchestrator.planner import initialize_run
from company_orchestrator.smoke import smoke_task


class MonitorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_directory.name)
        self.repo_root = Path(__file__).resolve().parent.parent
        (self.project_root / "orchestrator").symlink_to(
            self.repo_root / "orchestrator",
            target_is_directory=True,
        )
        self.run_id = "monitor-fixture"
        run_dir = initialize_run(
            self.project_root,
            self.run_id,
            "# Monitor Fixture Goal\n\n## Objectives\n- App A context verification\n- App B context verification\n",
        )
        write_json(
            run_dir / "objective-map.json",
            {
                "schema": "objective-map.v1",
                "run_id": self.run_id,
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
            smoke_task(self.run_id, "app-a", "frontend", "APP-A-SMOKE-001"),
            smoke_task(self.run_id, "app-b", "backend", "APP-B-SMOKE-001"),
        ]:
            write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        initialize_live_run(self.project_root, self.run_id)
        self.prompt_path = f"runs/{self.run_id}/prompt-logs/APP-A-SMOKE-001.prompt.md"
        write_text(self.project_root / self.prompt_path, "# Prompt\nCollect evidence for the smoke task.")
        self.plan_prompt_path = f"runs/{self.run_id}/manager-plans/discovery-app-a.prompt.md"
        write_text(
            self.project_root / self.plan_prompt_path,
            "# Planning Prompt\nDraft the discovery objective plan.",
        )

        ensure_activity(
            self.project_root,
            self.run_id,
            activity_id="plan:discovery:app-a",
            kind="objective_plan",
            entity_id="app-a",
            phase="discovery",
            objective_id="app-a",
            display_name="Discovery planning for app-a",
            assigned_role="objectives.app-a.objective-manager",
            status="running",
            current_activity="Drafting the objective plan.",
            prompt_path=self.plan_prompt_path,
            output_path=f"runs/{self.run_id}/manager-plans/discovery-app-a.json",
        )
        ensure_activity(
            self.project_root,
            self.run_id,
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="App A smoke task",
            assigned_role="objectives.app-a.frontend-worker",
            status="running",
            current_activity="Collecting context evidence.",
            prompt_path=self.prompt_path,
            warnings=[{"code": "parallel_fallback", "message": "Task was serialized after a safety check."}],
            parallel_execution_requested=True,
            parallel_execution_granted=False,
            parallel_fallback_reason="Parallel safety classifier denied concurrent execution.",
        )
        record_event(
            self.project_root,
            self.run_id,
            phase="discovery",
            activity_id="APP-A-SMOKE-001",
            event_type="task.started",
            message="Smoke task execution started.",
        )
        ensure_activity(
            self.project_root,
            self.run_id,
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
            self.project_root,
            self.run_id,
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
            attempt=2,
            prompt_path=f"runs/{self.run_id}/prompt-logs/APP-A-RECOVERY-001.repair-1.prompt.md",
            stdout_path=f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.stdout.jsonl",
            output_path=f"runs/{self.run_id}/reports/APP-A-RECOVERY-001.repair-1.json",
        )

        write_json(
            self.project_root / "runs" / self.run_id / "collaboration-plans" / "HOF-001.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": self.run_id,
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
                        "path": "runs/monitor-fixture/reports/APP-A-SMOKE-001.json",
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
        write_text(
            self.project_root / "runs" / self.run_id / "executions" / "APP-A-SMOKE-001.last-message.json",
            '{"status":"ready_for_bundle_review","summary":"Task completed successfully."}\n',
        )
        write_json(
            self.project_root / "runs" / self.run_id / "executions" / "APP-A-SMOKE-001.json",
            {
                "task_id": "APP-A-SMOKE-001",
                "last_message_path": f"runs/{self.run_id}/executions/APP-A-SMOKE-001.last-message.json",
                "report_path": f"runs/{self.run_id}/reports/APP-A-SMOKE-001.json",
            },
        )
        write_json(
            self.project_root / "runs" / self.run_id / "reports" / "APP-A-SMOKE-001.json",
            {
                "status": "ready_for_bundle_review",
                "summary": "Smoke task report",
                "artifacts": [],
            },
        )
        write_json(
            self.project_root / "runs" / self.run_id / "manager-plans" / "discovery-app-a.last-message.json",
            {
                "summary": "Discovery objective plan drafted.",
                "tasks": [],
            },
        )
        write_json(
            self.project_root / "runs" / self.run_id / "manager-plans" / "discovery-app-a.summary.json",
            {
                "objective_id": "app-a",
                "last_message_path": f"runs/{self.run_id}/manager-plans/discovery-app-a.last-message.json",
                "plan_path": f"runs/{self.run_id}/manager-plans/discovery-app-a.json",
            },
        )
        write_json(
            self.project_root / "runs" / self.run_id / "manager-plans" / "discovery-app-a.json",
            {
                "summary": "Objective plan output",
                "tasks": [],
            },
        )
        self.repair_prompt_path = f"runs/{self.run_id}/prompt-logs/APP-A-RECOVERY-001.repair-1.prompt.md"
        write_text(
            self.project_root / self.repair_prompt_path,
            "# Repair Assignment\n\nRedo the same task in a clean workspace and verify the validation command passes.\n",
        )
        write_text(
            self.project_root / f"runs/{self.run_id}/prompt-logs/APP-A-RECOVERY-001.prompt.md",
            "# Original Prompt\n\nRun the smoke replay validation.\n",
        )
        write_text(
            self.project_root / f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.stdout.jsonl",
            "\n".join(
                [
                    '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc \'npm run validate:todo-frontend-reload\'","aggregated_output":"✖ todo reload validation\\nAssertionError [ERR_ASSERTION]: Expected persisted todos to survive reload.\\n    at TestContext.<anonymous> (apps/todo/frontend/test/reload.test.js:18:12)\\n","exit_code":1,"status":"failed"}}',
                    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \'npm run validate:todo-frontend-reload\'","aggregated_output":"retry output that should not be chosen\\n","exit_code":0,"status":"completed"}}',
                ]
            )
            + "\n",
        )
        write_text(
            self.project_root / f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.stdout.jsonl",
            '{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"/bin/zsh -lc \'npm run validate:todo-frontend-reload\'","aggregated_output":"validation passed\\n","exit_code":0,"status":"completed"}}\n',
        )
        write_json(
            self.project_root / f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.last-message.json",
            {
                "status": "ready_for_bundle_review",
                "summary": "Recovered smoke replay completed.",
            },
        )
        write_json(
            self.project_root / f"runs/{self.run_id}/reports/APP-A-RECOVERY-001.repair-1.json",
            {
                "status": "ready_for_bundle_review",
                "summary": "Recovered smoke replay report",
            },
        )
        write_json(
            self.project_root / f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.json",
            {
                "task_id": "APP-A-RECOVERY-001",
                "stdout_path": f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.stdout.jsonl",
                "last_message_path": f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.last-message.json",
                "report_path": f"runs/{self.run_id}/reports/APP-A-RECOVERY-001.repair-1.json",
            },
        )
        update_autonomy_state(
            self.project_root,
            self.run_id,
            enabled=True,
            status="running",
            approval_scope="planning-only",
            stop_before_phases=["polish"],
            active_phase="discovery",
            last_action="run-phase",
            last_action_status="working",
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_dashboard_payload_contains_monitor_sections(self) -> None:
        payload = build_run_dashboard_payload(self.project_root, self.run_id, events_limit=10)

        self.assertEqual(payload["run"]["run_id"], self.run_id)
        self.assertEqual(payload["guidance"]["run_status"], "working")
        self.assertEqual(payload["autonomy"]["controller_status"], "running")
        self.assertTrue(payload["activities"]["active_planning"])
        self.assertTrue(payload["activities"]["active_tasks"])
        self.assertTrue(payload["activities"]["blocked_tasks"])
        self.assertTrue(payload["activities"]["interrupted_or_recovered"])
        self.assertTrue(payload["handoffs"])
        self.assertTrue(payload["warnings"])
        self.assertTrue(payload["recovery"])
        self.assertTrue(payload["history"])
        self.assertEqual(payload["history"][0]["phase"], "discovery")
        self.assertTrue(payload["events"])

    def test_activity_detail_payload_includes_recent_events(self) -> None:
        payload = build_activity_detail_payload(self.project_root, self.run_id, "APP-A-SMOKE-001", events_limit=5)

        self.assertEqual(payload["activity"]["activity_id"], "APP-A-SMOKE-001")
        self.assertEqual(payload["artifacts"]["prompt_path"], self.prompt_path)
        self.assertEqual(payload["events"][-1]["event_type"], "task.started")

    def test_prompt_debug_payload_returns_prompt_and_response_for_task(self) -> None:
        payload = build_prompt_debug_payload(self.project_root, self.run_id, "APP-A-SMOKE-001")

        self.assertEqual(payload["prompt_path"], self.prompt_path)
        self.assertIn("Collect evidence for the smoke task.", payload["prompt_text"])
        self.assertEqual(
            payload["response_path"],
            f"runs/{self.run_id}/executions/APP-A-SMOKE-001.last-message.json",
        )
        self.assertIn("Task completed successfully.", payload["response_text"])
        self.assertEqual(
            payload["structured_output_path"],
            f"runs/{self.run_id}/reports/APP-A-SMOKE-001.json",
        )
        self.assertIn("Smoke task report", payload["structured_output_text"])

    def test_prompt_debug_payload_returns_prompt_and_response_for_planning(self) -> None:
        payload = build_prompt_debug_payload(self.project_root, self.run_id, "plan:discovery:app-a")

        self.assertEqual(payload["prompt_path"], self.plan_prompt_path)
        self.assertIn("Draft the discovery objective plan.", payload["prompt_text"])
        self.assertEqual(
            payload["response_path"],
            f"runs/{self.run_id}/manager-plans/discovery-app-a.last-message.json",
        )
        self.assertIn("Discovery objective plan drafted.", payload["response_text"])
        self.assertEqual(
            payload["structured_output_path"],
            f"runs/{self.run_id}/manager-plans/discovery-app-a.json",
        )
        self.assertIn("Objective plan output", payload["structured_output_text"])

    def test_prompt_debug_payload_recovers_planning_prompt_path_when_live_state_lost_it(self) -> None:
        update_activity(
            self.project_root,
            self.run_id,
            "plan:discovery:app-a",
            prompt_path=None,
        )

        payload = build_prompt_debug_payload(self.project_root, self.run_id, "plan:discovery:app-a")

        self.assertEqual(
            payload["prompt_path"],
            f"runs/{self.run_id}/manager-plans/discovery-app-a.prompt.md",
        )
        self.assertIn("Draft the discovery objective plan.", payload["prompt_text"])

    def test_summarize_stdout_failure_distills_failed_command_output(self) -> None:
        payload = summarize_stdout_failure(
            self.project_root,
            f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.stdout.jsonl",
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["command"], "npm run validate:todo-frontend-reload")
        self.assertIn("AssertionError [ERR_ASSERTION]", payload["summary"])
        self.assertIn("Expected persisted todos to survive reload", payload["excerpt"])

    def test_prompt_debug_payload_uses_original_stdout_failure_for_repair_summary(self) -> None:
        payload = build_prompt_debug_payload(self.project_root, self.run_id, "APP-A-RECOVERY-001")

        self.assertEqual(payload["prompt_path"], self.repair_prompt_path)
        self.assertEqual(
            payload["stdout_path"],
            f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.repair-1.stdout.jsonl",
        )
        self.assertIsNotNone(payload["repair_context"])
        self.assertEqual(
            payload["repair_context"]["failure_command"],
            "npm run validate:todo-frontend-reload",
        )
        self.assertIn(
            "Expected persisted todos to survive reload",
            payload["repair_context"]["failure_summary"],
        )
        self.assertIn(
            "AssertionError [ERR_ASSERTION]",
            payload["repair_context"]["failure_excerpt"],
        )
        self.assertEqual(
            payload["repair_context"]["failure_stdout_path"],
            f"runs/{self.run_id}/executions/APP-A-RECOVERY-001.stdout.jsonl",
        )

    def test_summarize_repair_context_distills_failure_and_repair_request(self) -> None:
        repair_prompt = """
# Repair Assignment

You are repairing a previously returned planning response.
The previous response was not accepted because it failed deterministic validation.
Your job in this turn is to redo the same planning turn while correcting the invalid parts of the previous response.
Preserve as much of the previous valid plan as possible.

# What Failed In The Previous Response

- Schema: `capability-plan.response.v1`
- Validation error: Capability plan contradicted the observed backend workspace language.
""".strip()

        repair_context = summarize_repair_context(
            {
                "attempt": 2,
                "recovery_action": "planning_repair",
                "status_reason": "planning_stalled",
            },
            {"is_repair": True},
            repair_prompt,
        )

        self.assertIsNotNone(repair_context)
        self.assertIn("contradicted the observed backend workspace language", repair_context["failure_summary"])
        self.assertIn("redo the same planning turn", repair_context["repair_request_summary"])
        self.assertEqual(repair_context["recovery_action"], "Planning repair")

    def test_summarize_repair_context_humanizes_task_retry_fallbacks(self) -> None:
        repair_context = summarize_repair_context(
            {
                "attempt": 2,
                "display_name": "frontend_todo_crud_verification",
                "current_activity": "Running command: /bin/zsh -lc 'npm run validate:todo-frontend-reload'",
                "recovery_action": "recreated_workspace",
                "status_reason": None,
            },
            {"is_repair": True},
            None,
        )

        self.assertIsNotNone(repair_context)
        self.assertIn("workspace or local environment became unusable", repair_context["failure_summary"])
        self.assertIn("clean workspace", repair_context["repair_request_summary"])
        self.assertNotIn("Running command:", repair_context["repair_request_summary"])

    def test_run_list_payload_summarizes_runs(self) -> None:
        payload = list_runs_payload(self.project_root)

        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["run_id"], self.run_id)
        self.assertEqual(payload["runs"][0]["controller_status"], "running")
        self.assertIsNotNone(payload["runs"][0]["started_at"])
        self.assertGreaterEqual(payload["runs"][0]["active_activity_count"], 1)

    def test_run_list_payload_sorts_by_run_start_time_descending(self) -> None:
        newer_run = initialize_run(
            self.project_root,
            "monitor-fixture-newer",
            "# Newer Run\n\n## Objectives\n- Verify sorting\n",
        )
        older_goal = self.project_root / "runs" / self.run_id / "goal.md"
        newer_goal = newer_run / "goal.md"
        os.utime(older_goal, (1_700_000_000, 1_700_000_000))
        os.utime(newer_goal, (1_800_000_000, 1_800_000_000))

        payload = list_runs_payload(self.project_root)

        self.assertEqual(
            [item["run_id"] for item in payload["runs"]],
            ["monitor-fixture-newer", self.run_id],
        )

    def test_api_root_explains_that_frontend_and_api_are_different(self) -> None:
        server = start_monitor_api_server(self.project_root, port=0)

        try:
            with urlopen(f"{server.url}/") as response:
                payload = response.read().decode("utf-8")
        finally:
            server.close()

        self.assertIn('"service": "monitor-api"', payload)
        self.assertIn("not the browser frontend", payload)
        self.assertIn('"/api/runs"', payload)

    def test_api_open_file_opens_project_relative_path(self) -> None:
        server = start_monitor_api_server(self.project_root, port=0)

        try:
            with patch("company_orchestrator.monitor_api.open_local_path") as open_local_path:
                request = Request(
                    f"{server.url}/api/open-file",
                    data=b'{"path":"runs/monitor-fixture/prompt-logs/APP-A-SMOKE-001.prompt.md"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    payload = response.read().decode("utf-8")
        finally:
            server.close()

        open_local_path.assert_called_once()
        self.assertIn('"opened": true', payload)
        self.assertIn(self.prompt_path, payload)

    def test_api_open_file_rejects_paths_outside_project_root(self) -> None:
        server = start_monitor_api_server(self.project_root, port=0)

        try:
            request = Request(
                f"{server.url}/api/open-file",
                data=b'{"path":"../outside.txt"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request)
        finally:
            server.close()

        self.assertEqual(context.exception.code, 400)
        payload = context.exception.read().decode("utf-8")
        self.assertIn("Only files inside the project root can be opened.", payload)

    def test_select_recent_history_entries_preserves_recent_groups_from_each_phase(self) -> None:
        history = []
        for index in range(6):
            history.append(
                {
                    "activity_id": f"mvp-{index}",
                    "phase": "mvp-build",
                    "timestamp": f"2026-04-13T18:{50 + index:02d}:00Z",
                }
            )
        history.extend(
            [
                {
                    "activity_id": "design-1",
                    "phase": "design",
                    "timestamp": "2026-04-13T17:40:00Z",
                },
                {
                    "activity_id": "discovery-1",
                    "phase": "discovery",
                    "timestamp": "2026-04-13T16:30:00Z",
                },
            ]
        )

        selected = select_recent_history_entries(history, groups_per_phase=2)
        selected_pairs = {(entry["phase"], entry["activity_id"]) for entry in selected}

        self.assertIn(("mvp-build", "mvp-5"), selected_pairs)
        self.assertIn(("mvp-build", "mvp-4"), selected_pairs)
        self.assertIn(("design", "design-1"), selected_pairs)
        self.assertIn(("discovery", "discovery-1"), selected_pairs)


if __name__ == "__main__":
    unittest.main()
