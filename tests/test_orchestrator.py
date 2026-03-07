from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from company_orchestrator.bundles import assemble_review_bundle, review_bundle
from company_orchestrator.changes import analyze_change_request, approve_change, create_change_request, scaffold_delta_run
from company_orchestrator.collaboration import create_collaboration_request, resolve_collaboration_request
from company_orchestrator.executor import ExecutorError, execute_task
from company_orchestrator.filesystem import read_json, write_json
from company_orchestrator.management import run_phase
from company_orchestrator.objective_planner import build_planning_prompt, plan_objective, plan_phase
from company_orchestrator.planner import generate_role_files, initialize_run, suggest_team_proposals
from company_orchestrator.prompts import render_prompt
from company_orchestrator.reports import advance_phase, generate_phase_report, record_human_approval
from company_orchestrator.smoke import scaffold_smoke_test, simulate_context_echo_completion, verify_smoke_reports


REPO_ROOT = Path(__file__).resolve().parent.parent


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        shutil.copytree(REPO_ROOT / "orchestrator", self.project_root / "orchestrator")
        (self.project_root / "runs").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_prompt_inheritance_for_smoke_task(self) -> None:
        scaffold_smoke_test(self.project_root, "smoke")
        metadata = read_json(self.project_root / "runs" / "smoke" / "prompt-logs" / "APP-A-SMOKE-001.json")
        self.assertEqual(metadata["phase"], "discovery")
        self.assertIn("orchestrator/roles/base/company.md", metadata["files_loaded"])
        self.assertIn("orchestrator/roles/base/worker.md", metadata["files_loaded"])
        self.assertIn("orchestrator/roles/capabilities/frontend.md", metadata["files_loaded"])
        self.assertIn("orchestrator/phase-overlays/discovery.md", metadata["files_loaded"])

    def test_phase_lock_rejects_future_phase_task(self) -> None:
        run_dir = initialize_run(self.project_root, "phase-lock", "# Goal\n\n## Objectives\n- App A")
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "phase-lock",
            "objectives": [
                {
                    "objective_id": "app-a",
                    "title": "App A",
                    "summary": "App A",
                    "status": "approved",
                    "capabilities": ["frontend"],
                }
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "phase-lock")
        generate_role_files(self.project_root, "phase-lock", approve=True)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "phase-lock",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-DES-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Illegal future phase work",
            "inputs": [],
            "expected_outputs": [],
            "done_when": ["should fail because the phase is locked"],
            "depends_on": [],
            "validation": [{"id": "noop", "command": "noop"}],
            "collaboration_rules": []
        }
        task_path = run_dir / "tasks" / "APP-A-DES-001.json"
        write_json(task_path, task)
        with self.assertRaisesRegex(ValueError, "does not match active phase"):
            render_prompt(self.project_root, "phase-lock", task_path)

    def test_bundle_rejects_mixed_objectives(self) -> None:
        scaffold_smoke_test(self.project_root, "mixed")
        simulate_context_echo_completion(self.project_root, "mixed", "APP-A-SMOKE-001")
        simulate_context_echo_completion(self.project_root, "mixed", "APP-B-SMOKE-001")
        report_a = self.project_root / "runs" / "mixed" / "reports" / "APP-A-SMOKE-001.json"
        report_b = self.project_root / "runs" / "mixed" / "reports" / "APP-B-SMOKE-001.json"
        with self.assertRaisesRegex(ValueError, "one objective"):
            assemble_review_bundle(
                self.project_root,
                "mixed",
                "MIXED-BUNDLE",
                [report_a, report_b],
                "objectives.mixed.objective-manager",
                "objectives.mixed.acceptance-manager",
            )

    def test_bundle_review_blocks_unresolved_collaboration(self) -> None:
        scaffold_smoke_test(self.project_root, "bundle")
        simulate_context_echo_completion(self.project_root, "bundle", "APP-A-SMOKE-001")
        report_path = self.project_root / "runs" / "bundle" / "reports" / "APP-A-SMOKE-001.json"
        report = read_json(report_path)
        report["follow_up_requests"] = ["CR-001"]
        write_json(report_path, report)
        create_collaboration_request(
            self.project_root,
            "bundle",
            "CR-001",
            "app-a",
            "objectives.app-a.frontend-worker",
            "shared-platform.custodian",
            "shared-module-change",
            "Need shared dependency",
            blocking=True,
        )
        assemble_review_bundle(
            self.project_root,
            "bundle",
            "APP-A-BUNDLE-001",
            [report_path],
            "objectives.app-a.objective-manager",
            "objectives.app-a.acceptance-manager",
        )
        rejected = review_bundle(self.project_root, "bundle", "APP-A-BUNDLE-001")
        self.assertEqual(rejected["status"], "rejected")
        resolve_collaboration_request(self.project_root, "bundle", "CR-001")
        accepted = review_bundle(self.project_root, "bundle", "APP-A-BUNDLE-001")
        self.assertEqual(accepted["status"], "accepted")

    def test_phase_report_requires_human_approval_before_advancing(self) -> None:
        scaffold_smoke_test(self.project_root, "advance")
        verify_smoke_reports(self.project_root, "advance")
        report, _ = generate_phase_report(self.project_root, "advance")
        self.assertEqual(report["recommendation"], "advance")
        with self.assertRaisesRegex(ValueError, "requires human approval"):
            advance_phase(self.project_root, "advance")
        record_human_approval(self.project_root, "advance", "discovery", True)
        phase_plan = advance_phase(self.project_root, "advance")
        self.assertEqual(phase_plan["current_phase"], "design")

    def test_change_request_reentry_phase_and_delta_run(self) -> None:
        initialize_run(self.project_root, "changes", "# Goal")
        create_change_request(
            self.project_root,
            "changes",
            "chg-001",
            "Need interface updates",
            {
                "goal_changed": False,
                "scope_changed": False,
                "boundary_changed": False,
                "interface_changed": True,
                "architecture_changed": False,
                "team_changed": False,
                "implementation_changed": False,
            },
        )
        proposal = analyze_change_request(self.project_root, "changes", "chg-001")
        self.assertEqual(proposal["recommended_reentry_phase"], "design")
        approve_change(self.project_root, "changes", "chg-001", True)
        delta_root = scaffold_delta_run(self.project_root, "changes", "chg-001")
        delta_phase_plan = read_json(delta_root / "phase-plan.json")
        self.assertEqual(delta_phase_plan["current_phase"], "design")

    def test_execute_task_writes_completion_report_from_codex_output(self) -> None:
        scaffold_smoke_test(self.project_root, "exec")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "runs/exec/prompt-logs/APP-A-SMOKE-001.prompt.md", "status": "referenced"}],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "discovery",
                "prompt_layers": [
                    "orchestrator/roles/base/company.md",
                    "orchestrator/roles/base/worker.md",
                    "orchestrator/roles/capabilities/frontend.md",
                    "orchestrator/roles/objectives/app-a/approved/frontend-worker.md",
                    "orchestrator/phase-overlays/discovery.md",
                ],
                "schema": "task-assignment.v1",
            },
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-123"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.subprocess.run", return_value=completed):
            summary = execute_task(self.project_root, "exec", "APP-A-SMOKE-001")
        self.assertEqual(summary["status"], "ready_for_bundle_review")
        report = read_json(self.project_root / "runs" / "exec" / "reports" / "APP-A-SMOKE-001.json")
        self.assertEqual(report["summary"], "Finished the smoke task.")
        self.assertEqual(report["context_echo"]["objective_id"], "app-a")

    def test_execute_task_creates_collaboration_request_when_model_blocks(self) -> None:
        scaffold_smoke_test(self.project_root, "collab-exec")
        final_payload = {
            "summary": "Blocked on a shared dependency.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": ["requires shared-platform custodian review"],
            "open_issues": ["shared utility needs an approved change"],
            "follow_up_requests": [],
            "context_echo": None,
            "collaboration_request": {
                "to_role": "shared-platform.custodian",
                "type": "shared-module-change",
                "summary": "Need approval for shared module update",
                "blocking": True,
            },
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-999"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.subprocess.run", return_value=completed):
            summary = execute_task(self.project_root, "collab-exec", "APP-A-SMOKE-001")
        self.assertEqual(summary["status"], "blocked")
        report = read_json(self.project_root / "runs" / "collab-exec" / "reports" / "APP-A-SMOKE-001.json")
        self.assertEqual(report["follow_up_requests"], ["APP-A-SMOKE-001-CR-001"])
        request = read_json(
            self.project_root / "runs" / "collab-exec" / "collaboration" / "APP-A-SMOKE-001-CR-001.json"
        )
        self.assertEqual(request["to_role"], "shared-platform.custodian")

    def test_execute_task_raises_when_codex_turn_fails(self) -> None:
        scaffold_smoke_test(self.project_root, "failed-exec")
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-fail"}',
                '{"type":"turn.started"}',
                '{"type":"turn.failed","error":{"message":"Quota exceeded"}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=1)
        with patch("company_orchestrator.executor.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ExecutorError, "Quota exceeded"):
                execute_task(self.project_root, "failed-exec", "APP-A-SMOKE-001")

    def test_execute_task_timeout_preserves_partial_logs(self) -> None:
        import subprocess

        scaffold_smoke_test(self.project_root, "timeout-exec")
        timeout_error = subprocess.TimeoutExpired(
            cmd=["codex", "exec"],
            timeout=5,
            output=b'{"type":"thread.started","thread_id":"thread-timeout"}\n',
            stderr=b"still running\n",
        )
        with patch("company_orchestrator.executor.subprocess.run", side_effect=timeout_error):
            with self.assertRaisesRegex(ExecutorError, "timed out after 5 seconds"):
                execute_task(self.project_root, "timeout-exec", "APP-A-SMOKE-001", timeout_seconds=5)
        stdout_log = (self.project_root / "runs" / "timeout-exec" / "executions" / "APP-A-SMOKE-001.stdout.jsonl").read_text()
        stderr_log = (self.project_root / "runs" / "timeout-exec" / "executions" / "APP-A-SMOKE-001.stderr.log").read_text()
        self.assertIn("thread.started", stdout_log)
        self.assertIn("still running", stderr_log)

    def test_run_phase_executes_all_tasks_and_generates_phase_report(self) -> None:
        scaffold_smoke_test(self.project_root, "managed")

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = run_phase(self.project_root, "managed")

        self.assertEqual(summary["recommendation"], "advance")
        self.assertEqual(summary["objectives"]["app-a"]["status"], "accepted")
        self.assertEqual(summary["objectives"]["app-b"]["status"], "accepted")
        phase_report = read_json(self.project_root / "runs" / "managed" / "phase-reports" / "discovery.json")
        self.assertEqual(phase_report["recommendation"], "advance")
        manager_summary = read_json(self.project_root / "runs" / "managed" / "manager-runs" / "phase-discovery.json")
        self.assertEqual(manager_summary["phase"], "discovery")

    def test_run_phase_holds_when_one_objective_is_blocked(self) -> None:
        scaffold_smoke_test(self.project_root, "held")

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            status = "blocked" if task_id == "APP-B-SMOKE-001" else "ready_for_bundle_review"
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status=status,
                summary=f"{task_id} status {status}",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = run_phase(self.project_root, "held")

        self.assertEqual(summary["recommendation"], "hold")
        self.assertEqual(summary["objectives"]["app-a"]["status"], "accepted")
        self.assertEqual(summary["objectives"]["app-b"]["status"], "rejected")
        phase_report = read_json(self.project_root / "runs" / "held" / "phase-reports" / "discovery.json")
        self.assertEqual(phase_report["recommendation"], "hold")

    def test_run_phase_respects_task_dependencies(self) -> None:
        scaffold_smoke_test(self.project_root, "deps")
        app_b_task_path = self.project_root / "runs" / "deps" / "tasks" / "APP-B-SMOKE-001.json"
        app_b_task = read_json(app_b_task_path)
        app_b_task["depends_on"] = ["APP-A-SMOKE-001"]
        write_json(app_b_task_path, app_b_task)
        executed_order: list[str] = []

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            executed_order.append(task_id)
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = run_phase(self.project_root, "deps")

        self.assertEqual(executed_order, ["APP-A-SMOKE-001", "APP-B-SMOKE-001"])
        self.assertEqual(summary["recommendation"], "advance")

    def test_plan_objective_materializes_tasks_from_manager_plan(self) -> None:
        scaffold_planning_run(self.project_root, "planned", ["frontend"])
        final_payload = {
            "schema": "objective-plan.v1",
            "run_id": "planned",
            "phase": "discovery",
            "objective_id": "app-a",
            "summary": "Discovery plan for app-a",
            "tasks": [
                {
                    "task_id": "APP-A-DISC-001",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Identify the discovery boundary for app-a.",
                    "inputs": ["runs/planned/goal.md"],
                    "expected_outputs": ["boundary notes"],
                    "done_when": ["boundary is described"],
                    "depends_on": [],
                    "validation": [{"id": "manager-check", "command": "review-boundary-notes"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only"
                },
                {
                    "task_id": "APP-A-DISC-002",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Map dependencies for app-a.",
                    "inputs": ["runs/planned/goal.md"],
                    "expected_outputs": ["dependency notes"],
                    "done_when": ["dependencies are listed"],
                    "depends_on": ["APP-A-DISC-001"],
                    "validation": [{"id": "manager-check", "command": "review-dependency-notes"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only"
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "app-a-discovery-bundle-1",
                    "task_ids": ["APP-A-DISC-001", "APP-A-DISC-002"],
                    "summary": "Complete app-a discovery package"
                }
            ],
            "dependency_notes": ["Task 2 depends on task 1"],
            "collaboration_edges": []
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"plan-thread-123"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.objective_planner.subprocess.run", return_value=completed):
            summary = plan_objective(self.project_root, "planned", "app-a")
        self.assertEqual(summary["task_ids"], ["APP-A-DISC-001", "APP-A-DISC-002"])
        planned_task = read_json(self.project_root / "runs" / "planned" / "tasks" / "APP-A-DISC-001.json")
        self.assertEqual(planned_task["manager_role"], "objectives.app-a.frontend-manager")
        self.assertEqual(planned_task["acceptance_role"], "objectives.app-a.acceptance-manager")
        manager_plan = read_json(self.project_root / "runs" / "planned" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["bundle_plan"][0]["bundle_id"], "app-a-discovery-bundle-1")

    def test_plan_objective_normalizes_model_generated_run_id(self) -> None:
        scaffold_planning_run(self.project_root, "planned-run-id", ["frontend"])
        final_payload = planned_payload_for_objective("wrong-run-id", "app-a")
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"plan-thread-run-id"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.objective_planner.subprocess.run", return_value=completed):
            summary = plan_objective(self.project_root, "planned-run-id", "app-a")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["from"], "wrong-run-id")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["to"], "planned-run-id")
        manager_plan = read_json(self.project_root / "runs" / "planned-run-id" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["run_id"], "planned-run-id")

    def test_planning_prompt_forbids_repo_exploration(self) -> None:
        prompt = build_planning_prompt("base prompt")
        self.assertIn("Do not inspect the repository", prompt)
        self.assertIn("Return the JSON plan as your first and only response", prompt)

    def test_plan_objective_timeout_preserves_partial_logs(self) -> None:
        import subprocess

        scaffold_planning_run(self.project_root, "planned-timeout", ["frontend"])
        timeout_error = subprocess.TimeoutExpired(
            cmd=["codex", "exec"],
            timeout=7,
            output=b'{"type":"thread.started","thread_id":"plan-timeout"}\n',
            stderr=b"manager still reasoning\n",
        )
        with patch("company_orchestrator.objective_planner.subprocess.run", side_effect=timeout_error):
            with self.assertRaisesRegex(ExecutorError, "timed out after 7 seconds while planning objective app-a"):
                plan_objective(self.project_root, "planned-timeout", "app-a", timeout_seconds=7)
        stdout_log = (
            self.project_root / "runs" / "planned-timeout" / "manager-plans" / "discovery-app-a.stdout.jsonl"
        ).read_text()
        stderr_log = (
            self.project_root / "runs" / "planned-timeout" / "manager-plans" / "discovery-app-a.stderr.log"
        ).read_text()
        self.assertIn("thread.started", stdout_log)
        self.assertIn("manager still reasoning", stderr_log)

    def test_run_phase_uses_manager_generated_bundle_plan(self) -> None:
        scaffold_planning_run(self.project_root, "planned-phase", ["frontend"])
        final_payload = {
            "schema": "objective-plan.v1",
            "run_id": "planned-phase",
            "phase": "discovery",
            "objective_id": "app-a",
            "summary": "Discovery plan for app-a",
            "tasks": [
                {
                    "task_id": "APP-A-DISC-001",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Task one.",
                    "inputs": [],
                    "expected_outputs": ["note 1"],
                    "done_when": ["task one complete"],
                    "depends_on": [],
                    "validation": [{"id": "manager-check", "command": "check-1"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only"
                },
                {
                    "task_id": "APP-A-DISC-002",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Task two.",
                    "inputs": [],
                    "expected_outputs": ["note 2"],
                    "done_when": ["task two complete"],
                    "depends_on": [],
                    "validation": [{"id": "manager-check", "command": "check-2"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only"
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "app-a-discovery-bundle-1",
                    "task_ids": ["APP-A-DISC-001"],
                    "summary": "First discovery bundle"
                },
                {
                    "bundle_id": "app-a-discovery-bundle-2",
                    "task_ids": ["APP-A-DISC-002"],
                    "summary": "Second discovery bundle"
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": []
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"plan-thread-456"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.objective_planner.subprocess.run", return_value=completed):
            plan_objective(self.project_root, "planned-phase", "app-a")

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = run_phase(self.project_root, "planned-phase")

        self.assertEqual(summary["objectives"]["app-a"]["bundle_ids"], ["app-a-discovery-bundle-1", "app-a-discovery-bundle-2"])
        phase_report = read_json(self.project_root / "runs" / "planned-phase" / "phase-reports" / "discovery.json")
        self.assertEqual(phase_report["accepted_bundles"], ["app-a-discovery-bundle-1", "app-a-discovery-bundle-2"])

    def test_plan_phase_runs_all_objective_managers(self) -> None:
        scaffold_dual_planning_run(self.project_root, "plan-phase")

        def side_effect(*args: object, **kwargs: object):
            prompt = str(kwargs.get("input", ""))
            objective_id = "app-a" if '"objective_id": "app-a"' in prompt else "app-b"
            payload = planned_payload_for_objective("plan-phase", objective_id)
            stdout = "\n".join(
                [
                    '{"type":"thread.started","thread_id":"plan-thread"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                ]
            )
            return completed_process(stdout=stdout, stderr="", returncode=0)

        with patch("company_orchestrator.objective_planner.subprocess.run", side_effect=side_effect):
            summary = plan_phase(self.project_root, "plan-phase")

        self.assertEqual(len(summary["planned_objectives"]), 2)
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-A-DISC-001.json").exists())
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-B-DISC-001.json").exists())


if __name__ == "__main__":
    unittest.main()


def completed_process(*, stdout: str, stderr: str, returncode: int):
    import subprocess

    return subprocess.CompletedProcess(args=["codex", "exec"], returncode=returncode, stdout=stdout, stderr=stderr)


def json_line_event(event_type: str, item: dict[str, object]) -> str:
    return json.dumps({"type": event_type, "item": item})


def write_managed_report(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    status: str,
    summary: str,
) -> dict[str, object]:
    task = read_json(project_root / "runs" / run_id / "tasks" / f"{task_id}.json")
    report = {
        "schema": "completion-report.v1",
        "run_id": run_id,
        "phase": task["phase"],
        "objective_id": task["objective_id"],
        "task_id": task_id,
        "agent_role": task["assigned_role"],
        "status": status,
        "summary": summary,
        "artifacts": [],
        "validation_results": [],
        "dependency_impact": [],
        "open_issues": [],
        "follow_up_requests": [],
    }
    write_json(project_root / "runs" / run_id / "reports" / f"{task_id}.json", report)
    execution_summary = {
        "task_id": task_id,
        "thread_id": f"thread-{task_id}",
        "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        "stdout_path": f"runs/{run_id}/executions/{task_id}.stdout.jsonl",
        "stderr_path": f"runs/{run_id}/executions/{task_id}.stderr.log",
        "last_message_path": f"runs/{run_id}/executions/{task_id}.last-message.json",
        "report_path": f"runs/{run_id}/reports/{task_id}.json",
        "collaboration_request_ids": [],
        "status": status,
    }
    write_json(project_root / "runs" / run_id / "executions" / f"{task_id}.json", execution_summary)
    return execution_summary


def scaffold_planning_run(project_root: Path, run_id: str, capabilities: list[str]) -> None:
    run_dir = initialize_run(project_root, run_id, "# Goal\n\n## Objectives\n- App A")
    objective_map = {
        "schema": "objective-map.v1",
        "run_id": run_id,
        "objectives": [
            {
                "objective_id": "app-a",
                "title": "App A",
                "summary": "App A",
                "status": "approved",
                "capabilities": capabilities,
            }
        ],
        "dependencies": [],
    }
    write_json(run_dir / "objective-map.json", objective_map)
    suggest_team_proposals(project_root, run_id)
    generate_role_files(project_root, run_id, approve=True)


def scaffold_dual_planning_run(project_root: Path, run_id: str) -> None:
    run_dir = initialize_run(project_root, run_id, "# Goal\n\n## Objectives\n- App A\n- App B")
    objective_map = {
        "schema": "objective-map.v1",
        "run_id": run_id,
        "objectives": [
            {
                "objective_id": "app-a",
                "title": "App A",
                "summary": "App A",
                "status": "approved",
                "capabilities": ["frontend"],
            },
            {
                "objective_id": "app-b",
                "title": "App B",
                "summary": "App B",
                "status": "approved",
                "capabilities": ["backend"],
            }
        ],
        "dependencies": [],
    }
    write_json(run_dir / "objective-map.json", objective_map)
    suggest_team_proposals(project_root, run_id)
    generate_role_files(project_root, run_id, approve=True)


def planned_payload_for_objective(run_id: str, objective_id: str) -> dict[str, object]:
    capability = "frontend" if objective_id == "app-a" else "backend"
    return {
        "schema": "objective-plan.v1",
        "run_id": run_id,
        "phase": "discovery",
        "objective_id": objective_id,
        "summary": f"Discovery plan for {objective_id}",
        "tasks": [
            {
                "task_id": f"{objective_id.upper()}-DISC-001",
                "capability": capability,
                "assigned_role": f"objectives.{objective_id}.{capability}-worker",
                "objective": f"Plan task for {objective_id}",
                "inputs": [],
                "expected_outputs": ["note"],
                "done_when": ["task complete"],
                "depends_on": [],
                "validation": [{"id": "manager-check", "command": "check"}],
                "collaboration_rules": [],
                "working_directory": None,
                "additional_directories": [],
                "sandbox_mode": "read-only"
            }
        ],
        "bundle_plan": [
            {
                "bundle_id": f"{objective_id}-discovery-bundle",
                "task_ids": [f"{objective_id.upper()}-DISC-001"],
                "summary": f"Bundle for {objective_id}"
            }
        ],
        "dependency_notes": [],
        "collaboration_edges": []
    }
