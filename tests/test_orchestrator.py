from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from company_orchestrator.bundles import assemble_review_bundle, review_bundle
from company_orchestrator.cli import main as cli_main
from company_orchestrator.changes import analyze_change_request, approve_change, create_change_request, scaffold_delta_run
from company_orchestrator.collaboration import create_collaboration_request, resolve_collaboration_request
from company_orchestrator.executor import ExecutorError, execute_task
from company_orchestrator.filesystem import read_json, write_json
from company_orchestrator.management import run_phase
from company_orchestrator.monitoring import build_activity_detail, build_run_dashboard, inspect_activity
from company_orchestrator.objective_planner import build_planning_prompt, plan_objective, plan_phase
from company_orchestrator.planner import generate_role_files, initialize_run, suggest_team_proposals
from company_orchestrator.prompts import build_planning_payload, render_prompt
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

    def test_render_prompt_resolves_planning_inputs_and_prior_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "resolved-inputs", ["frontend"])
        prior_report = {
            "schema": "completion-report.v1",
            "run_id": "resolved-inputs",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-DISC-000",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Previous discovery output.",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
        }
        write_json(self.project_root / "runs" / "resolved-inputs" / "reports" / "APP-A-DISC-000.json", prior_report)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "resolved-inputs",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-DISC-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Use resolved planning context.",
            "inputs": [
                "Planning Inputs.goal_markdown",
                "Planning Inputs.objective",
                "Planning Inputs.team",
                "Runtime Context.phase",
                "Output of APP-A-DISC-000",
            ],
            "expected_outputs": [],
            "done_when": ["resolved inputs are present"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "resolved-inputs" / "tasks" / "APP-A-DISC-001.json"
        write_json(task_path, task)
        metadata = render_prompt(self.project_root, "resolved-inputs", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()
        self.assertIn("# Resolved Inputs", prompt_text)
        self.assertIn("Planning Inputs.goal_markdown", prompt_text)
        self.assertIn("Previous discovery output.", prompt_text)
        self.assertIn('"phase": "discovery"', prompt_text)
        prompt_log = read_json(self.project_root / "runs" / "resolved-inputs" / "prompt-logs" / "APP-A-DISC-001.json")
        self.assertIn("Planning Inputs.goal_markdown", prompt_log["resolved_input_refs"])

    def test_render_prompt_resolves_natural_language_goal_refs(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "natural-refs", goal_text)
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "natural-refs",
            "objectives": [
                {
                    "objective_id": "frontend-obj",
                    "title": "React frontend objective",
                    "summary": "React frontend objective",
                    "status": "proposed",
                    "capabilities": ["frontend"],
                }
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "natural-refs")
        generate_role_files(self.project_root, "natural-refs", approve=True)
        prior_report = {
            "schema": "completion-report.v1",
            "run_id": "natural-refs",
            "phase": "discovery",
            "objective_id": "frontend-obj",
            "task_id": "PREV-001",
            "agent_role": "objectives.frontend-obj.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Previous frontend discovery output.",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
        }
        write_json(self.project_root / "runs" / "natural-refs" / "reports" / "PREV-001.json", prior_report)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "natural-refs",
            "phase": "discovery",
            "objective_id": "frontend-obj",
            "capability": "frontend",
            "task_id": "FRONT-001",
            "assigned_role": "objectives.frontend-obj.frontend-worker",
            "manager_role": "objectives.frontend-obj.objective-manager",
            "acceptance_role": "objectives.frontend-obj.acceptance-manager",
            "objective": "Use natural language goal refs.",
            "inputs": [
                "Goal markdown sections: Objectives, Success Criteria, In Scope, Out Of Scope",
                "Objective Details: React Web Frontend",
                "Discovery Expectations and Known Unknowns",
                "Objective summary and title from planning inputs",
                "goal_markdown: Constraints",
                "output of PREV-001",
                "team.roles and available_roles",
            ],
            "expected_outputs": [],
            "done_when": ["natural refs resolve"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "natural-refs" / "tasks" / "FRONT-001.json"
        write_json(task_path, task)
        metadata = render_prompt(self.project_root, "natural-refs", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()
        self.assertIn("React web frontend", prompt_text)
        self.assertIn("frontend should use React", prompt_text)
        self.assertIn("Previous frontend discovery output.", prompt_text)
        self.assertIn("available_roles", prompt_text)
        self.assertNotIn('"unresolved_input_ref": "Goal markdown sections: Objectives, Success Criteria, In Scope, Out Of Scope"', prompt_text)

    def test_render_prompt_resolves_design_phase_goal_refs(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "design-refs", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["current_phase"] = "design"
        phase_plan["phases"][1]["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "design-refs",
            "objectives": [
                {
                    "objective_id": "frontend-obj",
                    "title": "React frontend objective",
                    "summary": "React frontend objective",
                    "status": "approved",
                    "capabilities": ["frontend"],
                }
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "design-refs")
        generate_role_files(self.project_root, "design-refs", approve=True)
        prior_report = {
            "schema": "completion-report.v1",
            "run_id": "design-refs",
            "phase": "design",
            "objective_id": "frontend-obj",
            "task_id": "DESIGN-000",
            "agent_role": "objectives.frontend-obj.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Previous design output.",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
        }
        write_json(self.project_root / "runs" / "design-refs" / "reports" / "DESIGN-000.json", prior_report)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "design-refs",
            "phase": "design",
            "objective_id": "frontend-obj",
            "capability": "frontend",
            "task_id": "DESIGN-001",
            "assigned_role": "objectives.frontend-obj.frontend-worker",
            "manager_role": "objectives.frontend-obj.objective-manager",
            "acceptance_role": "objectives.frontend-obj.acceptance-manager",
            "objective": "Resolve design-phase natural refs.",
            "inputs": [
                "Planning Inputs goal_markdown summary, objectives, success criteria, in-scope, and out-of-scope sections",
                "Objective Details for React Web Frontend",
                "Design Expectations for required design artifacts",
                "Planning Inputs Design Expectations for API/interface contract",
                "Planning Inputs success criteria, constraints, and human approval notes",
                "Technical constraint that the frontend should use React and remain straightforward",
                "Outputs from DESIGN-000",
            ],
            "expected_outputs": [],
            "done_when": ["design refs resolve"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "design-refs" / "tasks" / "DESIGN-001.json"
        write_json(task_path, task)
        metadata = render_prompt(self.project_root, "design-refs", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()
        self.assertIn("extremely simple todo list application", prompt_text)
        self.assertIn("frontend should use React", prompt_text)
        self.assertIn("API/interface contract", prompt_text)
        self.assertIn("human approval notes", prompt_text)
        self.assertIn("Previous design output.", prompt_text)
        self.assertNotIn('"unresolved_input_ref": "Planning Inputs goal_markdown summary, objectives, success criteria, in-scope, and out-of-scope sections"', prompt_text)

    def test_render_prompt_resolves_prior_phase_artifacts_for_mvp_inputs(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "mvp-refs", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "mvp-refs",
            "objectives": [
                {
                    "objective_id": "backend-obj",
                    "title": "Backend API and persistence",
                    "summary": "Backend API and persistence",
                    "status": "approved",
                    "capabilities": ["backend"],
                }
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "mvp-refs")
        generate_role_files(self.project_root, "mvp-refs", approve=True)
        design_dir = self.project_root / "docs" / "design" / "backend-obj"
        design_dir.mkdir(parents=True)
        architecture_path = design_dir / "backend-architecture.md"
        architecture_path.write_text(
            "# Backend Architecture\n\nUse a single Node.js/Express service with SQLite persistence.",
            encoding="utf-8",
        )
        gates_path = design_dir / "design-review-gates.md"
        gates_path.write_text("# Review Gates\n\nRun backend CRUD validation before acceptance.", encoding="utf-8")
        design_report = {
            "schema": "completion-report.v1",
            "run_id": "mvp-refs",
            "phase": "design",
            "objective_id": "backend-obj",
            "task_id": "DESIGN-BE-001",
            "agent_role": "objectives.backend-obj.backend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Approved backend design package for MVP persistence and API behavior.",
            "artifacts": [
                {"path": "docs/design/backend-obj/backend-architecture.md", "status": "created"},
                {"path": "docs/design/backend-obj/design-review-gates.md", "status": "created"},
            ],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
        }
        write_json(run_dir / "reports" / "DESIGN-BE-001.json", design_report)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "mvp-refs",
            "phase": "mvp-build",
            "objective_id": "backend-obj",
            "capability": "backend",
            "task_id": "MVP-BE-001",
            "assigned_role": "objectives.backend-obj.backend-worker",
            "manager_role": "objectives.backend-obj.backend-manager",
            "acceptance_role": "objectives.backend-obj.acceptance-manager",
            "objective": "Use approved design artifacts.",
            "inputs": [
                "Approved backend design package for todo API and persistence",
                "Review gates for MVP build",
            ],
            "expected_outputs": [],
            "done_when": ["prior phase artifacts resolve"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = run_dir / "tasks" / "MVP-BE-001.json"
        write_json(task_path, task)
        metadata = render_prompt(self.project_root, "mvp-refs", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()
        self.assertIn("docs/design/backend-obj/backend-architecture.md", prompt_text)
        self.assertIn("Node.js/Express service with SQLite persistence", prompt_text)
        self.assertIn("docs/design/backend-obj/design-review-gates.md", prompt_text)
        self.assertNotIn('"unresolved_input_ref": "Approved backend design package for todo API and persistence"', prompt_text)
        self.assertNotIn('"unresolved_input_ref": "Review gates for MVP build"', prompt_text)

    def test_build_planning_payload_compacts_prior_report_evidence(self) -> None:
        scaffold_planning_run(self.project_root, "compact-planning", ["frontend"])
        long_evidence = " ".join(["evidence"] * 200)
        report = {
            "schema": "completion-report.v1",
            "run_id": "compact-planning",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-DISC-001",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "A" * 400,
            "artifacts": [
                {"path": "docs/design/app-a/spec.md", "status": "created"},
            ],
            "validation_results": [
                {"id": "long-evidence", "status": "passed", "evidence": long_evidence},
            ],
            "dependency_impact": [long_evidence],
            "open_issues": [long_evidence],
            "follow_up_requests": [],
        }
        docs_dir = self.project_root / "docs" / "design" / "app-a"
        docs_dir.mkdir(parents=True)
        (docs_dir / "spec.md").write_text("# Spec\n", encoding="utf-8")
        write_json(self.project_root / "runs" / "compact-planning" / "reports" / "APP-A-DISC-001.json", report)

        phase_plan = read_json(self.project_root / "runs" / "compact-planning" / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "active"
        phase_plan["current_phase"] = "design"
        write_json(self.project_root / "runs" / "compact-planning" / "phase-plan.json", phase_plan)

        payload = build_planning_payload(self.project_root, "compact-planning", "app-a")
        prior_report = payload["prior_phase_reports"][0]

        self.assertIn("artifacts", prior_report)
        self.assertNotIn("validation_results", prior_report)
        self.assertLessEqual(len(prior_report["summary"]), 240)
        self.assertLessEqual(len(prior_report["open_issues_preview"][0]), 160)
        self.assertLessEqual(len(prior_report["dependency_impact_preview"][0]), 160)

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

    def test_bundle_review_ignores_prose_follow_up_requests(self) -> None:
        scaffold_smoke_test(self.project_root, "bundle-prose")
        simulate_context_echo_completion(self.project_root, "bundle-prose", "APP-A-SMOKE-001")
        report_path = self.project_root / "runs" / "bundle-prose" / "reports" / "APP-A-SMOKE-001.json"
        report = read_json(report_path)
        report["follow_up_requests"] = [
            "Manager should confirm the MVP boundary before design.",
            "Acceptance should review the handoff bundle for simplicity.",
        ]
        write_json(report_path, report)
        assemble_review_bundle(
            self.project_root,
            "bundle-prose",
            "APP-A-BUNDLE-001",
            [report_path],
            "objectives.app-a.objective-manager",
            "objectives.app-a.acceptance-manager",
        )
        bundle = review_bundle(self.project_root, "bundle-prose", "APP-A-BUNDLE-001")
        self.assertEqual(bundle["status"], "accepted")

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
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            summary = execute_task(self.project_root, "exec", "APP-A-SMOKE-001")
        self.assertEqual(summary["status"], "ready_for_bundle_review")
        report = read_json(self.project_root / "runs" / "exec" / "reports" / "APP-A-SMOKE-001.json")
        self.assertEqual(report["summary"], "Finished the smoke task.")
        self.assertEqual(report["context_echo"]["objective_id"], "app-a")

    def test_execute_task_streams_live_activity_updates_and_events(self) -> None:
        scaffold_smoke_test(self.project_root, "exec-live")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
            "context_echo": None,
            "collaboration_request": None,
        }
        lines = [
            '{"type":"thread.started","thread_id":"thread-live"}',
            '{"type":"turn.started"}',
            json_line_event(
                "item.started",
                {"id": "cmd-1", "type": "command_execution", "command": "echo test"},
            ),
            json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
        ]

        def side_effect(*_: object, **kwargs: object):
            callback = kwargs["on_stdout_line"]
            for line in lines:
                callback(line)
            return completed_process(stdout="\n".join(lines), stderr="", returncode=0)

        with patch("company_orchestrator.executor.run_codex_command", side_effect=side_effect):
            execute_task(self.project_root, "exec-live", "APP-A-SMOKE-001")

        activity = read_json(
            self.project_root / "runs" / "exec-live" / "live" / "activities" / "APP-A-SMOKE-001.json"
        )
        self.assertEqual(activity["status"], "ready_for_bundle_review")
        self.assertEqual(activity["progress_fraction"], 1.0)
        self.assertEqual(activity["current_activity"], "Finished the smoke task.")
        events = (
            self.project_root / "runs" / "exec-live" / "live" / "events.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("codex.thread.started", events)
        self.assertIn("codex.item.started.command_execution", events)
        self.assertIn("task.completed", events)

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
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
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
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
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
        with patch("company_orchestrator.executor.run_codex_command", side_effect=timeout_error):
            with self.assertRaisesRegex(ExecutorError, "timed out after 5 seconds"):
                execute_task(self.project_root, "timeout-exec", "APP-A-SMOKE-001", timeout_seconds=5)
        stdout_log = (self.project_root / "runs" / "timeout-exec" / "executions" / "APP-A-SMOKE-001.stdout.jsonl").read_text()
        stderr_log = (self.project_root / "runs" / "timeout-exec" / "executions" / "APP-A-SMOKE-001.stderr.log").read_text()
        self.assertIn("thread.started", stdout_log)
        self.assertIn("still running", stderr_log)
        activity = read_json(
            self.project_root / "runs" / "timeout-exec" / "live" / "activities" / "APP-A-SMOKE-001.json"
        )
        self.assertEqual(activity["status"], "failed")

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
        run_state = read_json(self.project_root / "runs" / "managed" / "live" / "run-state.json")
        self.assertEqual(run_state["current_phase"], "discovery")

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
        blocked_activity = read_json(self.project_root / "runs" / "held" / "live" / "activities" / "APP-B-SMOKE-001.json")
        self.assertEqual(blocked_activity["status"], "blocked")

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
        with patch("company_orchestrator.objective_planner.run_codex_command", return_value=completed):
            summary = plan_objective(self.project_root, "planned", "app-a")
        self.assertEqual(summary["task_ids"], ["APP-A-DISC-001", "APP-A-DISC-002"])
        planned_task = read_json(self.project_root / "runs" / "planned" / "tasks" / "APP-A-DISC-001.json")
        self.assertEqual(planned_task["manager_role"], "objectives.app-a.frontend-manager")
        self.assertEqual(planned_task["acceptance_role"], "objectives.app-a.acceptance-manager")
        manager_plan = read_json(self.project_root / "runs" / "planned" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["bundle_plan"][0]["bundle_id"], "app-a-discovery-bundle-1")

    def test_plan_objective_streams_live_activity_updates_and_events(self) -> None:
        scaffold_planning_run(self.project_root, "planned-live", ["frontend"])
        final_payload = planned_payload_for_objective("planned-live", "app-a")
        lines = [
            '{"type":"thread.started","thread_id":"plan-thread-live"}',
            '{"type":"turn.started"}',
            json_line_event(
                "item.started",
                {"id": "cmd-1", "type": "command_execution", "command": "plan objective"},
            ),
            json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
        ]

        def side_effect(*_: object, **kwargs: object):
            callback = kwargs["on_stdout_line"]
            for line in lines:
                callback(line)
            return completed_process(stdout="\n".join(lines), stderr="", returncode=0)

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect):
            plan_objective(self.project_root, "planned-live", "app-a")

        activity = read_json(
            self.project_root / "runs" / "planned-live" / "live" / "activities" / "plan__discovery__app-a.json"
        )
        self.assertEqual(activity["status"], "completed")
        self.assertEqual(activity["kind"], "objective_plan")
        events = (
            self.project_root / "runs" / "planned-live" / "live" / "events.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("planning.completed", events)
        self.assertIn("codex.item.started.command_execution", events)

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
        with patch("company_orchestrator.objective_planner.run_codex_command", return_value=completed):
            summary = plan_objective(self.project_root, "planned-run-id", "app-a")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["from"], "wrong-run-id")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["to"], "planned-run-id")
        manager_plan = read_json(self.project_root / "runs" / "planned-run-id" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["run_id"], "planned-run-id")

    def test_plan_objective_prefixes_bundle_ids_with_objective_id(self) -> None:
        scaffold_planning_run(self.project_root, "planned-bundles", ["frontend"])
        final_payload = planned_payload_for_objective("planned-bundles", "app-a")
        final_payload["bundle_plan"] = [
            {
                "bundle_id": "bundle-discovery-core",
                "task_ids": ["APP-A-DISC-001"],
                "summary": "Unscoped bundle id from model",
            }
        ]
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"plan-thread-bundle-id"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.objective_planner.run_codex_command", return_value=completed):
            summary = plan_objective(self.project_root, "planned-bundles", "app-a")
        self.assertEqual(summary["bundle_ids"], ["app-a-bundle-discovery-core"])
        manager_plan = read_json(self.project_root / "runs" / "planned-bundles" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["bundle_plan"][0]["bundle_id"], "app-a-bundle-discovery-core")

    def test_plan_objective_rejects_unresolved_generated_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "planned-unresolved", ["frontend"])
        final_payload = planned_payload_for_objective("planned-unresolved", "app-a")
        final_payload["tasks"][0]["inputs"] = ["Completely imaginary planning input"]
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"plan-thread-unresolved"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.objective_planner.run_codex_command", return_value=completed):
            with self.assertRaisesRegex(ExecutorError, "unresolved input refs for task APP-A-DISC-001"):
                plan_objective(self.project_root, "planned-unresolved", "app-a")

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
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=timeout_error):
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
        with patch("company_orchestrator.objective_planner.run_codex_command", return_value=completed):
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

    def test_phase_report_ignores_stale_bundle_ids_not_in_manager_plan(self) -> None:
        scaffold_planning_run(self.project_root, "stale-bundles", ["frontend"])
        plan = planned_payload_for_objective("stale-bundles", "app-a")
        plan["bundle_plan"] = [
            {
                "bundle_id": "app-a-required-bundle",
                "task_ids": ["APP-A-DISC-001"],
                "summary": "Required bundle",
            }
        ]
        write_json(self.project_root / "runs" / "stale-bundles" / "manager-plans" / "discovery-app-a.json", plan)
        required_bundle = {
            "schema": "review-bundle.v1",
            "run_id": "stale-bundles",
            "phase": "discovery",
            "objective_id": "app-a",
            "bundle_id": "app-a-required-bundle",
            "assembled_by": "objectives.app-a.objective-manager",
            "reviewed_by": "objectives.app-a.acceptance-manager",
            "included_tasks": ["APP-A-DISC-001"],
            "status": "accepted",
            "required_checks": [],
            "rejection_reasons": [],
        }
        stale_bundle = dict(required_bundle)
        stale_bundle["bundle_id"] = "stale-old-bundle"
        write_json(self.project_root / "runs" / "stale-bundles" / "bundles" / "app-a-required-bundle.json", required_bundle)
        write_json(self.project_root / "runs" / "stale-bundles" / "bundles" / "stale-old-bundle.json", stale_bundle)
        report, _ = generate_phase_report(self.project_root, "stale-bundles")
        self.assertEqual(report["objective_outcomes"][0]["accepted_bundles"], ["app-a-required-bundle"])
        self.assertEqual(report["accepted_bundles"], ["app-a-required-bundle"])

    def test_plan_phase_runs_all_objective_managers(self) -> None:
        scaffold_dual_planning_run(self.project_root, "plan-phase")

        def side_effect(*args: object, **kwargs: object):
            prompt = str(kwargs.get("prompt", ""))
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

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect):
            summary = plan_phase(self.project_root, "plan-phase")

        self.assertEqual(len(summary["planned_objectives"]), 2)
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-A-DISC-001.json").exists())
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-B-DISC-001.json").exists())

    def test_monitoring_renderers_show_sections_and_prompt_details(self) -> None:
        scaffold_smoke_test(self.project_root, "monitor")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [],
            "dependency_impact": [],
            "open_issues": [],
            "follow_up_requests": [],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-monitor"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            execute_task(self.project_root, "monitor", "APP-A-SMOKE-001")

        console = Console(record=True, width=140)
        console.print(build_run_dashboard(self.project_root, "monitor"))
        run_output = console.export_text()
        self.assertIn("Run Status", run_output)
        self.assertIn("Active Task Activities", run_output)
        self.assertIn("Objective Progress", run_output)

        console = Console(record=True, width=140)
        console.print(build_activity_detail(self.project_root, "monitor", "APP-A-SMOKE-001", events=10))
        detail_output = console.export_text()
        self.assertIn("Prompt", detail_output)
        self.assertIn("Task Assignment", detail_output)
        self.assertIn("Latest Events", detail_output)

    def test_inspect_activity_reports_missing_activity_cleanly(self) -> None:
        scaffold_smoke_test(self.project_root, "missing-activity")
        with self.assertRaises(SystemExit) as exc:
            inspect_activity(self.project_root, "missing-activity", "does-not-exist")
        self.assertIn("Activity does-not-exist was not found", str(exc.exception))

    def test_cli_execute_task_watch_uses_watch_wrapper(self) -> None:
        scaffold_smoke_test(self.project_root, "watch-cli")
        expected = {"task_id": "APP-A-SMOKE-001", "status": "ready_for_bundle_review"}
        argv = [
            "company-orchestrator",
            "--project-root",
            str(self.project_root),
            "execute-task",
            "watch-cli",
            "APP-A-SMOKE-001",
            "--watch",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("company_orchestrator.cli.run_maybe_watched", return_value=expected) as wrapped,
            patch("company_orchestrator.cli.print_result") as print_result_mock,
        ):
            cli_main()
        wrapped.assert_called_once()
        self.assertTrue(wrapped.call_args.args[2])
        print_result_mock.assert_called_once_with(expected, leading_blank_line=True)

    def test_build_planning_payload_uses_immediately_previous_phase_for_detailed_reports(self) -> None:
        scaffold_planning_run(self.project_root, "polish-payload", ["frontend"])
        run_dir = self.project_root / "runs" / "polish-payload"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "complete"
        phase_plan["phases"][2]["human_approved"] = True
        phase_plan["phases"][3]["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)

        reports = [
            ("discovery", "DISC-001"),
            ("design", "DESIGN-001"),
            ("mvp-build", "MVP-001"),
        ]
        for phase, task_id in reports:
            report = {
                "schema": "completion-report.v1",
                "run_id": "polish-payload",
                "phase": phase,
                "objective_id": "app-a",
                "task_id": task_id,
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": f"{phase} handoff",
                "artifacts": [{"path": f"docs/{phase}.md", "status": "created"}],
                "validation_results": [{"id": f"{phase}-check", "status": "passed", "evidence": "ok"}],
                "dependency_impact": [f"{phase} dependency"],
                "open_issues": [f"{phase} issue"],
                "follow_up_requests": [],
            }
            write_json(run_dir / "reports" / f"{task_id}.json", report)
            artifact_path = self.project_root / "docs" / f"{phase}.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(f"# {phase}\n", encoding="utf-8")

        payload = build_planning_payload(self.project_root, "polish-payload", "app-a")

        self.assertEqual([item["phase"] for item in payload["prior_phase_reports"]], ["mvp-build"])
        self.assertEqual(
            payload["approved_inputs_catalog"]["report_paths"],
            [
                "runs/polish-payload/reports/DISC-001.json",
                "runs/polish-payload/reports/DESIGN-001.json",
                "runs/polish-payload/reports/MVP-001.json",
            ],
        )


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
