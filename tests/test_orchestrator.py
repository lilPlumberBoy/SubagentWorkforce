from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from company_orchestrator.autonomy import default_autonomy_state, run_autonomous
from company_orchestrator.bundles import assemble_review_bundle, review_bundle
from company_orchestrator.change_replan import apply_approved_changes_and_resume
from company_orchestrator.cli import augment_result_with_guidance, format_result_summary, main as cli_main
from company_orchestrator.changes import (
    active_approved_change_requests,
    classify_change_request_approval,
    normalize_change_request_payloads,
    persist_change_requests,
)
from company_orchestrator.collaboration import create_collaboration_request, resolve_collaboration_request
from company_orchestrator.executor import (
    CodexProcessStall,
    ExecutorError,
    TaskExecutionRuntime,
    build_codex_command,
    build_execution_prompt,
    execute_task,
    materialize_task_context_files,
    materialize_executor_response,
    prepare_task_runtime,
)
from company_orchestrator.filesystem import read_json, write_json
from company_orchestrator.handoffs import blocking_handoffs_for_task, evaluate_handoff, list_handoffs
from company_orchestrator.impact import apply_approved_change_impacts, analyze_change_request_impact, stale_task_notifications
from company_orchestrator.management import finalize_objective_bundle, run_guidance, run_phase, schedule_tasks
from company_orchestrator.monitoring import (
    build_activity_detail,
    build_run_dashboard,
    inspect_activity,
)
from company_orchestrator.observability import (
    planning_compaction_profile,
    record_llm_call,
    recommend_runtime_tuning,
    refresh_run_observability,
)
from company_orchestrator.objective_roots import capability_shared_asset_hints
from company_orchestrator.objective_planner import (
    PlanningLimiter,
    aggregate_capability_plans,
    align_required_outbound_handoff_output_ids,
    attach_handoff_dependencies,
    build_capability_planning_prompt,
    build_planning_prompt,
    canonicalize_input_reference,
    normalize_task_execution_metadata,
    normalize_capability_plan,
    normalize_objective_outline,
    owned_path_targets_prefix,
    plan_capability,
    plan_objective,
    plan_phase,
    planning_stall_timeout_seconds,
    quarantined_objective_phase_artifacts,
    validate_capability_plan_contents,
)
from company_orchestrator.output_descriptors import normalize_output_descriptors
from company_orchestrator.parallelism import infer_execution_metadata
from company_orchestrator.parallelism import effective_sandbox_mode
from company_orchestrator.parallelism import normalize_task_artifact_descriptors
from company_orchestrator.planner import (
    bootstrap_run,
    decompose_goal,
    generate_role_files,
    initialize_run,
    suggest_team_proposals,
)
from company_orchestrator.prompts import (
    build_capability_planning_payload,
    build_dependency_preview_section,
    build_capability_prompt_payload,
    build_planning_payload,
    build_planning_prompt_payload,
    compact_goal_context,
    compact_resolved_inputs_for_prompt,
    preview_resolved_inputs,
    render_capability_planning_prompt,
    render_objective_planning_prompt,
    render_prompt,
)
from company_orchestrator.recovery import RecoveryBlockedError, inspect_planning_artifacts, reconcile_for_command, reconcile_run
from company_orchestrator.reports import advance_phase, generate_phase_report, record_human_approval
from company_orchestrator.schemas import SchemaValidationError, validate_document
from company_orchestrator.smoke import scaffold_smoke_test, simulate_context_echo_completion, verify_smoke_reports
from company_orchestrator.live import ensure_activity, note_activity_stream, read_activity, read_activity_history, record_event, update_activity
from company_orchestrator.worktree_manager import commit_task_workspace, ensure_run_integration_workspace, ensure_task_workspace
from company_orchestrator.bundles import land_accepted_bundle


REPO_ROOT = Path(__file__).resolve().parent.parent


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        shutil.copytree(REPO_ROOT / "orchestrator", self.project_root / "orchestrator")
        (self.project_root / "runs").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_planning_stall_timeout_allows_longer_mvp_build_silence(self) -> None:
        self.assertEqual(planning_stall_timeout_seconds(600), 150)
        self.assertEqual(planning_stall_timeout_seconds(450), 112)
        self.assertEqual(planning_stall_timeout_seconds(240), 60)
        self.assertEqual(planning_stall_timeout_seconds(900), 180)

    def test_prompt_inheritance_for_smoke_task(self) -> None:
        scaffold_smoke_test(self.project_root, "smoke")
        metadata = read_json(self.project_root / "runs" / "smoke" / "prompt-logs" / "APP-A-SMOKE-001.json")
        self.assertEqual(metadata["phase"], "discovery")
        self.assertIn("orchestrator/roles/base/company.md", metadata["files_loaded"])
        self.assertIn("orchestrator/roles/base/worker.md", metadata["files_loaded"])
        self.assertIn("orchestrator/roles/capabilities/frontend.md", metadata["files_loaded"])
        self.assertIn("orchestrator/phase-overlays/discovery/task-execution.md", metadata["files_loaded"])

    def test_planning_renderers_load_planning_overlays_only(self) -> None:
        scaffold_planning_run(self.project_root, "planning-overlays", ["frontend"])
        objective_metadata = render_objective_planning_prompt(self.project_root, "planning-overlays", "app-a")
        objective_prompt = (self.project_root / objective_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("orchestrator/phase-overlays/discovery/objective-planning.md", objective_metadata["files_loaded"])
        self.assertNotIn("orchestrator/phase-overlays/discovery/task-execution.md", objective_metadata["files_loaded"])
        self.assertNotIn("Allowed work: implement", objective_prompt)
        self.assertIn("This prompt is for planning only.", objective_prompt)

        objective_outline = objective_outline_for_objective("planning-overlays", "app-a", ["frontend"])
        capability_metadata = render_capability_planning_prompt(
            self.project_root,
            "planning-overlays",
            "app-a",
            "frontend",
            objective_outline,
        )
        capability_prompt = (self.project_root / capability_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn(
            "orchestrator/phase-overlays/discovery/capability-planning.md",
            capability_metadata["files_loaded"],
        )
        self.assertNotIn("orchestrator/phase-overlays/discovery/task-execution.md", capability_metadata["files_loaded"])
        self.assertNotIn("Allowed work: implement", capability_prompt)
        self.assertIn("This prompt is for planning only.", capability_prompt)

    def test_normalize_output_descriptors_rejects_stringified_null_sentinels(self) -> None:
        with self.assertRaises(ValueError):
            normalize_output_descriptors(
                [{"kind": "artifact", "output_id": "artifact-output", "path": "None"}],
                allow_legacy_strings=False,
            )
        with self.assertRaises(ValueError):
            normalize_output_descriptors(
                [{"kind": "asset", "output_id": "asset-output", "asset_id": "null", "path": "apps/todo/backend/src/server.js"}],
                allow_legacy_strings=False,
            )
        with self.assertRaises(ValueError):
            normalize_output_descriptors(
                [
                    {
                        "kind": "assertion",
                        "output_id": "assertion-output",
                        "path": "None",
                        "description": "CRUD behavior is covered",
                        "evidence": {"validation_ids": [], "artifact_paths": []},
                    }
                ],
                allow_legacy_strings=False,
            )

    def test_output_descriptor_schema_enforces_kind_specific_path_shapes(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_document(
                {
                    "kind": "artifact",
                    "output_id": "artifact-output",
                    "path": None,
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                "output-descriptor.v1",
                self.project_root,
            )

        validate_document(
            {
                "kind": "assertion",
                "output_id": "assertion-output",
                "path": None,
                "asset_id": None,
                "description": "CRUD behavior is covered",
                "evidence": {"validation_ids": ["frontend-crud"], "artifact_paths": []},
            },
            "output-descriptor.v1",
            self.project_root,
        )

    def test_canonicalize_input_reference_normalizes_bracketed_goal_context_refs(self) -> None:
        self.assertEqual(
            canonicalize_input_reference('Planning Inputs.goal_context.sections["MVP Build Expectations"]'),
            "Planning Inputs.goal_context.sections.MVP Build Expectations",
        )

    def test_decompose_goal_rebalances_same_app_integration_objective_to_middleware_only(self) -> None:
        goal_text = """# Goal

## Objectives
- React web frontend for creating viewing completing editing and deleting todo items
- Simple backend API and persistence layer for storing todo items
- Basic application integration and delivery workflow connecting frontend and backend
"""
        initialize_run(self.project_root, "integration-rebalance", goal_text)

        objective_map = decompose_goal(self.project_root, "integration-rebalance")
        integration_objective = next(
            objective
            for objective in objective_map["objectives"]
            if objective["objective_id"] == "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        )

        self.assertEqual(integration_objective["capabilities"], ["middleware"])
        self.assertEqual(
            objective_map["dependencies"],
            [
                {
                    "from_objective_id": "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items",
                    "to_objective_id": "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend",
                    "kind": "integration_input",
                },
                {
                    "from_objective_id": "simple-backend-api-and-persistence-layer-for-storing-todo-items",
                    "to_objective_id": "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend",
                    "kind": "integration_input",
                },
            ],
        )

        registry = suggest_team_proposals(self.project_root, "integration-rebalance")
        integration_team = next(
            team
            for team in registry["teams"]
            if team["objective_id"] == "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        )
        self.assertEqual(integration_team["capabilities"], ["middleware"])
        self.assertEqual(
            [role["role_id"] for role in integration_team["roles"]],
            [
                "objective-manager",
                "acceptance-manager",
                "middleware-manager",
                "middleware-worker",
            ],
        )

        generate_role_files(self.project_root, "integration-rebalance", approve=True)
        approved_dir = (
            self.project_root
            / "orchestrator"
            / "roles"
            / "objectives"
            / "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
            / "approved"
        )
        self.assertEqual(
            sorted(path.name for path in approved_dir.glob("*.md")),
            [
                "acceptance-manager.md",
                "middleware-manager.md",
                "middleware-worker.md",
                "objective-manager.md",
            ],
        )

    def test_run_autonomous_refuses_attach_without_overwriting_existing_state(self) -> None:
        scaffold_smoke_test(self.project_root, "autonomy-attach")
        autonomy_path = self.project_root / "runs" / "autonomy-attach" / "autonomy.json"
        state = default_autonomy_state("autonomy-attach")
        state.update(
            {
                "enabled": True,
                "status": "active",
                "active_phase": "discovery",
                "started_at": "2026-03-16T00:00:00Z",
                "last_action": "run-phase",
                "last_action_status": "completed",
            }
        )
        write_json(autonomy_path, state)
        guidance = {
            "run_id": "autonomy-attach",
            "phase": "discovery",
            "run_status": "working",
            "run_status_reason": "1 active activities, 0 queued, 0 blocked in discovery.",
            "phase_recommendation": None,
            "next_action_command": None,
            "next_action_reason": "Monitor the run. No manual action is required while work is active.",
        }

        with patch("company_orchestrator.autonomy.run_guidance", return_value=guidance):
            result = run_autonomous(self.project_root, "autonomy-attach")

        after = read_json(autonomy_path)
        self.assertEqual(result["stop_condition"], "active_external_work")
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["started_at"], "2026-03-16T00:00:00Z")

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
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
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

    def test_render_prompt_includes_discovery_overlay_context_guard(self) -> None:
        scaffold_smoke_test(self.project_root, "overlay-guard")
        task_path = self.project_root / "runs" / "overlay-guard" / "tasks" / "APP-A-SMOKE-001.json"

        metadata = render_prompt(self.project_root, "overlay-guard", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()

        self.assertIn("Use only the injected Resolved Inputs", prompt_text)
        self.assertIn("Do not mine `docs/design`", prompt_text)

    def test_render_objective_prompt_includes_exact_objective_contract_section(self) -> None:
        scaffold_planning_run(self.project_root, "objective-contract-prompt", ["frontend", "backend"])
        metadata = render_objective_planning_prompt(self.project_root, "objective-contract-prompt", "app-a")
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text(encoding="utf-8")

        self.assertIn("# Exact Objective Contract", prompt_text)
        self.assertIn("## Allowed Capabilities", prompt_text)
        self.assertIn("`frontend`", prompt_text)
        self.assertIn("`backend`", prompt_text)
        self.assertIn("## Allowed Output Surfaces By Capability", prompt_text)
        self.assertIn("runs/objective-contract-prompt/reports", prompt_text)

    def test_middleware_mvp_build_objective_contract_excludes_runtime_tree_paths(self) -> None:
        scaffold_planning_run(self.project_root, "objective-contract-middleware-mvp", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        (self.project_root / "apps" / "todo" / "scripts").mkdir(parents=True, exist_ok=True)
        (self.project_root / "package.json").write_text('{"name":"workspace-root"}\n', encoding="utf-8")
        runtime_root = self.project_root / "apps" / "todo" / "runtime" / "src"
        runtime_root.mkdir(parents=True, exist_ok=True)
        (runtime_root / "runtime.js").write_text("// runtime\n", encoding="utf-8")
        run_dir = self.project_root / "runs" / "objective-contract-middleware-mvp"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)

        payload = build_planning_payload(
            self.project_root,
            "objective-contract-middleware-mvp",
            "app-a",
        )
        roots = payload["objective_contract_hints"]["capability_output_roots"]["middleware"]
        self.assertIn("package.json", roots)
        self.assertIn("apps/todo/scripts/**", roots)
        self.assertNotIn("apps/todo/runtime/**", roots)
        self.assertNotIn("docs/objectives/app-a/**", roots)

        metadata = render_objective_planning_prompt(self.project_root, "objective-contract-middleware-mvp", "app-a")
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("package.json", prompt_text)
        self.assertNotIn("apps/todo/runtime/**", prompt_text)
        self.assertNotIn("docs/objectives/app-a/**", prompt_text)

    def test_render_prompt_includes_exact_task_contract_section(self) -> None:
        scaffold_planning_run(self.project_root, "task-contract-prompt", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "task-contract-prompt",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-DISC-EXACT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Author the frontend discovery brief.",
            "inputs": [
                "Planning Inputs.goal_markdown",
                "Runtime Context.phase",
            ],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend-discovery-brief",
                    "path": "apps/app-a/frontend/discovery-brief.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": ["frontend brief is written"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "writes_existing_paths": [],
            "owned_paths": ["apps/app-a/frontend/discovery-brief.md"],
            "shared_asset_ids": [],
        }
        task_path = self.project_root / "runs" / "task-contract-prompt" / "tasks" / "APP-A-DISC-EXACT-001.json"
        write_json(task_path, task)

        metadata = render_prompt(self.project_root, "task-contract-prompt", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text(encoding="utf-8")

        self.assertIn("# Exact Task Contract", prompt_text)
        self.assertIn("## Required Outputs", prompt_text)
        self.assertIn("`frontend-discovery-brief` (artifact) -> path `apps/app-a/frontend/discovery-brief.md`", prompt_text)
        self.assertIn("## Declared Inputs", prompt_text)
        self.assertIn("`Planning Inputs.goal_markdown`", prompt_text)

    def test_build_execution_prompt_forbids_rediscovering_resolved_task_outputs(self) -> None:
        prompt = build_execution_prompt("# Task prompt")
        self.assertIn("`# Exact Task Contract` section as the hard boundary", prompt)
        self.assertIn("do not shell-search `runs/`, sibling task workspaces", prompt)
        self.assertIn("Resolved Inputs as authoritative", prompt)
        self.assertIn("create its parent directory and write the artifact directly", prompt)
        self.assertIn("Do not waste turns on exploratory shell commands like `pwd`, `ls`", prompt)
        self.assertIn("Do not re-read the generated prompt log or task prompt file from `runs/...`", prompt)
        self.assertIn("do not run `test -f`, `rg`, or `grep` against files you just created", prompt)

    def test_build_execution_prompt_requires_blocking_issues_to_use_blocked_status(self) -> None:
        prompt = build_execution_prompt("# Task prompt")
        self.assertIn('status must be "blocked"', prompt)
        self.assertIn("design artifacts, runtime contracts, or handoff payloads contradict each other", prompt)
        self.assertIn("goal-critical, blocking, impossible to resolve within your owned scope", prompt)
        self.assertIn("include a change_requests array", prompt)
        self.assertIn("conflicting_input_refs", prompt)
        self.assertIn("leave affected_output_ids, affected_handoff_ids, impacted_objective_ids, and impacted_task_ids as empty arrays", prompt)

    def test_normalize_change_request_payloads_auto_approves_small_interface_change(self) -> None:
        normalized = normalize_change_request_payloads(
            [
                {
                    "change_category": "interface_contract",
                    "summary": "Align backend validation rule with the approved API contract.",
                    "blocking_reason": "The current request schema contradicts the canonical API contract.",
                    "why_local_resolution_is_invalid": "Changing only this task would create a contract mismatch for sibling consumers.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": ["backend-api-contract"],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": ["simple-backend-api-and-persistence-layer-for-storing-todo-items"],
                    "impacted_task_ids": ["backend-api-surface-and-review-bundle"],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": False,
                    },
                }
            ]
        )
        self.assertEqual(normalized[0]["approval"], {"mode": "auto", "status": "approved"})

    def test_normalize_change_request_payloads_requires_human_review_for_scope_change(self) -> None:
        normalized = normalize_change_request_payloads(
            [
                {
                    "change_category": "shared_behavior",
                    "summary": "Expand todo persistence to support tags.",
                    "blocking_reason": "The MVP contract no longer fits the required behavior.",
                    "why_local_resolution_is_invalid": "Completing the task as assigned would miss the changed product scope.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": ["todo-api-contract", "todo-ui-contract"],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": ["simple-backend-api-and-persistence-layer-for-storing-todo-items"],
                    "impacted_task_ids": [],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": True,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": True,
                    },
                }
            ]
        )
        self.assertEqual(normalized[0]["approval"], {"mode": "human", "status": "pending_human_review"})

    def test_normalize_change_request_payloads_rejects_non_blocking_local_issue(self) -> None:
        with self.assertRaisesRegex(ValueError, "blocking=true"):
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "shared_behavior",
                        "summary": "Rename an internal helper for clarity.",
                        "blocking_reason": "This is a local cleanup preference.",
                        "why_local_resolution_is_invalid": "Not applicable.",
                        "blocking": False,
                        "goal_critical": False,
                        "affected_output_ids": [],
                        "affected_handoff_ids": [],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "mvp-build",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": False,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            )

    def test_normalize_change_request_payloads_rejects_duplicate_root_blocker(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates another request"):
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Backend contract drift blocks completion.",
                        "blocking_reason": "Shared API response schema conflicts with the approved contract.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared API contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": [],
                        "impacted_objective_ids": ["simple-backend-api-and-persistence-layer-for-storing-todo-items"],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    },
                    {
                        "change_category": "interface_contract",
                        "summary": "API contract still conflicts.",
                        "blocking_reason": "Shared API response schema conflicts with the approved contract.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared API contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": [],
                        "impacted_objective_ids": ["react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items"],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    },
                ]
            )

    def test_build_capability_planning_prompt_enforces_minimal_mvp_build_tasks(self) -> None:
        prompt = build_capability_planning_prompt("# Planning prompt")
        self.assertIn("Produce very small isolated worker tasks", prompt)
        self.assertIn("Keep bundle_plan lean", prompt)
        self.assertIn("Do not create standalone evidence, report, conformance, review, or handoff tasks", prompt)

    def test_planning_compaction_profile_defaults_to_compact_on_cold_start(self) -> None:
        scaffold_smoke_test(self.project_root, "cold-start")
        profile = planning_compaction_profile(self.project_root, "cold-start", "discovery")
        self.assertEqual(profile["level"], "compact")
        self.assertEqual(profile["limits"]["existing_tasks"], 4)

    def test_build_codex_command_disables_configured_mcp_servers(self) -> None:
        codex_home = self.project_root / ".codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            """
[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"

[mcp_servers.Notion]
url = "https://mcp.notion.com/mcp"
enabled = false

[mcp_servers.linear]
url = "https://example.com/mcp"
""".strip()
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            command = build_codex_command(
                codex_path="codex",
                working_directory=self.project_root,
                output_schema_path=self.project_root / "schema.json",
                last_message_path=self.project_root / "out.json",
                sandbox_mode="workspace-write",
                additional_directories=[],
            )

        self.assertIn("-c", command)
        overrides = [command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"]
        self.assertIn("mcp_servers.github.enabled=false", overrides)
        self.assertIn("mcp_servers.linear.enabled=false", overrides)
        self.assertNotIn("mcp_servers.Notion.enabled=false", overrides)

    def test_begin_attempt_resets_attempt_scoped_activity_state(self) -> None:
        scaffold_smoke_test(self.project_root, "retry-state")
        activity = ensure_activity(
            self.project_root,
            "retry-state",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="queued",
            progress_stage="queued",
            current_activity="Waiting for execution slot.",
            prompt_path="runs/retry-state/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/retry-state/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/retry-state/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/retry-state/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            observability={"prompt_char_count": 100, "prompt_line_count": 10, "prompt_bytes": 100},
        )
        update_activity(
            self.project_root,
            "retry-state",
            "APP-A-SMOKE-001",
            status="launching",
            progress_stage="launching",
        )
        note_activity_stream(
            self.project_root,
            "retry-state",
            "APP-A-SMOKE-001",
            stdout_bytes=128,
            stderr_bytes=64,
        )
        update_activity(
            self.project_root,
            "retry-state",
            "APP-A-SMOKE-001",
            status="interrupted",
            progress_stage="interrupted",
            status_reason="Process missing; partial artifacts found.",
            artifact_reconciliation={"status": "partial", "details": ["workspace exists"]},
            recovery_action="refreshed_workspace",
        )

        retried = ensure_activity(
            self.project_root,
            "retry-state",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="prompt_rendered",
            progress_stage="prompt_rendered",
            current_activity="Rendered task prompt.",
            prompt_path="runs/retry-state/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/retry-state/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/retry-state/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/retry-state/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            observability={"prompt_char_count": 120, "prompt_line_count": 12, "prompt_bytes": 120},
            begin_attempt=True,
        )

        self.assertEqual(retried["attempt"], 2)
        self.assertIsNone(retried["status_reason"])
        self.assertIsNone(retried["interrupted_at"])
        self.assertIsNone(retried["recovered_at"])
        self.assertIsNone(retried["artifact_reconciliation"])
        self.assertIsNone(retried["process_metadata"])
        self.assertEqual(retried["observability"]["prompt_char_count"], 120)
        self.assertEqual(retried["observability"]["queue_wait_ms"], 0)
        self.assertEqual(retried["observability"]["runtime_ms"], 0)
        self.assertEqual(retried["observability"]["stream_stdout_bytes"], 0)
        self.assertEqual(retried["observability"]["stream_stderr_bytes"], 0)
        self.assertIsNone(retried["observability"]["queued_at"])
        self.assertIsNone(retried["observability"]["launched_at"])
        self.assertIsNone(retried["observability"]["completed_at"])
        self.assertEqual(retried["started_at"], retried["updated_at"])

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
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
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

    def test_compact_goal_context_prefers_matching_objective_detail(self) -> None:
        goal_text = "\n".join(
            [
                "# Goal",
                "",
                "## Objective Details",
                "### React Web Frontend",
                "Frontend detail.",
                "",
                "### Backend API And Persistence",
                "Backend detail.",
                "",
                "### Application Integration And Delivery Workflow",
                "Integration detail.",
            ]
        )

        compacted = compact_goal_context(
            goal_text,
            objective_id="simple-backend-api-and-persistence-layer-for-storing-todo-items",
            objective_title="Simple backend API and persistence layer for storing todo items",
            objective_summary="Simple backend API and persistence layer for storing todo items",
            objective_detail_limit=1,
        )

        self.assertEqual(list(compacted["objective_details"].keys()), ["Backend API And Persistence"])

    def test_preview_resolved_inputs_falls_back_to_full_goal_context_when_compaction_omits_detail(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "goal-context-fallback", goal_text)
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "goal-context-fallback",
            "objectives": [
                {
                    "objective_id": "simple-backend-api-and-persistence-layer-for-storing-todo-items",
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                }
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "goal-context-fallback")
        generate_role_files(self.project_root, "goal-context-fallback", approve=True)
        input_ref = "Planning Inputs.goal_context.objective_details.Application Integration And Delivery Workflow"
        task = {
            "schema": "task-assignment.v1",
            "run_id": "goal-context-fallback",
            "phase": "discovery",
            "objective_id": "simple-backend-api-and-persistence-layer-for-storing-todo-items",
            "capability": "backend",
            "task_id": "BACKEND-REF-001",
            "assigned_role": "objectives.simple-backend-api-and-persistence-layer-for-storing-todo-items.backend-worker",
            "manager_role": "objectives.simple-backend-api-and-persistence-layer-for-storing-todo-items.backend-manager",
            "acceptance_role": "objectives.simple-backend-api-and-persistence-layer-for-storing-todo-items.acceptance-manager",
            "objective": "Resolve a cross-objective detail from the full goal context.",
            "inputs": [input_ref],
            "expected_outputs": [],
            "done_when": ["resolved inputs are present"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }

        resolved = preview_resolved_inputs(self.project_root, "goal-context-fallback", task)

        self.assertIsInstance(resolved[input_ref], str)
        self.assertIn("connect", resolved[input_ref].lower())

    def test_render_prompt_uses_runtime_workspace_and_compacts_prior_report(self) -> None:
        scaffold_planning_run(self.project_root, "prompt-compaction", ["frontend"])
        prior_report = {
            "schema": "completion-report.v1",
            "run_id": "prompt-compaction",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-DISC-000",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Previous discovery output.",
            "artifacts": [{"path": "apps/app-a/docs/discovery-note.md", "status": "created"}],
            "validation_results": [{"id": "exists", "status": "passed", "evidence": "artifact exists"}],
            "legacy_dependency_notes": ["Frontend contract depends on this output."],
            "open_issues": ["Keep auth out of scope."],
            "legacy_follow_ups": ["Carry this note into design."],
            "context_echo": {"role_id": "objectives.app-a.frontend-worker"},
            "runtime_observability": {"input_tokens": 12345},
        }
        write_json(self.project_root / "runs" / "prompt-compaction" / "reports" / "APP-A-DISC-000.json", prior_report)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "prompt-compaction",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-DISC-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Use compact prior outputs and the task workspace.",
            "inputs": ["Output of APP-A-DISC-000"],
            "expected_outputs": ["apps/app-a/docs/discovery-follow-up.md"],
            "owned_paths": ["apps/app-a/docs/discovery-follow-up.md"],
            "additional_directories": ["apps/app-a"],
            "working_directory": str(self.project_root),
            "done_when": ["resolved inputs are present"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "prompt-compaction" / "tasks" / "APP-A-DISC-001.json"
        write_json(task_path, task)
        workspace = self.project_root / "workspaces" / "app-a-task"
        workspace.mkdir(parents=True)

        metadata = render_prompt(
            self.project_root,
            "prompt-compaction",
            task_path,
            working_directory=workspace,
        )
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()

        self.assertIn(f'"working_directory": "{workspace}"', prompt_text)
        self.assertIn('"additional_directories": [', prompt_text)
        self.assertIn("Previous discovery output.", prompt_text)
        self.assertNotIn(f'"working_directory": "{self.project_root}"', prompt_text)
        self.assertNotIn("runs/prompt-compaction/reports/APP-A-DISC-000.json", prompt_text)
        self.assertNotIn("legacy_follow_ups", prompt_text)
        self.assertNotIn("runtime_observability", prompt_text)
        self.assertNotIn("context_echo", prompt_text)

    def test_render_prompt_includes_upstream_artifact_preview_for_task_output_refs(self) -> None:
        scaffold_planning_run(self.project_root, "prompt-artifact-preview", ["frontend"])
        prior_report = {
            "schema": "completion-report.v1",
            "run_id": "prompt-artifact-preview",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-DISC-000",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Previous discovery output.",
            "artifacts": [{"path": "apps/app-a/docs/discovery-note.md", "status": "created"}],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
        }
        write_json(self.project_root / "runs" / "prompt-artifact-preview" / "reports" / "APP-A-DISC-000.json", prior_report)
        source_workspace = self.project_root / "workspaces" / "prior-task"
        artifact_path = source_workspace / "apps" / "app-a" / "docs" / "discovery-note.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("# Discovery Note\n\nArtifact preview content for downstream prompts.\n")
        write_json(
            self.project_root / "runs" / "prompt-artifact-preview" / "executions" / "APP-A-DISC-000.json",
            {
                "task_id": "APP-A-DISC-000",
                "status": "ready_for_bundle_review",
                "workspace_path": str(source_workspace),
            },
        )
        task = {
            "schema": "task-assignment.v1",
            "run_id": "prompt-artifact-preview",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-DISC-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Use upstream artifact previews from task outputs.",
            "inputs": ["Output of APP-A-DISC-000"],
            "expected_outputs": [],
            "done_when": ["upstream artifact previews are present"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "prompt-artifact-preview" / "tasks" / "APP-A-DISC-001.json"
        write_json(task_path, task)

        metadata = render_prompt(self.project_root, "prompt-artifact-preview", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()

        self.assertIn("Artifact preview content for downstream prompts.", prompt_text)
        self.assertIn("artifact_previews", prompt_text)
        self.assertIn("# Dependency Artifact Previews", prompt_text)
        self.assertIn("## Output of APP-A-DISC-000", prompt_text)
        self.assertIn("source: `APP-A-DISC-000`", prompt_text)
        self.assertEqual(prompt_text.count("Artifact preview content for downstream prompts."), 1)
        self.assertNotIn('"preview": "# Discovery Note', prompt_text)
        self.assertNotIn("runs/prompt-artifact-preview/reports/APP-A-DISC-000.json", prompt_text)

    def test_normalize_capability_plan_clears_planner_working_directory(self) -> None:
        scaffold_planning_run(self.project_root, "normalize-working-directory", ["backend"])
        outline = objective_outline_for_objective("normalize-working-directory", "app-a", ["backend"])
        plan = capability_plan_for_objective("normalize-working-directory", "app-a", "backend")
        plan["tasks"][0]["working_directory"] = str(self.project_root)

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id="normalize-working-directory",
            phase="discovery",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="workspace-write",
        )

        self.assertIsNone(normalized["tasks"][0]["working_directory"])

    def test_normalize_capability_plan_promotes_owned_path_tasks_to_write_mode_and_inherits_sandbox(self) -> None:
        scaffold_planning_run(self.project_root, "normalize-sandbox", ["backend"])
        outline = objective_outline_for_objective("normalize-sandbox", "app-a", ["backend"])
        plan = capability_plan_for_objective("normalize-sandbox", "app-a", "backend")
        plan["tasks"][0]["execution_mode"] = "read_only"
        plan["tasks"][0]["parallel_policy"] = "allow"
        plan["tasks"][0]["expected_outputs"] = ["backend_constraints_draft"]
        plan["tasks"][0]["owned_paths"] = ["apps/todo/backend/data"]
        plan["tasks"][0]["sandbox_mode"] = "read-only"

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id="normalize-sandbox",
            phase="discovery",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="workspace-write",
        )

        self.assertEqual(normalized["tasks"][0]["execution_mode"], "isolated_write")
        self.assertEqual(normalized["tasks"][0]["parallel_policy"], "allow")
        self.assertEqual(normalized["tasks"][0]["sandbox_mode"], "workspace-write")

    def test_normalize_objective_outline_promotes_bare_non_lane_role_refs(self) -> None:
        scaffold_planning_run(self.project_root, "normalize-outline-role", ["backend"])
        objective = {
            "objective_id": "app-a",
            "capabilities": ["backend"],
        }
        outline = objective_outline_for_objective(
            "normalize-outline-role",
            "app-a",
            ["backend"],
            collaboration_edges=[
                {
                    "edge_id": "backend-review-bundle",
                    "from_capability": "backend",
                    "to_capability": "acceptance",
                    "to_role": "acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance reviews the backend bundle.",
                    "deliverables": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.discovery.brief",
                            "path": "docs/backend-discovery-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        )

        normalized, _ = normalize_objective_outline(
            self.project_root,
            outline,
            run_id="normalize-outline-role",
            phase="discovery",
            objective=objective,
        )

        self.assertEqual(
            normalized["collaboration_edges"][0]["to_role"],
            "objectives.app-a.acceptance-manager",
        )

    def test_normalize_objective_outline_rejects_unexpected_lane_for_middleware_only_objective(self) -> None:
        scaffold_planning_run(self.project_root, "middleware-only-outline", ["middleware"])
        objective = {
            "objective_id": "app-a",
            "capabilities": ["middleware"],
        }
        outline = objective_outline_for_objective("middleware-only-outline", "app-a", ["middleware"])
        outline["capability_lanes"][0]["capability"] = "frontend"
        outline["capability_lanes"][0]["assigned_manager_role"] = "objectives.app-a.frontend-manager"

        with self.assertRaises(ExecutorError) as ctx:
            normalize_objective_outline(
                self.project_root,
                outline,
                run_id="middleware-only-outline",
                phase="discovery",
                objective=objective,
            )

        self.assertIn("unexpected capability lane frontend", str(ctx.exception))

    def test_normalize_objective_outline_rejects_directory_root_outputs_for_mvp_build(self) -> None:
        scaffold_planning_run(self.project_root, "outline-broad-root", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["frontend"],
        }
        outline = objective_outline_for_objective("outline-broad-root", "app-a", ["frontend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "frontend_mvp_implementation",
                "path": "apps/todo",
                "asset_id": "todo-react-frontend-mvp",
                "description": None,
                "evidence": None,
            }
        ]

        with self.assertRaises(ExecutorError) as ctx:
            normalize_objective_outline(
                self.project_root,
                outline,
                run_id="outline-broad-root",
                phase="mvp-build",
                objective=objective,
            )

        self.assertIn("must declare concrete file outputs", str(ctx.exception))

    def test_normalize_objective_outline_allows_run_local_artifacts_for_mvp_build(self) -> None:
        scaffold_planning_run(self.project_root, "outline-run-local", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["frontend"],
        }
        outline = objective_outline_for_objective("outline-run-local", "app-a", ["frontend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend_app_shell",
                "path": "apps/todo/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend_validation_summary",
                "path": "runs/outline-run-local/artifacts/frontend-mvp-build/frontend-validation-summary.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend_build_report",
                "path": "runs/outline-run-local/reports/frontend-mvp-build.json",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend_review_bundle",
                "path": "runs/outline-run-local/review-bundles/app-a/frontend-mvp-build-review.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]

        normalized, _ = normalize_objective_outline(
            self.project_root,
            outline,
            run_id="outline-run-local",
            phase="mvp-build",
            objective=objective,
        )

        self.assertEqual(
            [item["path"] for item in normalized["capability_lanes"][0]["expected_outputs"][:4]],
            [
                "apps/todo/frontend/src/App.tsx",
                "runs/outline-run-local/artifacts/frontend-mvp-build/frontend-validation-summary.md",
                "runs/outline-run-local/reports/frontend-mvp-build.json",
                "runs/outline-run-local/review-bundles/app-a/frontend-mvp-build-review.md",
            ],
        )

    def test_normalize_objective_outline_allows_middleware_owned_shared_root_manifest(self) -> None:
        scaffold_planning_run(self.project_root, "outline-shared-root-manifest", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        (self.project_root / "apps" / "todo" / "scripts").mkdir(parents=True, exist_ok=True)
        (self.project_root / "package.json").write_text('{"name":"workspace-root"}\n', encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["middleware"],
        }
        outline = objective_outline_for_objective("outline-shared-root-manifest", "app-a", ["middleware"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "todo_workspace_manifest",
                "path": "package.json",
                "asset_id": "todo-workspace-manifest",
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "middleware_build_handoff",
                "path": "apps/todo/orchestrator/roles/objectives/app-a/mvp-build/middleware-build-handoff.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]

        normalized, _ = normalize_objective_outline(
            self.project_root,
            outline,
            run_id="outline-shared-root-manifest",
            phase="mvp-build",
            objective=objective,
        )

        self.assertEqual(
            [item["path"] for item in normalized["capability_lanes"][0]["expected_outputs"]],
            [
                "package.json",
                "apps/todo/orchestrator/roles/objectives/app-a/mvp-build/middleware-build-handoff.md",
            ],
        )

    def test_normalize_capability_plan_allows_run_local_report_outputs_for_mvp_build(self) -> None:
        scaffold_planning_run(self.project_root, "capability-run-local", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")

        outline = objective_outline_for_objective("capability-run-local", "app-a", ["frontend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend_app_shell",
                "path": "apps/todo/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend_build_report",
                "path": "runs/capability-run-local/reports/frontend-mvp-build.json",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]

        plan = capability_plan_for_objective("capability-run-local", "app-a", "frontend")
        plan["phase"] = "mvp-build"
        task = plan["tasks"][0]
        task["execution_mode"] = "isolated_write"
        task["writes_existing_paths"] = ["apps/todo/frontend/src/index.js"]
        task["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend_app_shell",
                "path": "apps/todo/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend_build_report",
                "path": "runs/capability-run-local/reports/frontend-mvp-build.json",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]
        task["validation"] = []

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id="capability-run-local",
            phase="mvp-build",
            objective_id="app-a",
            capability="frontend",
            objective_outline=outline,
            default_sandbox_mode="read-only",
        )

        normalized_task = normalized["tasks"][0]
        self.assertCountEqual(
            normalized_task["owned_paths"],
            [
                "apps/todo/frontend/src/App.tsx",
                "apps/todo/frontend/src/index.js",
                "runs/capability-run-local/reports/frontend-mvp-build.json",
            ],
        )

    def test_normalize_objective_outline_strips_assertions_from_acceptance_review_edges(self) -> None:
        scaffold_planning_run(self.project_root, "outline-review-edge", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["frontend"],
        }
        outline = objective_outline_for_objective("outline-review-edge", "app-a", ["frontend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend_app_shell",
                "path": "apps/todo/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        outline["collaboration_edges"] = [
            {
                "edge_id": "frontend-review",
                "from_capability": "frontend",
                "to_capability": "acceptance",
                "to_role": "acceptance-manager",
                "handoff_type": "review_bundle",
                "reason": "Acceptance needs the build outputs.",
                "blocking": True,
                "shared_asset_ids": [],
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "frontend_review_bundle",
                        "path": "runs/outline-review-edge/review-bundles/app-a/frontend-review.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    },
                    {
                        "kind": "assertion",
                        "output_id": "frontend_ready",
                        "path": None,
                        "asset_id": None,
                        "description": "Frontend is ready.",
                        "evidence": {
                            "validation_ids": ["frontend-ready-check"],
                            "artifact_paths": [
                                "runs/outline-review-edge/review-bundles/app-a/frontend-review.md"
                            ],
                        },
                    },
                ],
            }
        ]

        normalized, _ = normalize_objective_outline(
            self.project_root,
            outline,
            run_id="outline-review-edge",
            phase="mvp-build",
            objective=objective,
        )

        self.assertEqual(
            normalized["collaboration_edges"][0]["deliverables"],
            [
                {
                    "kind": "artifact",
                    "output_id": "frontend_review_bundle",
                    "path": "runs/outline-review-edge/review-bundles/app-a/frontend-review.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
        )

    def test_normalize_objective_outline_strips_lane_assertions_for_acceptance_review_lanes(self) -> None:
        scaffold_planning_run(self.project_root, "outline-review-lane", ["backend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        backend_root = self.project_root / "apps" / "todo" / "backend" / "src"
        backend_root.mkdir(parents=True, exist_ok=True)
        (backend_root / "server.js").write_text("export const ready = true;\n", encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["backend"],
        }
        outline = objective_outline_for_objective("outline-review-lane", "app-a", ["backend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "backend_server_entry",
                "path": "apps/todo/backend/src/server.js",
                "asset_id": "backend-server-entry",
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "backend_review_bundle",
                "path": "runs/outline-review-lane/review-bundles/app-a/backend-review.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "assertion",
                "output_id": "backend_mvp_ready",
                "path": None,
                "asset_id": None,
                "description": "Backend is ready.",
                "evidence": {
                    "validation_ids": ["backend-ready-check"],
                    "artifact_paths": [
                        "runs/outline-review-lane/review-bundles/app-a/backend-review.md"
                    ],
                },
            },
        ]
        outline["collaboration_edges"] = [
            {
                "edge_id": "backend-review",
                "from_capability": "backend",
                "to_capability": "acceptance",
                "to_role": "acceptance-manager",
                "handoff_type": "review_bundle",
                "reason": "Acceptance needs the backend build outputs.",
                "blocking": True,
                "shared_asset_ids": ["backend-server-entry"],
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "backend_review_bundle",
                        "path": "runs/outline-review-lane/review-bundles/app-a/backend-review.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    },
                    {
                        "kind": "asset",
                        "output_id": "backend_server_entry",
                        "path": "apps/todo/backend/src/server.js",
                        "asset_id": "backend-server-entry",
                        "description": None,
                        "evidence": None,
                    },
                ],
            }
        ]

        normalized, _ = normalize_objective_outline(
            self.project_root,
            outline,
            run_id="outline-review-lane",
            phase="mvp-build",
            objective=objective,
        )

        self.assertEqual(
            normalized["capability_lanes"][0]["expected_outputs"],
            [
                {
                    "kind": "asset",
                    "output_id": "backend_server_entry",
                    "path": "apps/todo/backend/src/server.js",
                    "asset_id": "backend-server-entry",
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "artifact",
                    "output_id": "backend_review_bundle",
                    "path": "runs/outline-review-lane/review-bundles/app-a/backend-review.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
            ],
        )

    def test_normalize_objective_outline_rejects_backend_frontend_consumption_contract_input_in_mvp_build(self) -> None:
        scaffold_planning_run(self.project_root, "outline-backend-authority", ["backend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        backend_root = self.project_root / "apps" / "todo" / "backend" / "src"
        backend_root.mkdir(parents=True, exist_ok=True)
        (backend_root / "app.ts").write_text("export const app = true;\n", encoding="utf-8")
        objective = {
            "objective_id": "app-a",
            "capabilities": ["backend"],
        }
        outline = objective_outline_for_objective("outline-backend-authority", "app-a", ["backend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["inputs"] = [
            "apps/todo/backend/design/todo-api-contract.yaml",
            "apps/todo/orchestrator/roles/objectives/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/artifacts/frontend-api-consumption-contract.md",
        ]
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "backend_http_entrypoint",
                "path": "apps/todo/backend/src/app.ts",
                "asset_id": "backend-http-entrypoint",
                "description": None,
                "evidence": None,
            }
        ]

        with self.assertRaises(ExecutorError) as ctx:
            normalize_objective_outline(
                self.project_root,
                outline,
                run_id="outline-backend-authority",
                phase="mvp-build",
                objective=objective,
            )

        self.assertIn("must not consume frontend-api-consumption-contract.md directly", str(ctx.exception))

    def test_normalize_task_artifact_descriptors_drops_pathless_shared_asset_duplicates(self) -> None:
        task = {
            "shared_asset_ids": ["app-a:integration-contract"],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "middleware-brief",
                    "path": "apps/todo/orchestrator/roles/objectives/app-a/discovery/middleware-brief.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "asset",
                    "output_id": "middleware-integration-contract-handoff",
                    "path": None,
                    "asset_id": "app-a:integration-contract",
                    "description": None,
                    "evidence": None,
                },
            ],
            "owned_paths": [],
            "writes_existing_paths": [],
        }

        normalize_task_artifact_descriptors(task)

        self.assertEqual(
            task["expected_outputs"],
            [
                {
                    "kind": "artifact",
                    "output_id": "middleware-brief",
                    "path": "apps/todo/orchestrator/roles/objectives/app-a/discovery/middleware-brief.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
        )

    def test_normalize_capability_plan_rewrites_same_phase_future_paths(self) -> None:
        scaffold_planning_run(self.project_root, "canonical-inputs", ["frontend", "middleware"])
        outline = objective_outline_for_objective(
            "canonical-inputs",
            "app-a",
            ["frontend", "middleware"],
            collaboration_edges=[
                {
                    "edge_id": "frontend-to-middleware",
                    "from_capability": "frontend",
                    "to_capability": "middleware",
                    "to_role": "objectives.app-a.middleware-manager",
                    "handoff_type": "brief",
                    "reason": "Middleware depends on the frontend brief.",
                    "deliverables": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend.discovery.brief",
                            "path": "apps/todo/docs/discovery/frontend-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        )
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "canonical-inputs",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "middleware discovery",
            "tasks": [
                {
                    "task_id": "APP-A-MIDDLEWARE-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write middleware plan",
                    "inputs": ["Planning Inputs.goal_markdown"],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.plan",
                            "path": "apps/todo/docs/discovery/middleware-plan.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["middleware plan exists"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
                {
                    "task_id": "APP-A-MIDDLEWARE-002",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Review middleware plan against upstream brief",
                    "inputs": [
                        "apps/todo/docs/discovery/middleware-plan.md",
                        "apps/todo/docs/discovery/frontend-brief.md",
                    ],
                    "expected_outputs": [
                        {
                            "kind": "assertion",
                            "output_id": "middleware.discovery.reviewed",
                            "path": None,
                            "asset_id": None,
                            "description": "Middleware plan reviewed against upstream brief.",
                            "evidence": {"validation_ids": [], "artifact_paths": []},
                        }
                    ],
                    "done_when": ["middleware review is complete"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
            ],
            "bundle_plan": [{"bundle_id": "middleware-bundle", "task_ids": ["APP-A-MIDDLEWARE-001", "APP-A-MIDDLEWARE-002"], "summary": "middleware bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id="canonical-inputs",
            phase="discovery",
            objective_id="app-a",
            capability="middleware",
            objective_outline=outline,
            default_sandbox_mode="read-only",
        )

        self.assertEqual(
            normalized["tasks"][1]["inputs"],
            [
                "Output of APP-A-MIDDLEWARE-001",
                "Planning Inputs.required_inbound_handoffs[0].deliverables[0]",
            ],
        )

    def test_normalize_capability_plan_rejects_nonexistent_future_repo_input_paths(self) -> None:
        scaffold_planning_run(self.project_root, "reject-future-paths", ["middleware"])
        outline = objective_outline_for_objective("reject-future-paths", "app-a", ["middleware"])
        plan = capability_plan_for_objective("reject-future-paths", "app-a", "middleware")
        plan["tasks"][0]["inputs"] = ["apps/todo/docs/discovery/missing-upstream-brief.md"]

        with self.assertRaises(ExecutorError):
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id="reject-future-paths",
                phase="discovery",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
                default_sandbox_mode="read-only",
            )

    def test_normalize_capability_plan_accepts_prior_phase_repo_inputs_from_integration_workspace(self) -> None:
        run_id = "allow-integration-workspace-inputs"
        scaffold_planning_run(self.project_root, run_id, ["backend"])
        outline = objective_outline_for_objective(run_id, "app-a", ["backend"])
        plan = capability_plan_for_objective(run_id, "app-a", "backend")
        plan["phase"] = "design"
        discovery_path = (
            "apps/todo/orchestrator/roles/objectives/"
            "simple-backend-api-and-persistence-layer-for-storing-todo-items/artifacts/discovery/backend-discovery-brief.md"
        )
        integration_file = self.project_root / ".orchestrator-worktrees" / run_id / "integration" / discovery_path
        integration_file.parent.mkdir(parents=True, exist_ok=True)
        integration_file.write_text("# backend discovery brief\n")
        plan["tasks"][0]["inputs"] = [discovery_path]
        plan["tasks"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "app-a-backend-discovery-plan",
                "path": "apps/app-a/backend-discovery-plan.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id=run_id,
            phase="design",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="read-only",
        )

        self.assertEqual(normalized["tasks"][0]["inputs"], [discovery_path])

    def test_normalize_capability_plan_backfills_required_handoff_output_ids(self) -> None:
        run_id = "backfill-required-handoff-outputs"
        scaffold_planning_run(self.project_root, run_id, ["backend"])
        outline = objective_outline_for_objective(
            run_id,
            "app-a",
            ["backend"],
            collaboration_edges=[
                {
                    "edge_id": "backend-review-handoff",
                    "from_capability": "backend",
                    "to_capability": "acceptance",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance reviews the backend design package.",
                    "deliverables": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.design.doc",
                            "path": "apps/todo/docs/design/backend.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "assertion",
                            "output_id": "backend.design.validated",
                            "path": None,
                            "asset_id": None,
                            "description": "Backend design validated.",
                            "evidence": {"validation_ids": ["validate_backend_doc"], "artifact_paths": ["apps/todo/docs/design/backend.md"]},
                        },
                    ],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        )
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "backend.design.doc",
                "path": "apps/todo/docs/design/backend.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "assertion",
                "output_id": "backend.design.validated",
                "path": None,
                "asset_id": None,
                "description": "Backend design validated.",
                "evidence": {"validation_ids": ["validate_backend_doc"], "artifact_paths": ["apps/todo/docs/design/backend.md"]},
            },
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": run_id,
            "phase": "design",
            "objective_id": "app-a",
            "capability": "backend",
            "summary": "Backend plan.",
            "tasks": [
                {
                    "task_id": "BACKEND-DESIGN-001",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write backend design package.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.design.doc",
                            "path": "apps/todo/docs/design/backend.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "assertion",
                            "output_id": "backend.design.validated",
                            "path": None,
                            "asset_id": None,
                            "description": "Backend design validated.",
                            "evidence": {"validation_ids": ["validate_backend_doc"], "artifact_paths": ["apps/todo/docs/design/backend.md"]},
                        },
                    ],
                    "done_when": ["backend design exists"],
                    "depends_on": [],
                    "validation": [{"id": "validate_backend_doc", "command": "test -f apps/todo/docs/design/backend.md"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                }
            ],
            "bundle_plan": [{"bundle_id": "backend-bundle", "task_ids": ["BACKEND-DESIGN-001"], "summary": "backend bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [
                {
                    "handoff_id": "backend-review-handoff",
                    "from_capability": "backend",
                    "to_capability": "acceptance",
                    "from_task_id": "BACKEND-DESIGN-001",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance reviews the backend package.",
                    "deliverable_output_ids": ["backend.design.doc"],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        }

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id=run_id,
            phase="design",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="read-only",
        )

        self.assertEqual(
            normalized["collaboration_handoffs"][0]["deliverable_output_ids"],
            ["backend.design.doc", "backend.design.validated"],
        )

    def test_normalize_capability_plan_requires_lane_output_coverage(self) -> None:
        run_id = "require-lane-output-coverage"
        scaffold_planning_run(self.project_root, run_id, ["backend"])
        outline = objective_outline_for_objective(run_id, "app-a", ["backend"])
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "backend.design.doc",
                "path": "apps/todo/docs/design/backend.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "backend.design.task_graph",
                "path": "apps/todo/docs/design/backend-task-graph.json",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": run_id,
            "phase": "design",
            "objective_id": "app-a",
            "capability": "backend",
            "summary": "Backend plan.",
            "tasks": [
                {
                    "task_id": "BACKEND-DESIGN-001",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write backend design doc.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.design.doc",
                            "path": "apps/todo/docs/design/backend.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["backend design exists"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                }
            ],
            "bundle_plan": [{"bundle_id": "backend-bundle", "task_ids": ["BACKEND-DESIGN-001"], "summary": "backend bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as exc:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id=run_id,
                phase="design",
                objective_id="app-a",
                capability="backend",
                objective_outline=outline,
                default_sandbox_mode="read-only",
            )

        self.assertIn("backend.design.task_graph", str(exc.exception))

    def test_normalize_capability_plan_requires_task_outputs(self) -> None:
        run_id = "require-task-outputs"
        scaffold_planning_run(self.project_root, run_id, ["middleware"])
        outline = objective_outline_for_objective(run_id, "app-a", ["middleware"])
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "assertion",
                "output_id": "middleware.design.reconciled",
                "path": None,
                "asset_id": None,
                "description": "Middleware design inputs are reconciled.",
                "evidence": {"validation_ids": ["validate_inputs"], "artifact_paths": []},
            }
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": run_id,
            "phase": "design",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware plan.",
            "tasks": [
                {
                    "task_id": "MW-DESIGN-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Reconcile middleware inputs.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": ["inputs are reconciled"],
                    "depends_on": [],
                    "validation": [{"id": "validate_inputs", "command": "true"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                }
            ],
            "bundle_plan": [{"bundle_id": "mw-bundle", "task_ids": ["MW-DESIGN-001"], "summary": "mw bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as exc:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id=run_id,
                phase="design",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
                default_sandbox_mode="read-only",
            )

        self.assertIn("must declare at least one expected output", str(exc.exception))

    def test_normalize_capability_plan_rejects_internal_only_discovery_task_split(self) -> None:
        run_id = "reject-internal-discovery-split"
        scaffold_planning_run(self.project_root, run_id, ["middleware"])
        outline = objective_outline_for_objective(run_id, "app-a", ["middleware"])
        outline["phase"] = "discovery"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "middleware.discovery.scope_brief",
                "path": "apps/todo/docs/discovery/integration-scope-brief.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "middleware.discovery.handoff_bundle",
                "path": "apps/todo/docs/discovery/integration-discovery-handoff.json",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": run_id,
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware discovery split.",
            "tasks": [
                {
                    "task_id": "MW-DISC-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Synthesize the middleware discovery scope.",
                    "inputs": ["Planning Inputs.goal_context.sections.Summary"],
                    "expected_outputs": [
                        {
                            "kind": "assertion",
                            "output_id": "middleware.discovery.synthesis_complete",
                            "path": None,
                            "asset_id": None,
                            "description": "A temporary synthesis exists.",
                            "evidence": {"validation_ids": ["validate_synthesis"], "artifact_paths": []},
                        }
                    ],
                    "done_when": ["synthesis exists"],
                    "depends_on": [],
                    "validation": [{"id": "validate_synthesis", "command": "true"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
                {
                    "task_id": "MW-DISC-002",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Materialize the middleware discovery bundle.",
                    "inputs": ["Output of MW-DISC-001"],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.scope_brief",
                            "path": "apps/todo/docs/discovery/integration-scope-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.handoff_bundle",
                            "path": "apps/todo/docs/discovery/integration-discovery-handoff.json",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                    ],
                    "done_when": ["discovery bundle exists"],
                    "depends_on": ["MW-DISC-001"],
                    "validation": [{"id": "validate_bundle", "command": "test -n discovery"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
            ],
            "bundle_plan": [
                {
                    "bundle_id": "mw-discovery-bundle",
                    "task_ids": ["MW-DISC-001", "MW-DISC-002"],
                    "summary": "Split discovery bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as exc:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id=run_id,
                phase="discovery",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
                default_sandbox_mode="read-only",
            )

        self.assertIn("internal-only", str(exc.exception))

    def test_normalize_capability_plan_rejects_placeholder_validation_executable(self) -> None:
        run_id = "reject-placeholder-validation-command"
        scaffold_planning_run(self.project_root, run_id, ["middleware"])
        outline = objective_outline_for_objective(run_id, "app-a", ["middleware"])
        outline["phase"] = "discovery"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "middleware.discovery.scope_brief",
                "path": "apps/todo/docs/discovery/integration-scope-brief.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": run_id,
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware discovery.",
            "tasks": [
                {
                    "task_id": "MW-DISC-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Produce the middleware discovery brief.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.scope_brief",
                            "path": "apps/todo/docs/discovery/integration-scope-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["brief exists"],
                    "depends_on": [],
                    "validation": [
                        {
                            "id": "validate_bundle",
                            "command": "check-discovery-bundle --brief apps/todo/docs/discovery/integration-scope-brief.md",
                        }
                    ],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                }
            ],
            "bundle_plan": [{"bundle_id": "mw-bundle", "task_ids": ["MW-DISC-001"], "summary": "mw bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as exc:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id=run_id,
                phase="discovery",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
                default_sandbox_mode="read-only",
            )

        self.assertIn("placeholder validator", str(exc.exception))

    def test_normalize_capability_plan_rejects_middleware_runtime_tree_in_mvp_build(self) -> None:
        objective_id = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        outline = {
            "schema": "objective-outline.v1",
            "run_id": "reject-middleware-runtime-tree",
            "phase": "mvp-build",
            "objective_id": objective_id,
            "summary": "Middleware-only MVP build outline.",
            "capability_lanes": [
                {
                    "capability": "middleware",
                    "assigned_manager_role": f"objectives.{objective_id}.middleware-manager",
                    "objective": "Validate the integrated build outputs and emit the review bundle.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware-review-bundle",
                            "path": "apps/todo/orchestrator/roles/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/build/middleware-review-bundle.json",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "reject-middleware-runtime-tree",
            "phase": "mvp-build",
            "objective_id": objective_id,
            "capability": "middleware",
            "summary": "Bad middleware build plan.",
            "tasks": [
                {
                    "task_id": "middleware-runtime-wiring",
                    "capability": "middleware",
                    "assigned_role": f"objectives.{objective_id}.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Build a parallel runtime tree.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "middleware-runtime-server",
                            "path": "apps/todo/runtime/server.js",
                            "asset_id": "middleware-runtime-server",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "middleware-runtime-bundle",
                    "task_ids": ["middleware-runtime-wiring"],
                    "summary": "Bad middleware runtime bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id="reject-middleware-runtime-tree",
                phase="mvp-build",
                objective_id=objective_id,
                capability="middleware",
                objective_outline=outline,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("must consume existing frontend/backend outputs", str(ctx.exception))

    def test_normalize_capability_plan_requires_consolidation_task_for_multi_task_review_handoff(self) -> None:
        scaffold_planning_run(self.project_root, "reject-mixed-source-handoff", ["backend"])
        outline = objective_outline_for_objective(
            "reject-mixed-source-handoff",
            "app-a",
            ["backend"],
            collaboration_edges=[
                {
                    "edge_id": "backend-review-bundle",
                    "from_capability": "backend",
                    "to_capability": "acceptance",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance reviews the backend discovery bundle.",
                    "deliverables": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.discovery.brief",
                            "path": "apps/todo/docs/discovery/backend-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "artifact",
                            "output_id": "backend.discovery.task-graph",
                            "path": "apps/todo/docs/discovery/backend-task-graph.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                    ],
                    "blocking": True,
                    "shared_asset_ids": ["backend-review-bundle"],
                }
            ],
        )
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "backend.discovery.brief",
                "path": "apps/todo/docs/discovery/backend-brief.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "backend.discovery.task-graph",
                "path": "apps/todo/docs/discovery/backend-task-graph.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "reject-mixed-source-handoff",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "backend",
            "summary": "backend discovery",
            "tasks": [
                {
                    "task_id": "BACKEND-BRIEF",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/docs/discovery/backend-brief.md"],
                    "shared_asset_ids": ["backend-review-bundle"],
                    "objective": "Write the backend brief.",
                    "inputs": ["Planning Inputs.goal_markdown"],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.discovery.brief",
                            "path": "apps/todo/docs/discovery/backend-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["brief exists"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                },
                {
                    "task_id": "BACKEND-TASK-GRAPH",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/docs/discovery/backend-task-graph.md"],
                    "shared_asset_ids": ["backend-review-bundle"],
                    "objective": "Write the backend task graph.",
                    "inputs": ["Output of BACKEND-BRIEF"],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "backend.discovery.task-graph",
                            "path": "apps/todo/docs/discovery/backend-task-graph.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["task graph exists"],
                    "depends_on": ["BACKEND-BRIEF"],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                },
            ],
            "bundle_plan": [
                {
                    "bundle_id": "backend-bundle",
                    "task_ids": ["BACKEND-BRIEF", "BACKEND-TASK-GRAPH"],
                    "summary": "backend bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [
                {
                    "handoff_id": "backend-review-bundle",
                    "from_capability": "backend",
                    "to_capability": "acceptance",
                    "from_task_id": "BACKEND-TASK-GRAPH",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance reviews the backend discovery bundle.",
                    "deliverable_output_ids": [
                        "backend.discovery.brief",
                        "backend.discovery.task-graph",
                    ],
                    "blocking": True,
                    "shared_asset_ids": ["backend-review-bundle"],
                }
            ],
        }

        with self.assertRaises(ExecutorError) as raised:
            normalize_capability_plan(
                self.project_root,
                plan,
                run_id="reject-mixed-source-handoff",
                phase="discovery",
                objective_id="app-a",
                capability="backend",
                objective_outline=outline,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("Create a final consolidation task", str(raised.exception))

    def test_render_prompt_uses_effective_sandbox_override(self) -> None:
        scaffold_planning_run(self.project_root, "render-sandbox", ["backend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "render-sandbox",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "backend",
            "task_id": "APP-A-BACKEND-EXEC-001",
            "assigned_role": "objectives.app-a.backend-worker",
            "manager_role": "objectives.app-a.backend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Write a backend artifact.",
            "inputs": [],
            "expected_outputs": ["backend_constraints_draft"],
            "owned_paths": ["apps/todo/backend/data"],
            "sandbox_mode": "read-only",
            "execution_mode": "read_only",
            "parallel_policy": "serialize",
            "done_when": ["artifact exists"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "additional_directories": [],
        }
        task_path = self.project_root / "runs" / "render-sandbox" / "tasks" / "APP-A-BACKEND-EXEC-001.json"
        write_json(task_path, task)

        metadata = render_prompt(
            self.project_root,
            "render-sandbox",
            task_path,
            sandbox_mode="workspace-write",
        )
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()

        self.assertIn('"sandbox_mode": "workspace-write"', prompt_text)

    def test_render_prompt_uses_task_payload_override(self) -> None:
        scaffold_planning_run(self.project_root, "render-override", ["backend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "render-override",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "backend",
            "task_id": "APP-A-BACKEND-EXEC-002",
            "assigned_role": "objectives.app-a.backend-worker",
            "manager_role": "objectives.app-a.backend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Use the normalized task payload.",
            "inputs": [],
            "expected_outputs": ["apps/todo/backend/discovery/backend-discovery-brief.md"],
            "owned_paths": ["apps/todo/backend/discovery/backend-discovery-brief.md"],
            "sandbox_mode": "read-only",
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "done_when": ["artifact exists"],
            "depends_on": [],
            "validation": [
                {
                    "id": "scope-brief-file-exists",
                    "command": "test -f backend/discovery/backend-discovery-brief.md",
                }
            ],
            "collaboration_rules": [],
            "additional_directories": [],
        }
        task_path = self.project_root / "runs" / "render-override" / "tasks" / "APP-A-BACKEND-EXEC-002.json"
        write_json(task_path, task)
        overridden_task = json.loads(json.dumps(task))
        overridden_task["sandbox_mode"] = "workspace-write"
        overridden_task["validation"][0]["command"] = "test -f apps/todo/backend/discovery/backend-discovery-brief.md"

        metadata = render_prompt(
            self.project_root,
            "render-override",
            task_path,
            sandbox_mode="workspace-write",
            task_payload=overridden_task,
        )
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()

        self.assertIn('"sandbox_mode": "workspace-write"', prompt_text)
        self.assertIn("test -f apps/todo/backend/discovery/backend-discovery-brief.md", prompt_text)
        self.assertNotIn("test -f backend/discovery/backend-discovery-brief.md", prompt_text)

    def test_render_prompt_resolves_capability_planning_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "capability-input-resolution", ["frontend", "backend", "middleware"])
        objective_outline = objective_outline_for_objective(
            "capability-input-resolution",
            "app-a",
            ["frontend", "backend", "middleware"],
            collaboration_edges=[
                {
                    "edge_id": "edge-fe-mw",
                    "from_capability": "frontend",
                    "to_capability": "middleware",
                    "to_role": "objectives.app-a.middleware-manager",
                    "handoff_type": "consumer_needs",
                    "deliverables": ["Frontend delivers consumer needs."],
                    "blocking": True,
                    "shared_asset_ids": ["consumer-needs"],
                },
                {
                    "edge_id": "edge-mw-be",
                    "from_capability": "middleware",
                    "to_capability": "backend",
                    "to_role": "objectives.app-a.backend-manager",
                    "handoff_type": "provider_constraints",
                    "deliverables": ["Middleware delivers provider constraints."],
                    "blocking": True,
                    "shared_asset_ids": ["provider-constraints"],
                },
            ],
        )
        write_json(
            self.project_root / "runs" / "capability-input-resolution" / "manager-plans" / "discovery-app-a.outline.json",
            objective_outline,
        )
        existing_task = {
            "schema": "task-assignment.v1",
            "run_id": "capability-input-resolution",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "task_id": "APP-A-MW-PRIOR",
            "assigned_role": "objectives.app-a.middleware-worker",
            "manager_role": "objectives.app-a.middleware-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Prior middleware discovery task.",
            "inputs": [],
            "expected_outputs": ["docs/middleware-prior.md"],
            "done_when": ["prior task exists"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(
            self.project_root / "runs" / "capability-input-resolution" / "tasks" / "APP-A-MW-PRIOR.json",
            existing_task,
        )
        task = {
            "schema": "task-assignment.v1",
            "run_id": "capability-input-resolution",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "task_id": "APP-A-MW-001",
            "assigned_role": "objectives.app-a.middleware-worker",
            "manager_role": "objectives.app-a.middleware-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Use capability planning inputs.",
            "inputs": [
                "Runtime Context.capability",
                "Planning Inputs.goal_context.sections.In Scope",
                "Planning Inputs.capability_lane.objective",
                "Planning Inputs.existing_capability_tasks_by_id.APP-A-MW-PRIOR.expected_outputs[0]",
                "Planning Inputs.required_inbound_handoffs[0].deliverables[0]",
                "Planning Inputs.required_outbound_handoffs[0].deliverables[0]",
                "Planning Inputs.objective_outline.relevant_collaboration_edges[0].deliverables[0]",
            ],
            "expected_outputs": [],
            "done_when": ["resolved inputs are present"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        task_path = self.project_root / "runs" / "capability-input-resolution" / "tasks" / "APP-A-MW-001.json"
        write_json(task_path, task)
        metadata = render_prompt(self.project_root, "capability-input-resolution", task_path)
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text()
        self.assertIn("Frontend delivers consumer needs.", prompt_text)
        self.assertIn("Middleware delivers provider constraints.", prompt_text)
        self.assertNotIn('"missing_path": "capability"', prompt_text)
        self.assertIn("docs/middleware-prior.md", prompt_text)
        self.assertNotIn('"missing_path": "capability_lane.objective"', prompt_text)
        self.assertNotIn('"missing_path": "existing_capability_tasks_by_id.APP-A-MW-PRIOR.expected_outputs[0]"', prompt_text)
        self.assertNotIn('"missing_path": "required_inbound_handoffs[0].deliverables[0]"', prompt_text)
        self.assertNotIn('"missing_path": "required_outbound_handoffs[0].deliverables[0]"', prompt_text)
        self.assertNotIn('"missing_path": "objective_outline.relevant_collaboration_edges[0].deliverables[0]"', prompt_text)

    def test_aggregate_capability_plans_attach_inbound_handoff_dependencies_from_required_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "inbound-handoff-deps", ["backend", "middleware"])
        outline = objective_outline_for_objective(
            "inbound-handoff-deps",
            "app-a",
            ["backend", "middleware"],
            collaboration_edges=[
                {
                    "edge_id": "edge-backend-to-middleware",
                    "from_capability": "backend",
                    "to_capability": "middleware",
                    "to_role": "objectives.app-a.middleware-manager",
                    "handoff_type": "contract",
                    "deliverables": [
                        {
                            "kind": "assertion",
                            "output_id": "contract.todo-api-v1",
                            "path": None,
                            "asset_id": None,
                            "description": "Backend contract summary for middleware.",
                            "evidence": {"validation_ids": [], "artifact_paths": []},
                        },
                        {
                            "kind": "assertion",
                            "output_id": "decision.todo-backend-stack-and-persistence",
                            "path": None,
                            "asset_id": None,
                            "description": "Backend stack and persistence decision summary for middleware.",
                            "evidence": {"validation_ids": [], "artifact_paths": []},
                        },
                    ],
                    "blocking": True,
                    "shared_asset_ids": ["app-a:api-contract"],
                }
            ],
        )
        backend_plan = capability_plan_for_objective(
            "inbound-handoff-deps",
            "app-a",
            "backend",
            collaboration_handoffs=[
                {
                    "handoff_id": "app-a-backend-contract-to-middleware-handshake",
                    "from_capability": "backend",
                    "from_task_id": "APP-A-BACKEND-001",
                    "to_role": "objectives.app-a.middleware-manager",
                    "to_capability": "middleware",
                    "handoff_type": "contract",
                    "reason": "Middleware needs the backend contract handshake.",
                    "deliverable_output_ids": [
                        "contract.todo-api-v1",
                        "decision.todo-backend-stack-and-persistence",
                    ],
                    "blocking": True,
                    "shared_asset_ids": ["app-a:api-contract"],
                }
            ],
        )
        backend_plan["tasks"][0]["expected_outputs"] = [
            {
                "kind": "assertion",
                "output_id": "contract.todo-api-v1",
                "path": None,
                "asset_id": None,
                "description": "Backend contract summary for middleware.",
                "evidence": {"validation_ids": [], "artifact_paths": []},
            },
            {
                "kind": "assertion",
                "output_id": "decision.todo-backend-stack-and-persistence",
                "path": None,
                "asset_id": None,
                "description": "Backend stack and persistence decision summary for middleware.",
                "evidence": {"validation_ids": [], "artifact_paths": []},
            },
        ]
        middleware_plan = capability_plan_for_objective("inbound-handoff-deps", "app-a", "middleware")
        middleware_task = middleware_plan["tasks"][0]
        middleware_task["task_id"] = "APP-A-MW-001"
        middleware_task["inputs"] = ["Planning Inputs.required_inbound_handoffs[0].deliverables"]
        middleware_task["shared_asset_ids"] = ["app-a:integration-contract"]
        middleware_plan["bundle_plan"][0]["task_ids"] = ["APP-A-MW-001"]

        normalized_backend, _ = normalize_capability_plan(
            self.project_root,
            backend_plan,
            run_id="inbound-handoff-deps",
            phase="discovery",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="workspace-write",
        )
        normalized_middleware, _ = normalize_capability_plan(
            self.project_root,
            middleware_plan,
            run_id="inbound-handoff-deps",
            phase="discovery",
            objective_id="app-a",
            capability="middleware",
            objective_outline=outline,
            default_sandbox_mode="workspace-write",
        )

        aggregated = aggregate_capability_plans(
            self.project_root,
            "inbound-handoff-deps",
            "discovery",
            "app-a",
            outline,
            [normalized_backend, normalized_middleware],
        )

        resolved_middleware_task = next(task for task in aggregated["tasks"] if task["task_id"] == "APP-A-MW-001")
        self.assertIn(
            "app-a-backend-contract-to-middleware-handshake",
            resolved_middleware_task["handoff_dependencies"],
        )
        self.assertIn("contract.todo-api-v1", resolved_middleware_task["shared_asset_ids"])
        self.assertIn(
            "decision.todo-backend-stack-and-persistence",
            resolved_middleware_task["shared_asset_ids"],
        )

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
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
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
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
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

    def test_build_capability_planning_payload_includes_related_same_app_reports(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "related-capability-prompt", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "related-capability-prompt",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "related-capability-prompt")
        generate_role_files(self.project_root, "related-capability-prompt", approve=True)
        write_json(
            run_dir / "reports" / "STACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-capability-prompt",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "STACK-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Created the backend service and persistence design, locking the MVP backend onto a local SQLite persistence model.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        objective_outline = {
            "schema": "objective-outline.v1",
            "run_id": "related-capability-prompt",
            "phase": "mvp-build",
            "objective_id": integration_objective,
            "summary": "Integration backend MVP build.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "objective": "Integrate backend build work.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }

        payload = build_capability_planning_payload(
            self.project_root,
            "related-capability-prompt",
            integration_objective,
            "backend",
            objective_outline,
        )

        self.assertEqual(len(payload["related_prior_phase_reports"]), 1)
        self.assertEqual(payload["related_prior_phase_reports"][0]["objective_id"], backend_objective)
        self.assertIn("SQLite persistence model", payload["related_prior_phase_reports"][0]["summary"])

    def test_build_planning_payload_includes_related_same_app_reports(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "related-planning-prompt", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "related-planning-prompt",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "related-planning-prompt")
        generate_role_files(self.project_root, "related-planning-prompt", approve=True)
        write_json(
            run_dir / "reports" / "STACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-planning-prompt",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "STACK-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Created the backend service and persistence design, locking the MVP backend onto a local SQLite persistence model.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )

        payload = build_planning_payload(
            self.project_root,
            "related-planning-prompt",
            integration_objective,
        )

        self.assertEqual(len(payload["related_prior_phase_reports"]), 1)
        self.assertEqual(payload["related_prior_phase_reports"][0]["objective_id"], backend_objective)
        self.assertIn("SQLite persistence model", payload["related_prior_phase_reports"][0]["summary"])

    def test_build_planning_payload_prefers_immediately_previous_related_phase_reports(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "related-phase-priority", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "related-phase-priority",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "related-phase-priority")
        generate_role_files(self.project_root, "related-phase-priority", approve=True)
        write_json(
            run_dir / "reports" / "DISC-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-phase-priority",
                "phase": "discovery",
                "objective_id": backend_objective,
                "task_id": "DISC-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Discovery narrowed backend options but left persistence unresolved.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "DESIGN-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-phase-priority",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "DESIGN-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Design locked the backend onto SQLite persistence.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )

        payload = build_planning_payload(
            self.project_root,
            "related-phase-priority",
            integration_objective,
        )

        self.assertEqual([item["phase"] for item in payload["related_prior_phase_reports"]], ["design"])
        self.assertIn("SQLite", payload["related_prior_phase_reports"][0]["summary"])

    def test_build_planning_prompt_payload_prioritizes_backend_persistence_related_report_under_tight_limit(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "related-prompt-priority", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        frontend_objective = "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "related-prompt-priority",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": frontend_objective,
                    "title": "React web frontend for creating, viewing, completing, editing, and deleting todo items",
                    "summary": "React web frontend for creating, viewing, completing, editing, and deleting todo items",
                    "status": "approved",
                    "capabilities": ["frontend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "related-prompt-priority")
        generate_role_files(self.project_root, "related-prompt-priority", approve=True)
        write_json(
            run_dir / "reports" / "FE-DESIGN.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-prompt-priority",
                "phase": "design",
                "objective_id": frontend_objective,
                "task_id": "FE-DESIGN",
                "agent_role": f"objectives.{frontend_objective}.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Frontend design spec for the todo UI.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "BE-DESIGN.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-prompt-priority",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "BE-DESIGN",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Backend stack design locked the app onto SQLite persistence and a stable todo API contract.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )

        payload = build_planning_prompt_payload(
            self.project_root,
            "related-prompt-priority",
            integration_objective,
            compaction={"limits": {"prior_reports": 1, "prior_artifacts": 1, "catalog_reports": 12, "catalog_artifacts": 12}},
        )

        self.assertEqual(len(payload["related_prior_phase_reports"]), 2)
        self.assertEqual(payload["related_prior_phase_reports"][0]["objective_id"], backend_objective)
        self.assertIn("SQLite", payload["related_prior_phase_reports"][0]["summary"])

    def test_build_capability_prompt_payload_prioritizes_backend_design_related_report_under_tight_limit(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "related-capability-priority", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "related-capability-priority",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "related-capability-priority")
        generate_role_files(self.project_root, "related-capability-priority", approve=True)
        write_json(
            run_dir / "reports" / "BE-DISCOVERY.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-capability-priority",
                "phase": "discovery",
                "objective_id": backend_objective,
                "task_id": "BE-DISCOVERY",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Discovery left backend persistence unresolved.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "BE-DESIGN.json",
            {
                "schema": "completion-report.v1",
                "run_id": "related-capability-priority",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "BE-DESIGN",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Backend stack design locked the app onto SQLite persistence and a stable todo API contract.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        objective_outline = {
            "schema": "objective-outline.v1",
            "run_id": "related-capability-priority",
            "phase": "mvp-build",
            "objective_id": integration_objective,
            "summary": "Integration backend MVP build.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "objective": "Integrate backend build work.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }

        payload = build_capability_prompt_payload(
            self.project_root,
            "related-capability-priority",
            integration_objective,
            "backend",
            objective_outline,
            compaction={"limits": {"prior_reports": 1, "prior_artifacts": 1, "catalog_reports": 12, "catalog_artifacts": 12, "outline_edges": 8}},
        )

        self.assertEqual(len(payload["related_prior_phase_reports"]), 1)
        self.assertEqual(payload["related_prior_phase_reports"][0]["task_id"], "BE-DESIGN")
        self.assertIn("SQLite", payload["related_prior_phase_reports"][0]["summary"])

    def test_build_planning_payload_can_ignore_existing_current_phase_tasks_when_replacing(self) -> None:
        scaffold_planning_run(self.project_root, "replace-existing-tasks", ["backend"])
        run_dir = self.project_root / "runs" / "replace-existing-tasks"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "tasks" / "backend-mvp-01-repository.json",
            {
                "schema": "task-assignment.v1",
                "run_id": "replace-existing-tasks",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "backend-mvp-01-repository",
                "capability": "backend",
                "assigned_role": "objectives.app-a.backend-worker",
                "objective": "Stale backend task from the previous plan.",
                "inputs": [],
                "expected_outputs": ["apps/app-a/backend/data/todos.json"],
                "done_when": ["stale task complete"],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
                "working_directory": None,
                "sandbox_mode": "workspace-write",
                "additional_directories": [],
            },
        )

        payload = build_planning_payload(self.project_root, "replace-existing-tasks", "app-a")
        replace_payload = build_planning_payload(
            self.project_root,
            "replace-existing-tasks",
            "app-a",
            ignore_existing_phase_tasks=True,
        )

        self.assertEqual(len(payload["existing_phase_tasks"]), 1)
        self.assertEqual(replace_payload["existing_phase_tasks"], [])

    def test_quarantined_objective_phase_artifacts_restore_on_failure(self) -> None:
        scaffold_planning_run(self.project_root, "quarantine-restore", ["backend"])
        run_dir = self.project_root / "runs" / "quarantine-restore"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        task_path = run_dir / "tasks" / "backend-mvp-01-repository.json"
        write_json(
            task_path,
            {
                "schema": "task-assignment.v1",
                "run_id": "quarantine-restore",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "backend-mvp-01-repository",
                "capability": "backend",
                "assigned_role": "objectives.app-a.backend-worker",
                "objective": "Stale backend task from the previous plan.",
                "inputs": [],
                "expected_outputs": ["apps/app-a/backend/data/todos.json"],
                "done_when": ["stale task complete"],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
                "working_directory": None,
                "sandbox_mode": "workspace-write",
                "additional_directories": [],
            },
        )
        manager_plan_path = run_dir / "manager-plans" / "mvp-build-app-a.json"
        write_json(
            manager_plan_path,
            {
                "schema": "objective-plan.v1",
                "run_id": "quarantine-restore",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "summary": "Stale plan.",
                "tasks": [],
                "bundle_plan": [],
                "dependency_notes": [],
                "collaboration_handoffs": [],
            },
        )

        with self.assertRaises(RuntimeError):
            with quarantined_objective_phase_artifacts(
                self.project_root,
                "quarantine-restore",
                "mvp-build",
                "app-a",
                enabled=True,
            ):
                self.assertFalse(task_path.exists())
                self.assertFalse(manager_plan_path.exists())
                raise RuntimeError("boom")

        self.assertTrue(task_path.exists())
        self.assertTrue(manager_plan_path.exists())

    def test_normalize_capability_plan_rejects_backend_persistence_drift_against_related_reports(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "backend-persistence-drift", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "backend-persistence-drift",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "backend-persistence-drift")
        generate_role_files(self.project_root, "backend-persistence-drift", approve=True)
        write_json(
            run_dir / "reports" / "STACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "backend-persistence-drift",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "STACK-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Created the backend service and persistence design, locking the MVP backend onto a local SQLite persistence model.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        objective_outline = {
            "schema": "objective-outline.v1",
            "run_id": "backend-persistence-drift",
            "phase": "mvp-build",
            "objective_id": integration_objective,
            "summary": "Integration backend MVP build.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "objective": "Integrate backend build work.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }
        drifting_plan = {
            "schema": "capability-plan.v1",
            "run_id": "backend-persistence-drift",
            "phase": "mvp-build",
            "objective_id": integration_objective,
            "capability": "backend",
            "summary": "Incorrectly switch persistence to a JSON file.",
            "tasks": [
                {
                    "task_id": "backend-mvp-01-repository",
                    "capability": "backend",
                    "assigned_role": f"objectives.{integration_objective}.backend-worker",
                    "objective": "Implement file-backed persistence.",
                    "inputs": [],
                    "expected_outputs": [
                        "Updated apps/todo/backend/src/todos/repository.js implementing persistent todo CRUD storage against apps/todo/backend/data/todos.json"
                    ],
                    "done_when": [
                        "apps/todo/backend/src/todos/repository.js reads from and writes to apps/todo/backend/data/todos.json for durable todo storage."
                    ],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "sandbox_mode": "workspace-write",
                    "additional_directories": [],
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "backend-bundle",
                    "task_ids": ["backend-mvp-01-repository"],
                    "review_prompt": "Review backend MVP persistence work.",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as context:
            normalize_capability_plan(
                self.project_root,
                drifting_plan,
                run_id="backend-persistence-drift",
                phase="mvp-build",
                objective_id=integration_objective,
                capability="backend",
                objective_outline=objective_outline,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("SQLite", str(context.exception))
        self.assertIn("JSON-file persistence", str(context.exception))

    def test_normalize_objective_outline_rejects_backend_persistence_drift_against_related_reports(self) -> None:
        goal_text = (REPO_ROOT / "apps" / "todo" / "goal-draft.md").read_text()
        run_dir = initialize_run(self.project_root, "objective-outline-persistence-drift", goal_text)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["phases"][0]["status"] = "complete"
        phase_plan["phases"][0]["human_approved"] = True
        phase_plan["phases"][1]["status"] = "complete"
        phase_plan["phases"][1]["human_approved"] = True
        phase_plan["phases"][2]["status"] = "active"
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)
        backend_objective = "simple-backend-api-and-persistence-layer-for-storing-todo-items"
        integration_objective = "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend"
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "objective-outline-persistence-drift",
            "objectives": [
                {
                    "objective_id": backend_objective,
                    "title": "Simple backend API and persistence layer for storing todo items",
                    "summary": "Simple backend API and persistence layer for storing todo items",
                    "status": "approved",
                    "capabilities": ["backend"],
                },
                {
                    "objective_id": integration_objective,
                    "title": "Basic application integration and delivery workflow connecting frontend and backend",
                    "summary": "Basic application integration and delivery workflow connecting frontend and backend",
                    "status": "approved",
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "objective-outline-persistence-drift")
        generate_role_files(self.project_root, "objective-outline-persistence-drift", approve=True)
        write_json(
            run_dir / "reports" / "STACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "objective-outline-persistence-drift",
                "phase": "design",
                "objective_id": backend_objective,
                "task_id": "STACK-001",
                "agent_role": f"objectives.{backend_objective}.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Created the backend service and persistence design, locking the MVP backend onto a local SQLite persistence model.",
                "artifacts": [],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        drifting_outline = {
            "schema": "objective-outline.v1",
            "run_id": "objective-outline-persistence-drift",
            "phase": "mvp-build",
            "objective_id": integration_objective,
            "summary": "Integration backend MVP build.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "assigned_manager_role": f"objectives.{integration_objective}.backend-manager",
                    "objective": "Implement the approved todo API contract end to end over durable JSON persistence.",
                    "inputs": [],
                    "expected_outputs": [
                        "Updated apps/todo/backend/src/todos/repository.js implementing persistent todo CRUD storage against apps/todo/backend/data/todos.json"
                    ],
                    "done_when": [
                        "The backend persists todos durably to apps/todo/backend/data/todos.json."
                    ],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }

        with self.assertRaises(ExecutorError) as context:
            normalize_objective_outline(
                self.project_root,
                drifting_outline,
                run_id="objective-outline-persistence-drift",
                phase="mvp-build",
                objective={
                    "objective_id": integration_objective,
                    "capabilities": ["backend", "frontend", "middleware"],
                },
            )

        self.assertIn("SQLite", str(context.exception))
        self.assertIn("JSON-file persistence", str(context.exception))

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
            "legacy_dependency_notes": [long_evidence],
            "open_issues": [long_evidence],
            "legacy_follow_ups": [],
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

    def test_execute_task_writes_completion_report_from_codex_output(self) -> None:
        scaffold_smoke_test(self.project_root, "exec")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": ["context-echo"],
                        "artifact_paths": [],
                    },
                }
            ],
            "artifacts": [{"path": "runs/exec/prompt-logs/APP-A-SMOKE-001.prompt.md", "status": "referenced"}],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "discovery",
                "prompt_layers": [
                    "orchestrator/roles/base/company.md",
                    "orchestrator/roles/base/worker.md",
                    "orchestrator/roles/capabilities/frontend.md",
                    "orchestrator/roles/objectives/app-a/approved/frontend-worker.md",
                    "orchestrator/phase-overlays/discovery/task-execution.md",
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

    def test_execute_task_records_observability_artifacts(self) -> None:
        scaffold_smoke_test(self.project_root, "exec-observability")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": [],
                        "artifact_paths": [],
                    },
                }
            ],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-obs"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="stderr line\n", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            execute_task(self.project_root, "exec-observability", "APP-A-SMOKE-001")

        llm_calls = read_json_lines(self.project_root / "runs" / "exec-observability" / "live" / "llm-calls.jsonl")
        self.assertEqual(len(llm_calls), 1)
        self.assertEqual(llm_calls[0]["kind"], "task_execution")
        self.assertEqual(llm_calls[0]["input_tokens"], 10)
        self.assertEqual(llm_calls[0]["cached_input_tokens"], 2)
        self.assertEqual(llm_calls[0]["output_tokens"], 5)

        report = read_json(
            self.project_root / "runs" / "exec-observability" / "reports" / "APP-A-SMOKE-001.json"
        )
        self.assertEqual(report["runtime_observability"]["llm_call_count"], 1)
        self.assertEqual(report["runtime_observability"]["input_tokens"], 10)
        self.assertEqual(report["runtime_observability"]["cached_input_tokens"], 2)
        self.assertEqual(report["runtime_observability"]["output_tokens"], 5)

        run_observability = read_json(
            self.project_root / "runs" / "exec-observability" / "live" / "observability.json"
        )
        self.assertEqual(run_observability["total_calls"], 1)
        self.assertEqual(run_observability["completed_calls"], 1)
        self.assertEqual(run_observability["calls_by_kind"]["task_execution"], 1)

    def test_execute_task_streams_live_activity_updates_and_events(self) -> None:
        scaffold_smoke_test(self.project_root, "exec-live")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": [],
                        "artifact_paths": [],
                    },
                }
            ],
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
            "open_issues": ["shared utility needs an approved change"],
            "change_requests": [],
            "produced_outputs": [],
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
        request = read_json(
            self.project_root / "runs" / "collab-exec" / "collaboration" / "APP-A-SMOKE-001-CR-001.json"
        )
        self.assertEqual(request["to_role"], "shared-platform.custodian")

    def test_execute_task_persists_goal_critical_change_requests(self) -> None:
        scaffold_smoke_test(self.project_root, "change-request-exec")
        final_payload = {
            "summary": "Blocked on a shared contract change.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "open_issues": ["Blocking: the shared API contract must change before implementation can continue."],
            "change_requests": [
                {
                    "change_category": "interface_contract",
                    "summary": "Align the shared API contract with the approved backend behavior.",
                    "blocking_reason": "The injected frontend and backend contract inputs disagree on request validation.",
                    "why_local_resolution_is_invalid": "Changing only this task would fork the shared contract used by sibling objectives.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": ["todo-api-contract"],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": ["simple-backend-api-and-persistence-layer-for-storing-todo-items"],
                    "impacted_task_ids": ["backend-api-surface-and-review-bundle"],
                    "conflicting_input_refs": [],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": False,
                    },
                }
            ],
            "produced_outputs": [],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-change"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            summary = execute_task(self.project_root, "change-request-exec", "APP-A-SMOKE-001")

        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(len(summary["change_request_ids"]), 1)
        report = read_json(
            self.project_root / "runs" / "change-request-exec" / "reports" / "APP-A-SMOKE-001.json"
        )
        self.assertEqual(len(report["change_requests"]), 1)
        self.assertEqual(report["change_requests"][0]["approval"], {"mode": "auto", "status": "approved"})

    def test_materialize_executor_response_converts_local_contract_change_request_to_collaboration_request(self) -> None:
        scaffold_planning_run(self.project_root, "local-contract-repair", ["frontend"])
        run_dir = self.project_root / "runs" / "local-contract-repair"
        task = {
            "schema": "task-assignment.v1",
            "run_id": "local-contract-repair",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-LOCAL-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Wire the frontend entrypoint.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend_entrypoint",
                    "path": "apps/todo/frontend/src/index.js",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": [],
            "writes_existing_paths": [],
        }
        parsed_response = {
            "summary": "Blocked on an inconsistent local task contract.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "open_issues": ["Blocking: required entrypoint already exists but is outside the allowed edit set."],
            "change_requests": [
                {
                    "change_category": "interface_contract",
                    "summary": "Authorize edits to the existing frontend entrypoint.",
                    "blocking_reason": "The required output path already exists and Allowed Existing-File Edits excludes it.",
                    "why_local_resolution_is_invalid": "Editing the existing file would violate the task contract.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": ["frontend_entrypoint"],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": [],
                    "impacted_task_ids": [],
                    "conflicting_input_refs": [],
                    "required_reentry_phase": "mvp-build",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": True,
                    },
                }
            ],
            "produced_outputs": [],
            "context_echo": None,
            "collaboration_request": None,
        }

        report, collaboration_ids, change_request_ids = materialize_executor_response(
            self.project_root,
            "local-contract-repair",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(change_request_ids, [])
        self.assertEqual(report["change_requests"], [])
        self.assertEqual(len(collaboration_ids), 1)
        collaboration = read_json(
            run_dir / "collaboration" / f"{collaboration_ids[0]}.json"
        )
        self.assertEqual(collaboration["to_role"], "objectives.app-a.frontend-manager")
        self.assertEqual(collaboration["type"], "contract_resolution")

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

    def test_execute_task_retries_timeout_when_using_policy_defaults(self) -> None:
        import subprocess

        scaffold_smoke_test(self.project_root, "timeout-retry-exec")
        final_payload = {
            "summary": "Recovered after timeout retry.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": [],
                        "artifact_paths": [],
                    },
                }
            ],
            "context_echo": None,
            "collaboration_request": None,
        }
        timeout_error = subprocess.TimeoutExpired(
            cmd=["codex", "exec"],
            timeout=300,
            output=b'{"type":"thread.started","thread_id":"thread-timeout-retry"}\n',
            stderr=b"worker still reasoning\n",
        )
        completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-timeout-retry-2"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        with patch("company_orchestrator.executor.run_codex_command", side_effect=[timeout_error, completed]):
            summary = execute_task(self.project_root, "timeout-retry-exec", "APP-A-SMOKE-001", timeout_seconds=None)
        self.assertEqual(summary["status"], "ready_for_bundle_review")
        report = read_json(self.project_root / "runs" / "timeout-retry-exec" / "reports" / "APP-A-SMOKE-001.json")
        self.assertEqual(report["runtime_recovery"]["timeout_retries_used"], 1)
        activity = read_activity(self.project_root, "timeout-retry-exec", "APP-A-SMOKE-001")
        self.assertEqual(activity["status"], "recovered")
        events = read_json_lines(self.project_root / "runs" / "timeout-retry-exec" / "live" / "events.jsonl")
        self.assertIn("task.timeout_retry_scheduled", {event["event_type"] for event in events})

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

    def test_run_phase_rolls_up_observability_into_phase_report(self) -> None:
        scaffold_smoke_test(self.project_root, "managed-observability")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": [],
                        "artifact_paths": [],
                    },
                }
            ],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-managed-obs"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            summary = run_phase(self.project_root, "managed-observability")

        self.assertEqual(summary["recommendation"], "advance")
        report = read_json(
            self.project_root / "runs" / "managed-observability" / "phase-reports" / "discovery.json"
        )
        self.assertEqual(report["observability_summary"]["total_calls"], 2)
        self.assertEqual(report["observability_summary"]["completed_calls"], 2)
        self.assertEqual(report["observability_summary"]["total_input_tokens"], 20)
        self.assertEqual(report["observability_summary"]["total_cached_input_tokens"], 2)
        self.assertEqual(report["observability_summary"]["total_output_tokens"], 10)

    def test_generate_phase_report_accepts_objective_with_no_phase_work(self) -> None:
        scaffold_planning_run(self.project_root, "no-phase-work-report", ["backend"])
        run_dir = self.project_root / "runs" / "no-phase-work-report"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "manager-plans" / "design-app-a.json",
            {
                "schema": "objective-plan.v1",
                "run_id": "no-phase-work-report",
                "phase": "design",
                "objective_id": "app-a",
                "summary": "No design work required for this objective.",
                "tasks": [],
                "bundle_plan": [],
                "dependency_notes": [],
                "collaboration_handoffs": [],
            },
        )

        report, _ = generate_phase_report(self.project_root, "no-phase-work-report")

        self.assertEqual(report["recommendation"], "advance")
        self.assertEqual(
            report["objective_outcomes"],
            [{"objective_id": "app-a", "status": "accepted", "accepted_bundles": []}],
        )

    def test_generate_polish_phase_report_requires_integrated_release_validation(self) -> None:
        scaffold_planning_run(self.project_root, "polish-release-pass", ["middleware"])
        run_dir = self.project_root / "runs" / "polish-release-pass"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
            elif item["phase"] == "design":
                item["status"] = "complete"
            elif item["phase"] == "mvp-build":
                item["status"] = "complete"
            elif item["phase"] == "polish":
                item["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "manager-plans" / "polish-app-a.json",
            {
                "schema": "objective-plan.v1",
                "run_id": "polish-release-pass",
                "phase": "polish",
                "objective_id": "app-a",
                "summary": "Polish plan uses the integrated release gate as the canonical finish check.",
                "tasks": [
                    {
                        "task_id": "APP-A-POLISH-001",
                        "capability": "middleware",
                        "assigned_role": "objectives.app-a.middleware-worker",
                        "objective": "Package final polish evidence.",
                        "inputs": [],
                        "expected_outputs": [
                            {
                                "kind": "artifact",
                                "output_id": "app-a-polish-readiness",
                                "path": "runs/polish-release-pass/reports/app-a-polish-readiness.json",
                                "asset_id": None,
                                "description": None,
                                "evidence": None,
                            }
                        ],
                        "done_when": ["final polish evidence is packaged"],
                        "execution_mode": "read_only",
                        "parallel_policy": "serialize",
                        "writes_existing_paths": [],
                        "owned_paths": [],
                        "shared_asset_ids": [],
                        "depends_on": [],
                        "validation": [],
                        "collaboration_rules": [],
                        "working_directory": None,
                        "additional_directories": [],
                        "sandbox_mode": "read-only",
                    }
                ],
                "bundle_plan": [],
                "dependency_notes": [],
                "collaboration_handoffs": [],
            },
        )
        integration_workspace = self.project_root / ".orchestrator-worktrees" / "polish-release-pass" / "integration"
        integration_workspace.mkdir(parents=True, exist_ok=True)
        write_json(
            integration_workspace / "package.json",
            {
                "name": "polish-release-pass",
                "private": True,
                "scripts": {
                    "validate:release-readiness": "node -e \"process.exit(0)\""
                },
            },
        )

        report, _ = generate_phase_report(self.project_root, "polish-release-pass")

        self.assertEqual(report["recommendation"], "advance")
        self.assertEqual(report["release_validation_summary"]["status"], "passed")
        self.assertEqual(
            report["objective_outcomes"],
            [{"objective_id": "app-a", "status": "accepted", "accepted_bundles": []}],
        )

    def test_generate_polish_phase_report_holds_when_integrated_release_validation_fails(self) -> None:
        scaffold_planning_run(self.project_root, "polish-release-fail", ["middleware"])
        run_dir = self.project_root / "runs" / "polish-release-fail"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
            elif item["phase"] == "design":
                item["status"] = "complete"
            elif item["phase"] == "mvp-build":
                item["status"] = "complete"
            elif item["phase"] == "polish":
                item["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "manager-plans" / "polish-app-a.json",
            {
                "schema": "objective-plan.v1",
                "run_id": "polish-release-fail",
                "phase": "polish",
                "objective_id": "app-a",
                "summary": "Polish plan is still incomplete until integrated validation passes.",
                "tasks": [
                    {
                        "task_id": "APP-A-POLISH-001",
                        "capability": "middleware",
                        "assigned_role": "objectives.app-a.middleware-worker",
                        "objective": "Package final polish evidence.",
                        "inputs": [],
                        "expected_outputs": [
                            {
                                "kind": "artifact",
                                "output_id": "app-a-polish-readiness",
                                "path": "runs/polish-release-fail/reports/app-a-polish-readiness.json",
                                "asset_id": None,
                                "description": None,
                                "evidence": None,
                            }
                        ],
                        "done_when": ["final polish evidence is packaged"],
                        "execution_mode": "read_only",
                        "parallel_policy": "serialize",
                        "writes_existing_paths": [],
                        "owned_paths": [],
                        "shared_asset_ids": [],
                        "depends_on": [],
                        "validation": [],
                        "collaboration_rules": [],
                        "working_directory": None,
                        "additional_directories": [],
                        "sandbox_mode": "read-only",
                    }
                ],
                "bundle_plan": [],
                "dependency_notes": [],
                "collaboration_handoffs": [],
            },
        )
        integration_workspace = self.project_root / ".orchestrator-worktrees" / "polish-release-fail" / "integration"
        integration_workspace.mkdir(parents=True, exist_ok=True)
        write_json(
            integration_workspace / "package.json",
            {
                "name": "polish-release-fail",
                "private": True,
                "scripts": {
                    "validate:release-readiness": "node -e \"process.exit(1)\""
                },
            },
        )

        report, _ = generate_phase_report(self.project_root, "polish-release-fail")

        self.assertEqual(report["recommendation"], "hold")
        self.assertEqual(report["release_validation_summary"]["status"], "failed")
        self.assertIn("polish_release_validation", report["unresolved_risks"])

    def test_generate_polish_phase_report_extracts_repairable_failure_diagnostics(self) -> None:
        run_dir = initialize_run(self.project_root, "polish-release-diagnostics", "# Goal\n\n## Objectives\n- Frontend\n- Backend\n- Middleware")
        write_json(
            run_dir / "objective-map.json",
            {
                "schema": "objective-map.v1",
                "run_id": "polish-release-diagnostics",
                "objectives": [
                    {
                        "objective_id": "frontend-obj",
                        "title": "Frontend",
                        "summary": "Frontend",
                        "status": "approved",
                        "capabilities": ["frontend"],
                    },
                    {
                        "objective_id": "backend-obj",
                        "title": "Backend",
                        "summary": "Backend",
                        "status": "approved",
                        "capabilities": ["backend"],
                    },
                    {
                        "objective_id": "middleware-obj",
                        "title": "Middleware",
                        "summary": "Middleware",
                        "status": "approved",
                        "capabilities": ["middleware"],
                    },
                ],
                "dependencies": [],
            },
        )
        suggest_team_proposals(self.project_root, "polish-release-diagnostics")
        generate_role_files(self.project_root, "polish-release-diagnostics", approve=True)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete" if item["phase"] != "polish" else "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        for objective_id in ("frontend-obj", "backend-obj", "middleware-obj"):
            write_json(
                run_dir / "manager-plans" / f"polish-{objective_id}.json",
                {
                    "schema": "objective-plan.v1",
                    "run_id": "polish-release-diagnostics",
                    "phase": "polish",
                    "objective_id": objective_id,
                    "summary": f"Polish plan for {objective_id}",
                    "tasks": [],
                    "bundle_plan": [],
                    "dependency_notes": [],
                    "collaboration_handoffs": [],
                },
            )
        integration_workspace = self.project_root / ".orchestrator-worktrees" / "polish-release-diagnostics" / "integration"
        integration_workspace.mkdir(parents=True, exist_ok=True)
        failure_script = (
            "node -e \"console.log('✖ apps/todo/frontend/test/app.editing.test.js'); "
            "console.log('Error [ERR_MODULE_NOT_FOUND]: Cannot find module \\'/tmp/apps/todo/frontend/src/todos/TodoApp\\' imported from /tmp/apps/todo/frontend/src/index.js'); "
            "console.log('✖ crud-contract + no-op-update: HTTP contract supports list, create, edit, complete, uncomplete, no-op patch, delete, and stable ordering'); "
            "console.log('✖ runtime connectivity serves the frontend, wires the backend origin, and supports CRUD through the integrated runtime'); "
            "console.log('NotFoundError: Not Found at /tmp/apps/todo/runtime/src/frontend-server.js:34:14'); "
            "process.exit(1)\""
        )
        write_json(
            integration_workspace / "package.json",
            {
                "name": "polish-release-diagnostics",
                "private": True,
                "scripts": {"validate:release-readiness": failure_script},
            },
        )

        report, _ = generate_phase_report(self.project_root, "polish-release-diagnostics")

        diagnostics = report["release_validation_summary"]["failure_diagnostics"]
        self.assertEqual(report["recommendation"], "hold")
        self.assertEqual({item["owner_capability"] for item in diagnostics}, {"frontend", "backend", "middleware"})
        self.assertTrue(all(item["repairable"] for item in diagnostics))

    def test_run_phase_attempts_targeted_polish_release_repair(self) -> None:
        run_dir = initialize_run(self.project_root, "polish-release-repair", "# Goal\n\n## Objectives\n- Frontend\n- Backend")
        write_json(
            run_dir / "objective-map.json",
            {
                "schema": "objective-map.v1",
                "run_id": "polish-release-repair",
                "objectives": [
                    {
                        "objective_id": "frontend-obj",
                        "title": "Frontend",
                        "summary": "Frontend",
                        "status": "approved",
                        "capabilities": ["frontend"],
                    },
                    {
                        "objective_id": "backend-obj",
                        "title": "Backend",
                        "summary": "Backend",
                        "status": "approved",
                        "capabilities": ["backend"],
                    },
                ],
                "dependencies": [],
            },
        )
        suggest_team_proposals(self.project_root, "polish-release-repair")
        generate_role_files(self.project_root, "polish-release-repair", approve=True)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete" if item["phase"] != "polish" else "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        for objective_id in ("frontend-obj", "backend-obj"):
            write_json(
                run_dir / "manager-plans" / f"polish-{objective_id}.json",
                {
                    "schema": "objective-plan.v1",
                    "run_id": "polish-release-repair",
                    "phase": "polish",
                    "objective_id": objective_id,
                    "summary": f"Polish plan for {objective_id}",
                    "tasks": [],
                    "bundle_plan": [],
                    "dependency_notes": [],
                    "collaboration_handoffs": [],
                },
            )
        failed_report = {
            "schema": "phase-report.v1",
            "run_id": "polish-release-repair",
            "phase": "polish",
            "summary": "Polish release validation failed.",
            "objective_outcomes": [
                {"objective_id": "frontend-obj", "status": "accepted", "accepted_bundles": []},
                {"objective_id": "backend-obj", "status": "accepted", "accepted_bundles": []},
            ],
            "accepted_bundles": [],
            "unresolved_risks": ["polish_release_validation"],
            "parallelism_summary": {
                "total_tasks_considered": 0,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {"interrupted_activities": 0, "recovered_activities": 0, "abandoned_attempts": 0, "incidents": []},
            "release_validation_summary": {
                "status": "failed",
                "command": "npm run validate:release-readiness",
                "working_directory": "worktrees/integration",
                "report_path": "runs/polish-release-repair/phase-reports/polish-release-validation.json",
                "stdout_path": "runs/polish-release-repair/phase-reports/polish-release-validation.stdout.log",
                "stderr_path": "runs/polish-release-repair/phase-reports/polish-release-validation.stderr.log",
                "summary": "Frontend and backend polish validations failed.",
                "failure_diagnostics": [
                    {
                        "category": "module_resolution",
                        "owner_capability": "frontend",
                        "owner_objective_id": "frontend-obj",
                        "source_test": "apps/todo/frontend/test/app.editing.test.js",
                        "paths": ["apps/todo/frontend/src/index.js"],
                        "excerpt": "Frontend import failed.",
                        "repairable": True,
                    },
                    {
                        "category": "backend_contract",
                        "owner_capability": "backend",
                        "owner_objective_id": "backend-obj",
                        "source_test": "crud-contract + no-op-update",
                        "paths": ["apps/todo/backend/src/server.js"],
                        "excerpt": "Backend contract drifted.",
                        "repairable": True,
                    },
                ],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        passed_report = {
            **failed_report,
            "summary": "Polish release validation passed.",
            "unresolved_risks": [],
            "release_validation_summary": {
                **failed_report["release_validation_summary"],
                "status": "passed",
                "summary": "Integrated release-readiness validation passed.",
                "failure_diagnostics": [],
            },
            "recommendation": "advance",
        }
        with (
            patch(
                "company_orchestrator.management.generate_phase_report",
                side_effect=[
                    (failed_report, run_dir / "phase-reports" / "polish.json"),
                    (passed_report, run_dir / "phase-reports" / "polish.json"),
                ],
            ),
            patch(
                "company_orchestrator.management.plan_objective",
                side_effect=lambda *args, **kwargs: {
                    "objective_id": args[2],
                    "recovery_action": "polish_release_repair",
                },
            ) as plan_objective_mock,
            patch(
                "company_orchestrator.management.schedule_tasks",
                return_value={"phase": "polish", "executed": [], "failures": []},
            ),
            patch(
                "company_orchestrator.management.finalize_objective_bundle",
                return_value={"status": "accepted", "bundle_ids": [], "included_tasks": [], "rejection_reasons": []},
            ),
        ):
            summary = run_phase(self.project_root, "polish-release-repair")

        self.assertEqual(summary["recommendation"], "advance")
        self.assertIn("release_repair", summary)
        self.assertEqual(summary["release_repair"]["status"], "completed")
        self.assertEqual(plan_objective_mock.call_count, 2)
        call_contexts = [call.kwargs["repair_context"] for call in plan_objective_mock.call_args_list]
        self.assertEqual({context["source"] for context in call_contexts}, {"polish_release_validation"})
        self.assertTrue(all(context["compact_prompt"] for context in call_contexts))
        self.assertTrue(all(context["rejection_reasons"] for context in call_contexts))
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        self.assertIn("phase.release_repair_requested", {event["event_type"] for event in events})
        self.assertIn("phase.release_repair_completed", {event["event_type"] for event in events})

    def test_run_phase_accepts_objective_with_no_phase_work(self) -> None:
        scaffold_planning_run(self.project_root, "no-phase-work-run", ["backend"])
        run_dir = self.project_root / "runs" / "no-phase-work-run"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "manager-plans" / "design-app-a.json",
            {
                "schema": "objective-plan.v1",
                "run_id": "no-phase-work-run",
                "phase": "design",
                "objective_id": "app-a",
                "summary": "No design work required for this objective.",
                "tasks": [],
                "bundle_plan": [],
                "dependency_notes": [],
                "collaboration_handoffs": [],
            },
        )

        summary = run_phase(self.project_root, "no-phase-work-run")

        self.assertEqual(summary["recommendation"], "advance")

    def test_run_guidance_marks_fully_completed_final_phase_as_complete(self) -> None:
        scaffold_planning_run(self.project_root, "complete-run-guidance", ["backend"])
        run_dir = self.project_root / "runs" / "complete-run-guidance"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete"
        write_json(run_dir / "phase-plan.json", phase_plan)
        write_json(
            run_dir / "phase-reports" / "polish.json",
            {
                "schema": "phase-report.v1",
                "run_id": "complete-run-guidance",
                "phase": "polish",
                "summary": "Polish complete.",
                "objective_outcomes": [
                    {"objective_id": "app-a", "status": "accepted", "accepted_bundles": []}
                ],
                "accepted_bundles": [],
                "unresolved_risks": [],
                "parallelism_summary": {
                    "total_tasks_considered": 0,
                    "tasks_run_in_parallel": 0,
                    "tasks_serialized_by_policy": 0,
                    "tasks_serialized_by_runtime_conflict": 0,
                    "incidents": [],
                },
                "collaboration_summary": {
                    "total_handoffs": 0,
                    "blocking_handoffs": 0,
                    "satisfied_handoffs": 0,
                    "pending_handoffs": 0,
                    "blocked_handoffs": 0,
                    "handoffs_by_objective": [],
                    "incidents": [],
                },
                "observability_summary": {
                    "total_calls": 0,
                    "completed_calls": 0,
                    "failed_calls": 0,
                    "timed_out_calls": 0,
                    "retry_scheduled_calls": 0,
                    "total_input_tokens": 0,
                    "total_cached_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_prompt_chars": 0,
                    "total_prompt_lines": 0,
                    "average_latency_ms": 0,
                    "max_latency_ms": 0,
                    "average_queue_wait_ms": 0,
                },
                "recovery_summary": {
                    "interrupted_activities": 0,
                    "recovered_activities": 0,
                    "abandoned_attempts": 0,
                    "incidents": [],
                },
                "release_validation_summary": {
                    "status": "passed",
                    "command": "npm run validate:release-readiness",
                    "working_directory": ".orchestrator-worktrees/complete-run-guidance/integration",
                    "report_path": "runs/complete-run-guidance/phase-reports/polish-release-validation.json",
                    "stdout_path": "runs/complete-run-guidance/phase-reports/polish-release-validation.stdout.log",
                    "stderr_path": "runs/complete-run-guidance/phase-reports/polish-release-validation.stderr.log",
                    "summary": "Integrated release-readiness validation passed.",
                },
                "proposed_role_changes": [],
                "recommendation": "advance",
                "human_approved": True,
            },
        )

        guidance = run_guidance(self.project_root, "complete-run-guidance")

        self.assertEqual(guidance["run_status"], "complete")

    def test_run_autonomous_auto_approves_final_phase_and_completes(self) -> None:
        scaffold_smoke_test(self.project_root, "auto-final")
        run_dir = self.project_root / "runs" / "auto-final"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for index, phase_state in enumerate(phase_plan["phases"]):
            if index < 3:
                phase_state["status"] = "complete"
                phase_state["human_approved"] = True
            else:
                phase_state["status"] = "active"
                phase_state["human_approved"] = False
        write_json(run_dir / "phase-plan.json", phase_plan)
        for task_path in sorted((run_dir / "tasks").glob("*.json")):
            task = read_json(task_path)
            task["phase"] = "polish"
            write_json(task_path, task)

        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "returned expected context"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "smoke.context-echo",
                    "path": None,
                    "asset_id": None,
                    "description": "Smoke task echoed the assigned role, objective id, phase, and prompt layers.",
                    "evidence": {
                        "validation_ids": [],
                        "artifact_paths": [],
                    },
                }
            ],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-auto"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            result = run_autonomous(self.project_root, "auto-final", max_concurrency=2)

        self.assertEqual(result["status"], "completed")
        updated_phase_plan = read_json(run_dir / "phase-plan.json")
        self.assertTrue(all(item["status"] == "complete" for item in updated_phase_plan["phases"]))
        autonomy_state = read_json(run_dir / "autonomy.json")
        self.assertEqual(autonomy_state["status"], "completed")
        self.assertTrue(autonomy_state["auto_approve"])
        report = read_json(run_dir / "phase-reports" / "polish.json")
        self.assertTrue(report["human_approved"])

    def test_run_autonomous_stops_on_hold_report(self) -> None:
        run_dir = initialize_run(self.project_root, "auto-hold", "# Goal\n\n## Objectives\n- App A")
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "auto-hold",
            "phase": "discovery",
            "summary": "Discovery needs review.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "pending", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": ["missing-context"],
            "parallelism_summary": {
                "total_tasks_considered": 0,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "discovery.json", phase_report)

        result = run_autonomous(self.project_root, "auto-hold")

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_condition"], "blocked_recovery")
        autonomy_state = read_json(run_dir / "autonomy.json")
        self.assertEqual(autonomy_state["status"], "stopped")
        self.assertEqual(autonomy_state["stop_phase"], "discovery")
        self.assertEqual(
            autonomy_state["stop_reason"],
            "Phase report is on hold and no automated recovery path is available.",
        )

    def test_run_autonomous_respects_stop_before_phase_policy(self) -> None:
        scaffold_smoke_test(self.project_root, "auto-stop-phase")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-auto-stop"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            result = run_autonomous(
                self.project_root,
                "auto-stop-phase",
                max_concurrency=2,
                stop_before_phases=["design"],
            )

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_condition"], "policy_stop_phase")
        self.assertEqual(result["active_phase"], "design")
        autonomy_state = read_json(self.project_root / "runs" / "auto-stop-phase" / "autonomy.json")
        self.assertEqual(autonomy_state["approval_scope"], "all")
        self.assertEqual(autonomy_state["stop_before_phases"], ["design"])
        history = read_json_lines(self.project_root / "runs" / "auto-stop-phase" / "live" / "autonomy-history.jsonl")
        self.assertTrue(any(entry["event_type"] == "autonomy.auto_approved_phase" for entry in history))
        self.assertTrue(any(entry["event_type"] == "autonomy.advanced_phase" for entry in history))
        self.assertTrue(any(entry["event_type"] == "autonomy.stopped" and entry["reason"] == "Autonomy policy is configured to stop before phase design." for entry in history))

    def test_run_autonomous_respects_planning_only_approval_scope(self) -> None:
        run_dir = initialize_run(self.project_root, "auto-scope", "# Goal\n\n## Objectives\n- App A")
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        for phase_state in phase_plan["phases"]:
            if phase_state["phase"] in {"discovery", "design"}:
                phase_state["status"] = "complete"
                phase_state["human_approved"] = True
            elif phase_state["phase"] == "mvp-build":
                phase_state["status"] = "active"
                phase_state["human_approved"] = False
            else:
                phase_state["status"] = "pending"
                phase_state["human_approved"] = False
        write_json(run_dir / "phase-plan.json", phase_plan)
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "auto-scope",
            "phase": "mvp-build",
            "summary": "MVP is ready.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "accepted", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": [],
            "parallelism_summary": {
                "total_tasks_considered": 0,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "advance",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "mvp-build.json", phase_report)

        result = run_autonomous(self.project_root, "auto-scope", approval_scope="planning-only")

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_condition"], "review_gate_policy")
        autonomy_state = read_json(run_dir / "autonomy.json")
        self.assertEqual(autonomy_state["approval_scope"], "planning-only")
        history = read_json_lines(run_dir / "live" / "autonomy-history.jsonl")
        self.assertTrue(any(entry["event_type"] == "autonomy.stopped" for entry in history))

    def test_run_autonomous_records_tuning_decision_from_observability(self) -> None:
        scaffold_smoke_test(self.project_root, "auto-tuned")
        run_dir = self.project_root / "runs" / "auto-tuned"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        for phase_state in phase_plan["phases"]:
            if phase_state["phase"] == "discovery":
                phase_state["status"] = "complete"
                phase_state["human_approved"] = True
            elif phase_state["phase"] == "design":
                phase_state["status"] = "active"
                phase_state["human_approved"] = False
            else:
                phase_state["status"] = "pending"
                phase_state["human_approved"] = False
        write_json(run_dir / "phase-plan.json", phase_plan)
        record_llm_call(
            self.project_root,
            "auto-tuned",
            phase="design",
            activity_id="plan:design:app-a",
            kind="objective_plan",
            attempt=1,
            started_at="2026-03-12T12:00:00Z",
            completed_at="2026-03-12T12:03:00Z",
            latency_ms=180000,
            queue_wait_ms=0,
            prompt_char_count=18000,
            prompt_line_count=300,
            prompt_bytes=18000,
            timed_out=True,
            retry_scheduled=False,
            success=False,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=600,
            error="timeout",
        )
        record_llm_call(
            self.project_root,
            "auto-tuned",
            phase="design",
            activity_id="plan:design:app-b",
            kind="objective_plan",
            attempt=1,
            started_at="2026-03-12T12:04:00Z",
            completed_at="2026-03-12T12:07:00Z",
            latency_ms=180000,
            queue_wait_ms=0,
            prompt_char_count=18500,
            prompt_line_count=305,
            prompt_bytes=18500,
            timed_out=True,
            retry_scheduled=False,
            success=False,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=600,
            error="timeout",
        )

        captured: dict[str, object] = {}

        def fake_plan_phase(project_root: Path, run_id: str, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"run_id": run_id, "phase": "design", "recommendation": "hold"}

        with patch("company_orchestrator.autonomy.plan_phase", side_effect=fake_plan_phase):
            result = run_autonomous(self.project_root, "auto-tuned", max_concurrency=3, max_iterations=1)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(captured["max_concurrency"], 1)
        autonomy_state = read_json(run_dir / "autonomy.json")
        self.assertEqual(autonomy_state["last_tuning_decision"]["effective_max_concurrency"], 1)
        history = read_json_lines(run_dir / "live" / "autonomy-history.jsonl")
        action_entries = [entry for entry in history if entry["event_type"] == "autonomy.action_completed"]
        self.assertEqual(action_entries[-1]["tuning_snapshot"]["effective_max_concurrency"], 1)

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

    def test_run_phase_suggests_replan_command_for_stale_plan_blockers(self) -> None:
        scaffold_smoke_test(self.project_root, "stale-plan")
        task_path = self.project_root / "runs" / "stale-plan" / "tasks" / "APP-A-SMOKE-001.json"
        task = read_json(task_path)
        task["depends_on"] = ["Approved planning inputs for scope, constraints, and success criteria"]
        write_json(task_path, task)

        summary = run_phase(self.project_root, "stale-plan")

        self.assertEqual(
            summary["recommended_next_command"],
            "python3 -m company_orchestrator plan-phase stale-plan --replace --sandbox read-only --max-concurrency 2 --timeout-seconds 600 --watch",
        )
        console = Console(record=True, width=140)
        console.print(build_run_dashboard(self.project_root, "stale-plan"))
        dashboard_output = console.export_text()
        self.assertIn("Next Action", dashboard_output)
        self.assertIn("plan-phase stale-plan --replace", dashboard_output)
        self.assertIn("recoverable", dashboard_output)
        self.assertNotIn("Phase report:", dashboard_output)
        self.assertLess(dashboard_output.index("Activity History"), dashboard_output.index("Next Action"))

    def test_dashboard_distinguishes_controller_status_from_run_status(self) -> None:
        scaffold_smoke_test(self.project_root, "controller-parity")
        ensure_activity(
            self.project_root,
            "controller-parity",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="App A smoke",
            assigned_role="objectives.app-a.frontend-worker",
            status="running",
            current_activity="Working.",
        )
        state = default_autonomy_state("controller-parity")
        state.update(
            {
                "enabled": True,
                "status": "stopped",
                "active_phase": "discovery",
                "started_at": "2026-03-16T00:00:00Z",
                "stop_reason": "Autonomy was detached.",
            }
        )
        write_json(self.project_root / "runs" / "controller-parity" / "autonomy.json", state)

        console = Console(record=True, width=140)
        console.print(build_run_dashboard(self.project_root, "controller-parity"))
        dashboard_output = console.export_text()

        self.assertIn("Autonomy Controller", dashboard_output)
        self.assertIn("Controller status: stopped", dashboard_output)
        self.assertIn("Run status: working", dashboard_output)
        self.assertIn("Execution note: run work is active outside the autonomous controller.", dashboard_output)

    def test_run_guidance_reports_review_and_advance_states(self) -> None:
        scaffold_smoke_test(self.project_root, "reviewable")

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            run_phase(self.project_root, "reviewable")

        guidance = run_guidance(self.project_root, "reviewable")
        self.assertEqual(guidance["run_status"], "ready_for_review")
        self.assertEqual(
            guidance["next_action_command"],
            "python3 -m company_orchestrator approve-phase reviewable discovery",
        )
        self.assertTrue(str(guidance["review_doc_path"]).endswith("runs/reviewable/phase-reports/discovery.md"))

        record_human_approval(self.project_root, "reviewable", "discovery", True)
        guidance = run_guidance(self.project_root, "reviewable")
        self.assertEqual(guidance["run_status"], "ready_to_advance")
        self.assertEqual(
            guidance["next_action_command"],
            "python3 -m company_orchestrator advance-phase reviewable",
        )

    def test_run_guidance_prefers_active_work_over_stale_hold_report(self) -> None:
        scaffold_smoke_test(self.project_root, "active-hold")
        run_dir = self.project_root / "runs" / "active-hold"
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "active-hold",
            "phase": "discovery",
            "summary": "Discovery needs review.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "pending", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": [],
            "parallelism_summary": {
                "total_tasks_considered": 0,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "discovery.json", phase_report)
        ensure_activity(
            self.project_root,
            "active-hold",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="App A smoke",
            assigned_role="objectives.app-a.frontend-worker",
            status="running",
            current_activity="Working.",
        )

        guidance = run_guidance(self.project_root, "active-hold")

        self.assertEqual(guidance["run_status"], "working")
        self.assertIsNone(guidance["next_action_command"])

    def test_run_guidance_recommends_resume_after_partial_execution_hold(self) -> None:
        scaffold_smoke_test(self.project_root, "recoverable-hold")
        run_dir = self.project_root / "runs" / "recoverable-hold"
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "recoverable-hold",
            "phase": "discovery",
            "summary": "Discovery needs recovery.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "pending", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": [],
            "parallelism_summary": {
                "total_tasks_considered": 1,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "discovery.json", phase_report)
        write_json(
            run_dir / "manager-runs" / "phase-discovery.json",
            {
                "phase": "discovery",
                "scheduled": {
                    "phase": "discovery",
                    "executed": [{"task_id": "APP-A-SMOKE-001"}],
                    "skipped_dependency": {"APP-B-SMOKE-001": ["APP-A-SMOKE-001"]},
                    "unresolved_dependencies": {},
                    "blocked_handoffs": {},
                    "failures": [],
                },
            },
        )
        ensure_activity(
            self.project_root,
            "recoverable-hold",
            activity_id="APP-B-SMOKE-001",
            kind="task_execution",
            entity_id="APP-B-SMOKE-001",
            phase="discovery",
            objective_id="app-b",
            display_name="App B smoke",
            assigned_role="objectives.app-b.backend-worker",
            status="waiting_dependencies",
            current_activity="Waiting on dependency completion.",
            dependency_blockers=["APP-A-SMOKE-001"],
        )

        guidance = run_guidance(self.project_root, "recoverable-hold")

        self.assertEqual(guidance["run_status"], "recoverable")
        self.assertEqual(
            guidance["next_action_command"],
            "python3 -m company_orchestrator resume-phase recoverable-hold --sandbox read-only --max-concurrency 2 --timeout-seconds 600 --watch",
        )

    def test_run_guidance_recommends_resume_when_only_queued_work_remains(self) -> None:
        scaffold_smoke_test(self.project_root, "queued-recoverable")
        run_dir = self.project_root / "runs" / "queued-recoverable"
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "queued-recoverable",
            "phase": "discovery",
            "summary": "Discovery has queued work to resume.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "pending", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": [],
            "parallelism_summary": {
                "total_tasks_considered": 1,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "discovery.json", phase_report)
        write_json(
            run_dir / "manager-runs" / "phase-discovery.json",
            {
                "phase": "discovery",
                "scheduled": {
                    "phase": "discovery",
                    "executed": [{"task_id": "APP-A-SMOKE-001"}],
                    "skipped_dependency": {},
                    "unresolved_dependencies": {"APP-B-SMOKE-001": ["APP-A-SMOKE-001"]},
                    "blocked_handoffs": {},
                    "failures": [],
                },
            },
        )
        ensure_activity(
            self.project_root,
            "queued-recoverable",
            activity_id="APP-B-SMOKE-001",
            kind="task_execution",
            entity_id="APP-B-SMOKE-001",
            phase="discovery",
            objective_id="app-b",
            display_name="App B smoke",
            assigned_role="objectives.app-b.backend-worker",
            status="queued",
            current_activity="Queued for retry.",
        )

        guidance = run_guidance(self.project_root, "queued-recoverable")

        self.assertEqual(guidance["run_status"], "recoverable")
        self.assertEqual(
            guidance["next_action_command"],
            "python3 -m company_orchestrator resume-phase queued-recoverable --sandbox read-only --max-concurrency 2 --timeout-seconds 600 --watch",
        )

    def test_run_autonomous_uses_recovery_path_for_hold_report(self) -> None:
        scaffold_smoke_test(self.project_root, "auto-recoverable-hold")
        run_dir = self.project_root / "runs" / "auto-recoverable-hold"
        phase_report = {
            "schema": "phase-report.v1",
            "run_id": "auto-recoverable-hold",
            "phase": "discovery",
            "summary": "Discovery needs recovery.",
            "objective_outcomes": [{"objective_id": "app-a", "status": "pending", "accepted_bundles": []}],
            "accepted_bundles": [],
            "unresolved_risks": [],
            "parallelism_summary": {
                "total_tasks_considered": 1,
                "tasks_run_in_parallel": 0,
                "tasks_serialized_by_policy": 0,
                "tasks_serialized_by_runtime_conflict": 0,
                "incidents": [],
            },
            "collaboration_summary": {
                "total_handoffs": 0,
                "blocking_handoffs": 0,
                "satisfied_handoffs": 0,
                "pending_handoffs": 0,
                "blocked_handoffs": 0,
                "handoffs_by_objective": [],
                "incidents": [],
            },
            "observability_summary": {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "total_input_tokens": 0,
                "total_cached_input_tokens": 0,
                "total_output_tokens": 0,
                "total_prompt_chars": 0,
                "total_prompt_lines": 0,
                "average_latency_ms": 0,
                "max_latency_ms": 0,
                "average_queue_wait_ms": 0,
            },
            "recovery_summary": {
                "interrupted_activities": 0,
                "recovered_activities": 0,
                "abandoned_attempts": 0,
                "incidents": [],
            },
            "proposed_role_changes": [],
            "recommendation": "hold",
            "human_approved": False,
        }
        write_json(run_dir / "phase-reports" / "discovery.json", phase_report)
        write_json(
            run_dir / "manager-runs" / "phase-discovery.json",
            {
                "phase": "discovery",
                "scheduled": {
                    "phase": "discovery",
                    "executed": [{"task_id": "APP-A-SMOKE-001"}],
                    "skipped_dependency": {"APP-B-SMOKE-001": ["APP-A-SMOKE-001"]},
                    "unresolved_dependencies": {},
                    "blocked_handoffs": {},
                    "failures": [],
                },
            },
        )
        ensure_activity(
            self.project_root,
            "auto-recoverable-hold",
            activity_id="APP-B-SMOKE-001",
            kind="task_execution",
            entity_id="APP-B-SMOKE-001",
            phase="discovery",
            objective_id="app-b",
            display_name="App B smoke",
            assigned_role="objectives.app-b.backend-worker",
            status="waiting_dependencies",
            current_activity="Waiting on dependency completion.",
            dependency_blockers=["APP-A-SMOKE-001"],
        )

        captured: dict[str, object] = {}

        def fake_run_phase(project_root: Path, run_id: str, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"run_id": run_id, "phase": "discovery", "recommendation": "hold"}

        with patch("company_orchestrator.autonomy.run_phase", side_effect=fake_run_phase):
            result = run_autonomous(self.project_root, "auto-recoverable-hold", max_iterations=1)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_condition"], "iteration_limit")
        self.assertEqual(captured["max_concurrency"], 3)
        history = read_json_lines(run_dir / "live" / "autonomy-history.jsonl")
        self.assertTrue(any(entry["event_type"] == "autonomy.action_completed" and entry["action"] == "resume-phase" for entry in history))

    def test_cli_result_guidance_adds_review_paths(self) -> None:
        scaffold_smoke_test(self.project_root, "guided")
        payload = augment_result_with_guidance(self.project_root, {"run_id": "guided"})
        self.assertEqual(payload["run_status"], "working")
        self.assertEqual(
            payload["next_action_command"],
            "python3 -m company_orchestrator run-phase guided --sandbox read-only --max-concurrency 2 --timeout-seconds 600 --watch",
        )
        self.assertNotIn("phase_report_path", payload)

    def test_normalize_objective_outline_allows_acceptance_target_edge(self) -> None:
        payload = {
            "schema": "objective-outline.v1",
            "run_id": "outline-acceptance",
            "phase": "design",
            "objective_id": "app-a",
            "summary": "Outline with acceptance review edge.",
            "capability_lanes": [
                {
                    "capability": "frontend",
                    "assigned_manager_role": "wrong-role",
                    "objective": "Plan frontend work.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [
                {
                    "edge_id": "frontend-review-gate",
                    "from_capability": "frontend",
                    "to_capability": "acceptance",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Needs approval.",
                    "deliverables": ["design.md"],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        }
        normalized, _ = normalize_objective_outline(
            self.project_root,
            payload,
            run_id="outline-acceptance",
            phase="design",
            objective={"objective_id": "app-a", "capabilities": ["frontend"]},
        )
        self.assertEqual(
            normalized["capability_lanes"][0]["assigned_manager_role"],
            "objectives.app-a.frontend-manager",
        )
        self.assertEqual(normalized["collaboration_edges"][0]["to_capability"], "acceptance")

    def test_normalize_objective_outline_allows_objective_management_target_edge(self) -> None:
        payload = {
            "schema": "objective-outline.v1",
            "run_id": "outline-objective-management",
            "phase": "discovery",
            "objective_id": "app-a",
            "summary": "Outline with objective-management edge.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "assigned_manager_role": "objectives.app-a.backend-manager",
                    "objective": "Plan backend discovery work.",
                    "inputs": [],
                    "expected_outputs": [],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [
                {
                    "edge_id": "backend-escalation",
                    "from_capability": "backend",
                    "to_capability": "objective-management",
                    "to_role": "objectives.app-a.objective-manager",
                    "handoff_type": "collaboration-request",
                    "reason": "Escalate external alignment through the objective manager.",
                    "deliverables": ["handoff.md"],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        }
        normalized, _ = normalize_objective_outline(
            self.project_root,
            payload,
            run_id="outline-objective-management",
            phase="discovery",
            objective={"objective_id": "app-a", "capabilities": ["backend"]},
        )
        self.assertEqual(normalized["collaboration_edges"][0]["to_capability"], "objective-management")

    def test_normalize_objective_outline_merges_sublanes_into_expected_capability(self) -> None:
        payload = {
            "schema": "objective-outline.v1",
            "run_id": "outline-sub-lanes",
            "phase": "discovery",
            "objective_id": "app-a",
            "summary": "Outline with descriptive frontend sub-lanes.",
            "capability_lanes": [
                {
                    "capability": "frontend-browse-booking-discovery",
                    "assigned_manager_role": "objectives.app-a.frontend-manager",
                    "objective": "Map browse and booking discovery.",
                    "inputs": ["browse"],
                    "expected_outputs": ["browse.md"],
                    "done_when": ["browse bounded"],
                    "depends_on": [],
                    "planning_notes": ["browse note"],
                    "collaboration_rules": ["browse rule"],
                },
                {
                    "capability": "frontend-discovery-synthesis",
                    "assigned_manager_role": "objectives.app-a.frontend-manager",
                    "objective": "Synthesize the frontend discovery package.",
                    "inputs": ["synthesis"],
                    "expected_outputs": ["synthesis.md"],
                    "done_when": ["synthesis bounded"],
                    "depends_on": ["frontend-browse-booking-discovery"],
                    "planning_notes": ["synthesis note"],
                    "collaboration_rules": ["synthesis rule"],
                },
            ],
            "dependency_notes": [],
            "collaboration_edges": [
                {
                    "edge_id": "browse-to-synthesis",
                    "from_capability": "frontend-browse-booking-discovery",
                    "to_capability": "frontend-discovery-synthesis",
                    "to_role": "objectives.app-a.frontend-manager",
                    "handoff_type": "artifact_handoff",
                    "reason": "Synthesis needs browse outputs.",
                    "deliverables": ["browse.md"],
                    "blocking": True,
                    "shared_asset_ids": [],
                },
                {
                    "edge_id": "synthesis-review",
                    "from_capability": "frontend-discovery-synthesis",
                    "to_capability": "acceptance",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Review the merged discovery package.",
                    "deliverables": ["bundle.md"],
                    "blocking": True,
                    "shared_asset_ids": [],
                },
            ],
        }
        normalized, _ = normalize_objective_outline(
            self.project_root,
            payload,
            run_id="outline-sub-lanes",
            phase="discovery",
            objective={"objective_id": "app-a", "capabilities": ["frontend"]},
        )
        self.assertEqual(len(normalized["capability_lanes"]), 1)
        lane = normalized["capability_lanes"][0]
        self.assertEqual(lane["capability"], "frontend")
        self.assertIn("browse", lane["inputs"])
        self.assertIn("synthesis", lane["inputs"])
        self.assertEqual(lane["depends_on"], [])
        self.assertEqual(len(normalized["collaboration_edges"]), 1)
        self.assertEqual(normalized["collaboration_edges"][0]["from_capability"], "frontend")
        self.assertEqual(normalized["collaboration_edges"][0]["to_capability"], "acceptance")

    def test_normalize_objective_outline_rejects_pathless_asset_outputs(self) -> None:
        payload = {
            "schema": "objective-outline.v1",
            "run_id": "outline-pathless-assets",
            "phase": "mvp-build",
            "objective_id": "simple-backend-api-and-persistence-layer-for-storing-todo-items",
            "summary": "Backend outline with invalid logical assets.",
            "capability_lanes": [
                {
                    "capability": "backend",
                    "assigned_manager_role": "objectives.simple-backend-api-and-persistence-layer-for-storing-todo-items.backend-manager",
                    "objective": "Build the backend lane.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "todo-backend-api-service",
                            "path": None,
                            "asset_id": "todo-backend-api-service",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_objective_outline(
                self.project_root,
                payload,
                run_id="outline-pathless-assets",
                phase="mvp-build",
                objective={
                    "objective_id": "simple-backend-api-and-persistence-layer-for-storing-todo-items",
                    "capabilities": ["backend"],
                },
            )

        self.assertIn("Asset outputs must always be backed by concrete file paths", str(ctx.exception))

    def test_normalize_objective_outline_allows_existing_required_output_paths_without_task_write_metadata(self) -> None:
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export {};\n", encoding="utf-8")
        payload = {
            "schema": "objective-outline.v1",
            "run_id": "outline-existing-output",
            "phase": "mvp-build",
            "objective_id": "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items",
            "summary": "Frontend outline with an existing entrypoint output.",
            "capability_lanes": [
                {
                    "capability": "frontend",
                    "assigned_manager_role": "objectives.react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items.frontend-manager",
                    "objective": "Build the frontend lane.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_entrypoint",
                            "path": "apps/todo/frontend/src/index.js",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": [],
                    "depends_on": [],
                    "planning_notes": [],
                    "collaboration_rules": [],
                }
            ],
            "dependency_notes": [],
            "collaboration_edges": [],
        }

        normalized, _ = normalize_objective_outline(
            self.project_root,
            payload,
            run_id="outline-existing-output",
            phase="mvp-build",
            objective={
                "objective_id": "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items",
                "capabilities": ["frontend"],
            },
        )

        self.assertEqual(
            normalized["capability_lanes"][0]["expected_outputs"][0]["path"],
            "apps/todo/frontend/src/index.js",
        )

    def test_schedule_tasks_runs_parallel_read_only_work(self) -> None:
        scaffold_smoke_test(self.project_root, "parallel")
        run_dir = self.project_root / "runs" / "parallel"
        tasks = [read_json(path) for path in sorted((run_dir / "tasks").glob("*.json"))]
        timing: dict[str, tuple[float, float]] = {}

        def side_effect(project_root: Path, run_id: str, task_id: str, *, runtime=None, **_: object):
            start = time.perf_counter()
            time.sleep(0.2)
            summary = write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )
            summary["parallel_execution_requested"] = runtime.parallel_execution_requested if runtime else False
            summary["parallel_execution_granted"] = runtime.parallel_execution_granted if runtime else False
            summary["parallel_fallback_reason"] = runtime.parallel_fallback_reason if runtime else None
            summary["runtime_warnings"] = list(runtime.runtime_warnings) if runtime else []
            write_json(project_root / "runs" / run_id / "executions" / f"{task_id}.json", summary)
            timing[task_id] = (start, time.perf_counter())
            return summary

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "parallel",
                tasks,
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        self.assertEqual(summary["max_concurrency"], 2)
        a_start, a_end = timing["APP-A-SMOKE-001"]
        b_start, b_end = timing["APP-B-SMOKE-001"]
        self.assertLess(max(a_start, b_start), min(a_end, b_end))
        for execution in summary["executed"]:
            self.assertTrue(execution["parallel_execution_granted"])

    def test_schedule_tasks_allows_serialized_writes_from_other_objectives_when_isolated(self) -> None:
        scaffold_smoke_test(self.project_root, "serialize-isolated")
        run_dir = self.project_root / "runs" / "serialize-isolated"
        task_ids = ["APP-A-SMOKE-001", "APP-B-SMOKE-001"]
        task_paths = [
            run_dir / "tasks" / f"{task_id}.json"
            for task_id in task_ids
        ]
        unique_paths = {
            "APP-A-SMOKE-001": "apps/app-a/docs/discovery-a.md",
            "APP-B-SMOKE-001": "apps/app-b/docs/discovery-b.md",
        }
        tasks: list[dict[str, Any]] = []
        for task_id, task_path in zip(task_ids, task_paths):
            task = read_json(task_path)
            task["execution_mode"] = "isolated_write"
            task["parallel_policy"] = "serialize"
            task["owned_paths"] = [unique_paths[task_id]]
            task["expected_outputs"] = [unique_paths[task_id]]
            task["shared_asset_ids"] = []
            write_json(task_path, task)
            tasks.append(task)
        timing: dict[str, tuple[float, float]] = {}

        def side_effect(project_root: Path, run_id: str, task_id: str, *, runtime=None, **_: object):
            start = time.perf_counter()
            time.sleep(0.2)
            summary = write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )
            summary["parallel_execution_requested"] = runtime.parallel_execution_requested if runtime else False
            summary["parallel_execution_granted"] = runtime.parallel_execution_granted if runtime else False
            summary["parallel_fallback_reason"] = runtime.parallel_fallback_reason if runtime else None
            summary["runtime_warnings"] = list(runtime.runtime_warnings) if runtime else []
            write_json(project_root / "runs" / run_id / "executions" / f"{task_id}.json", summary)
            timing[task_id] = (start, time.perf_counter())
            return summary

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "serialize-isolated",
                tasks,
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        self.assertEqual(summary["max_concurrency"], 2)
        a_start, a_end = timing["APP-A-SMOKE-001"]
        b_start, b_end = timing["APP-B-SMOKE-001"]
        self.assertLess(max(a_start, b_start), min(a_end, b_end))
        for execution in summary["executed"]:
            self.assertFalse(execution["parallel_execution_requested"])
            self.assertTrue(execution["parallel_execution_granted"])

    def test_schedule_tasks_serializes_conflicting_parallel_writes_with_warning(self) -> None:
        scaffold_smoke_test(self.project_root, "serialize-warning")
        run_dir = self.project_root / "runs" / "serialize-warning"
        task_ids = ["APP-A-SMOKE-001", "APP-B-SMOKE-001"]
        for task_id in task_ids:
            task_path = run_dir / "tasks" / f"{task_id}.json"
            task = read_json(task_path)
            task["execution_mode"] = "isolated_write"
            task["parallel_policy"] = "allow"
            task["owned_paths"] = ["apps/todo/shared.txt"]
            write_json(task_path, task)
        tasks = [read_json(run_dir / "tasks" / f"{task_id}.json") for task_id in task_ids]

        def side_effect(project_root: Path, run_id: str, task_id: str, *, runtime=None, **_: object):
            summary = write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )
            summary["parallel_execution_requested"] = runtime.parallel_execution_requested if runtime else False
            summary["parallel_execution_granted"] = runtime.parallel_execution_granted if runtime else False
            summary["parallel_fallback_reason"] = runtime.parallel_fallback_reason if runtime else None
            summary["runtime_warnings"] = list(runtime.runtime_warnings) if runtime else []
            write_json(project_root / "runs" / run_id / "executions" / f"{task_id}.json", summary)
            return summary

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "serialize-warning",
                tasks,
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        serialized = [
            item for item in summary["executed"]
            if not item["parallel_execution_granted"]
        ]
        self.assertEqual(len(serialized), 1)
        self.assertIn("Conflicts on owned paths", serialized[0]["parallel_fallback_reason"])
        fallback_task = serialized[0]["task_id"]
        activity = read_json(run_dir / "live" / "activities" / f"{fallback_task}.json")
        self.assertTrue(activity["warnings"])
        phase_report, _ = generate_phase_report(self.project_root, "serialize-warning")
        self.assertEqual(phase_report["parallelism_summary"]["tasks_serialized_by_runtime_conflict"], 1)

    def test_schedule_tasks_waits_for_blocking_handoff_before_running_consumer(self) -> None:
        scaffold_planning_run(self.project_root, "handoff-gating", ["frontend", "backend"])
        run_dir = self.project_root / "runs" / "handoff-gating"
        source_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-gating",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "execution_mode": "isolated_write",
            "parallel_policy": "allow",
            "owned_paths": [],
            "shared_asset_ids": ["app-a:api-contract"],
            "handoff_dependencies": [],
            "objective": "Publish the API contract.",
            "inputs": [],
            "expected_outputs": ["docs/contracts/app-a-api.md"],
            "done_when": ["contract published"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        consumer_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-gating",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "backend",
            "task_id": "APP-A-BACKEND-001",
            "assigned_role": "objectives.app-a.backend-worker",
            "manager_role": "objectives.app-a.backend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "execution_mode": "isolated_write",
            "parallel_policy": "allow",
            "owned_paths": [],
            "shared_asset_ids": ["app-a:api-contract"],
            "handoff_dependencies": ["app-a-frontend-api-contract"],
            "objective": "Consume the published API contract.",
            "inputs": [],
            "expected_outputs": ["backend notes"],
            "done_when": ["contract consumed"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(run_dir / "tasks" / "APP-A-FRONTEND-001.json", source_task)
        write_json(run_dir / "tasks" / "APP-A-BACKEND-001.json", consumer_task)
        write_json(
            run_dir / "collaboration-plans" / "app-a-frontend-api-contract.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "handoff-gating",
                "phase": "discovery",
                "objective_id": "app-a",
                "handoff_id": "app-a-frontend-api-contract",
                "from_capability": "frontend",
                "to_capability": "backend",
                "from_task_id": "APP-A-FRONTEND-001",
                "to_role": "objectives.app-a.backend-manager",
                "handoff_type": "contract",
                "reason": "Backend depends on the frontend contract.",
                "deliverables": ["docs/contracts/app-a-api.md"],
                "blocking": True,
                "shared_asset_ids": ["app-a:api-contract"],
                "to_task_ids": ["APP-A-BACKEND-001"],
                "status": "planned",
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        call_order: list[str] = []

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            call_order.append(task_id)
            artifacts = []
            if task_id == "APP-A-FRONTEND-001":
                artifact_path = project_root / "docs" / "contracts" / "app-a-api.md"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text("contract", encoding="utf-8")
                artifacts.append({"path": "docs/contracts/app-a-api.md", "status": "created"})
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
                artifacts=artifacts,
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "handoff-gating",
                [source_task, consumer_task],
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        self.assertEqual(call_order, ["APP-A-FRONTEND-001", "APP-A-BACKEND-001"])
        self.assertFalse(summary["unresolved_dependencies"])
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        self.assertIn("task.waiting_handoffs", {event["event_type"] for event in events})
        refreshed_handoff = read_json(run_dir / "collaboration-plans" / "app-a-frontend-api-contract.json")
        self.assertEqual(refreshed_handoff["status"], "satisfied")

    def test_evaluate_handoff_accepts_symbolic_deliverables_referenced_in_artifact_content(self) -> None:
        scaffold_planning_run(self.project_root, "symbolic-handoff", ["frontend", "middleware"])
        run_dir = self.project_root / "runs" / "symbolic-handoff"
        workspace_path = (
            self.project_root / ".orchestrator-worktrees" / "symbolic-handoff" / "tasks" / "APP-A-FRONTEND-001"
        )
        handoff_doc = workspace_path / "docs" / "handoffs" / "frontend-middleware.md"
        handoff_doc.parent.mkdir(parents=True, exist_ok=True)
        handoff_doc.write_text(
            "\n".join(
                [
                    "# Frontend Middleware Handoff",
                    "",
                    "Provides `asset.frontend-discovery-brief` and `contract.todo-ui-operation-list` to middleware.",
                ]
            ),
            encoding="utf-8",
        )
        write_json(
            run_dir / "executions" / "APP-A-FRONTEND-001.json",
            {
                "task_id": "APP-A-FRONTEND-001",
                "status": "ready_for_bundle_review",
                "workspace_path": str(workspace_path),
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "symbolic-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Packaged asset.frontend-discovery-brief for middleware review.",
                "artifacts": [{"path": "docs/handoffs/frontend-middleware.md", "status": "created"}],
                "validation_results": [
                    {
                        "id": "deliverables-listed",
                        "status": "passed",
                        "evidence": "The handoff maps asset.frontend-discovery-brief and contract.todo-ui-operation-list.",
                    }
                ],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "symbolic-handoff",
            "phase": "discovery",
            "objective_id": "app-a",
            "handoff_id": "app-a-frontend-to-middleware",
            "from_capability": "frontend",
            "to_capability": "middleware",
            "from_task_id": "APP-A-FRONTEND-001",
            "to_role": "objectives.app-a.middleware-manager",
            "handoff_type": "discovery-brief",
            "reason": "Middleware depends on the frontend discovery package.",
            "deliverables": ["asset.frontend-discovery-brief", "contract.todo-ui-operation-list"],
            "blocking": True,
            "shared_asset_ids": ["app-a:integration"],
            "to_task_ids": ["APP-A-MW-001"],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "symbolic-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])

    def test_evaluate_handoff_accepts_asset_deliverables_declared_by_source_task_contract(self) -> None:
        scaffold_planning_run(self.project_root, "declared-asset-handoff", ["frontend", "middleware"])
        run_dir = self.project_root / "runs" / "declared-asset-handoff"
        workspace_path = (
            self.project_root / ".orchestrator-worktrees" / "declared-asset-handoff" / "tasks" / "APP-A-FRONTEND-001"
        )
        artifact_path = workspace_path / "docs" / "handoffs" / "frontend-middleware.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("# Frontend Middleware Handoff\n", encoding="utf-8")
        write_json(
            run_dir / "tasks" / "APP-A-FRONTEND-001.json",
            {
                "schema": "task-assignment.v1",
                "run_id": "declared-asset-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "capability": "frontend",
                "task_id": "APP-A-FRONTEND-001",
                "assigned_role": "objectives.app-a.frontend-worker",
                "manager_role": "objectives.app-a.frontend-manager",
                "acceptance_role": "objectives.app-a.acceptance-manager",
                "objective": "Prepare frontend discovery handoff.",
                "inputs": [],
                "expected_outputs": [
                    "asset:frontend-mvp-behavior-list",
                    "docs/handoffs/frontend-middleware.md",
                ],
                "done_when": [],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
            },
        )
        write_json(
            run_dir / "executions" / "APP-A-FRONTEND-001.json",
            {
                "task_id": "APP-A-FRONTEND-001",
                "status": "ready_for_bundle_review",
                "workspace_path": str(workspace_path),
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "declared-asset-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Prepared the frontend behavior handoff package.",
                "artifacts": [{"path": "docs/handoffs/frontend-middleware.md", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "declared-asset-handoff",
            "phase": "discovery",
            "objective_id": "app-a",
            "handoff_id": "app-a-frontend-to-middleware",
            "from_capability": "frontend",
            "to_capability": "middleware",
            "from_task_id": "APP-A-FRONTEND-001",
            "to_role": "objectives.app-a.middleware-manager",
            "handoff_type": "discovery-brief",
            "reason": "Middleware depends on the frontend discovery package.",
            "deliverables": ["asset:frontend-mvp-behavior-list"],
            "blocking": True,
            "shared_asset_ids": ["app-a:integration"],
            "to_task_ids": ["APP-A-MW-001"],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "declared-asset-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])

    def test_evaluate_handoff_resolves_artifact_paths_from_task_workspace(self) -> None:
        scaffold_planning_run(self.project_root, "workspace-handoff", ["frontend", "backend"])
        run_dir = self.project_root / "runs" / "workspace-handoff"
        workspace_path = (
            self.project_root / ".orchestrator-worktrees" / "workspace-handoff" / "tasks" / "APP-A-FRONTEND-001"
        )
        artifact_path = workspace_path / "docs" / "contracts" / "app-a-api.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("contract", encoding="utf-8")
        write_json(
            run_dir / "executions" / "APP-A-FRONTEND-001.json",
            {
                "task_id": "APP-A-FRONTEND-001",
                "status": "ready_for_bundle_review",
                "workspace_path": str(workspace_path),
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "workspace-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Published docs/contracts/app-a-api.md from the task workspace.",
                "artifacts": [{"path": "docs/contracts/app-a-api.md", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "workspace-handoff",
            "phase": "discovery",
            "objective_id": "app-a",
            "handoff_id": "app-a-frontend-api-contract",
            "from_capability": "frontend",
            "to_capability": "backend",
            "from_task_id": "APP-A-FRONTEND-001",
            "to_role": "objectives.app-a.backend-manager",
            "handoff_type": "contract",
            "reason": "Backend depends on the frontend contract.",
            "deliverables": ["docs/contracts/app-a-api.md"],
            "blocking": True,
            "shared_asset_ids": ["app-a:api-contract"],
            "to_task_ids": ["APP-A-BACKEND-001"],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "workspace-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])

    def test_evaluate_handoff_accepts_review_bundle_deliverables_from_peer_task_reports(self) -> None:
        scaffold_planning_run(self.project_root, "bundle-handoff", ["frontend"])
        run_dir = self.project_root / "runs" / "bundle-handoff"
        peer_workspace = self.project_root / ".orchestrator-worktrees" / "bundle-handoff" / "tasks" / "APP-A-FRONTEND-001"
        source_workspace = self.project_root / ".orchestrator-worktrees" / "bundle-handoff" / "tasks" / "APP-A-FRONTEND-004"
        first_artifact = peer_workspace / "docs" / "frontend" / "behavior.md"
        second_artifact = peer_workspace / "docs" / "frontend" / "boundary.md"
        source_artifact = source_workspace / "docs" / "frontend" / "bundle.md"
        first_artifact.parent.mkdir(parents=True, exist_ok=True)
        source_artifact.parent.mkdir(parents=True, exist_ok=True)
        first_artifact.write_text("behavior", encoding="utf-8")
        second_artifact.write_text("boundary", encoding="utf-8")
        source_artifact.write_text("bundle", encoding="utf-8")
        write_json(
            run_dir / "executions" / "APP-A-FRONTEND-001.json",
            {
                "task_id": "APP-A-FRONTEND-001",
                "status": "ready_for_bundle_review",
                "workspace_path": str(peer_workspace),
            },
        )
        write_json(
            run_dir / "executions" / "APP-A-FRONTEND-004.json",
            {
                "task_id": "APP-A-FRONTEND-004",
                "status": "ready_for_bundle_review",
                "workspace_path": str(source_workspace),
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "bundle-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced the earlier frontend discovery artifacts.",
                "artifacts": [
                    {"path": "docs/frontend/behavior.md", "status": "created"},
                    {"path": "docs/frontend/boundary.md", "status": "created"},
                ],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-004.json",
            {
                "schema": "completion-report.v1",
                "run_id": "bundle-handoff",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-004",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Assembled the review bundle.",
                "artifacts": [{"path": "docs/frontend/bundle.md", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "bundle-handoff",
            "phase": "discovery",
            "objective_id": "app-a",
            "handoff_id": "app-a-frontend-review-bundle",
            "from_capability": "frontend",
            "to_capability": "objective",
            "from_task_id": "APP-A-FRONTEND-004",
            "to_role": "objectives.app-a.objective-manager",
            "handoff_type": "review_bundle",
            "reason": "Objective review needs the full frontend discovery bundle.",
            "deliverables": [
                "docs/frontend/behavior.md",
                "docs/frontend/boundary.md",
                "docs/frontend/bundle.md",
            ],
            "blocking": True,
            "shared_asset_ids": ["app-a:frontend:handoff"],
            "to_task_ids": [],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "bundle-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])

    def test_evaluate_handoff_accepts_prose_deliverables_backed_by_multiple_upstream_task_artifacts(self) -> None:
        scaffold_planning_run(self.project_root, "prose-handoff", ["backend", "middleware"])
        run_dir = self.project_root / "runs" / "prose-handoff"
        workspaces = {
            "BACK-001": self.project_root / ".orchestrator-worktrees" / "prose-handoff" / "tasks" / "BACK-001",
            "BACK-002": self.project_root / ".orchestrator-worktrees" / "prose-handoff" / "tasks" / "BACK-002",
            "BACK-003": self.project_root / ".orchestrator-worktrees" / "prose-handoff" / "tasks" / "BACK-003",
        }
        artifacts = {
            "BACK-001": "apps/todo/backend/src/server.js",
            "BACK-002": "apps/todo/backend/src/todos/repository.js",
            "BACK-003": "apps/todo/backend/test/contract.test.js",
        }
        for task_id, workspace in workspaces.items():
            artifact_path = workspace / artifacts[task_id]
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(f"{task_id} artifact\n", encoding="utf-8")
            write_json(
                run_dir / "executions" / f"{task_id}.json",
                {
                    "task_id": task_id,
                    "status": "ready_for_bundle_review",
                    "workspace_path": str(workspace),
                },
            )
        write_json(
            run_dir / "reports" / "BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "prose-handoff",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "BACK-001",
                "agent_role": "objectives.app-a.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Implemented the backend CRUD server routes.",
                "artifacts": [{"path": artifacts["BACK-001"], "status": "updated_and_validated"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "BACK-002.json",
            {
                "schema": "completion-report.v1",
                "run_id": "prose-handoff",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "BACK-002",
                "agent_role": "objectives.app-a.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Implemented repository ordering and persistence behavior.",
                "artifacts": [{"path": artifacts["BACK-002"], "status": "updated_and_validated"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            run_dir / "reports" / "BACK-003.json",
            {
                "schema": "completion-report.v1",
                "run_id": "prose-handoff",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "BACK-003",
                "agent_role": "objectives.app-a.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Validated the contract handoff package with passing CRUD contract tests.",
                "artifacts": [{"path": artifacts["BACK-003"], "status": "updated_and_validated"}],
                "validation_results": [
                    {"id": "contract-tests", "status": "passed", "evidence": "CRUD contract tests passed."}
                ],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "prose-handoff",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "handoff_id": "app-a-backend-to-middleware",
            "from_capability": "backend",
            "to_capability": "middleware",
            "from_task_id": "BACK-003",
            "to_role": "objectives.app-a.middleware-manager",
            "handoff_type": "implementation_handoff",
            "reason": "Middleware needs the backend MVP implementation package.",
            "deliverables": [
                "Updated apps/todo/backend/src/server.js with contract-preserving /api/todos and /api/todos/:id behavior",
                "Updated apps/todo/backend/src/todos/repository.js with approved normalization, ordering, and persistence behavior",
                "Updated apps/todo/backend/test/contract.test.js with passing CRUD contract validation output",
            ],
            "blocking": True,
            "shared_asset_ids": ["asset.contract.todo-api.v1"],
            "to_task_ids": ["MID-001"],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "prose-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])
        self.assertEqual(refreshed["satisfied_by_task_ids"], ["BACK-001", "BACK-002", "BACK-003"])

    def test_materialize_executor_response_records_declared_produced_deliverables(self) -> None:
        scaffold_planning_run(self.project_root, "produced-deliverables", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "produced-deliverables",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a discovery handoff package.",
            "inputs": [],
            "expected_outputs": [
                "asset:frontend-mvp-behavior-list",
                "docs/handoffs/frontend-middleware.md",
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Produced the handoff package.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "docs/handoffs/frontend-middleware.md", "status": "created"}],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "discovery",
                "prompt_layers": ["orchestrator/roles/base/company.md"],
                "schema": "task-assignment.v1",
            },
            "collaboration_request": None,
        }

        report, _ = materialize_executor_response(
            self.project_root,
            "produced-deliverables",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(
            report["produced_deliverables"],
            ["asset:frontend-mvp-behavior-list", "docs/handoffs/frontend-middleware.md"],
        )

    def test_materialize_executor_response_normalizes_mixed_produced_deliverables(self) -> None:
        scaffold_planning_run(self.project_root, "normalized-produced-deliverables", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "normalized-produced-deliverables",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a frontend integration contract.",
            "inputs": [],
            "expected_outputs": [
                "asset.contract.frontend-todo-integration.v1 in apps/todo/frontend/frontend-todo-integration.v1.json",
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Produced the frontend integration contract.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "apps/todo/frontend/frontend-todo-integration.v1.json", "status": "created"}],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "design",
                "prompt_layers": ["orchestrator/roles/base/company.md"],
                "schema": "task-assignment.v1",
            },
            "collaboration_request": None,
        }

        report, _ = materialize_executor_response(
            self.project_root,
            "normalized-produced-deliverables",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(
            report["produced_deliverables"],
            [
                "asset.contract.frontend-todo-integration.v1",
                "apps/todo/frontend/frontend-todo-integration.v1.json",
            ],
        )

    def test_materialize_executor_response_rejects_ready_status_with_blocking_issue(self) -> None:
        scaffold_planning_run(self.project_root, "blocking-complete", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "blocking-complete",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-MVP-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Wire the MVP data layer.",
            "inputs": [],
            "expected_outputs": ["apps/todo/frontend/src/data.ts"],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Implemented most of the data layer but contract drift remains.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "apps/todo/frontend/src/data.ts", "status": "created"}],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": ["Blocking: backend contract drift remains unresolved."],
            "legacy_follow_ups": [],
            "collaboration_request": None,
            "context_echo": None,
        }

        with self.assertRaises(ExecutorError):
            materialize_executor_response(
                self.project_root,
                "blocking-complete",
                task,
                parsed_response,
                runtime_warnings=[],
                runtime_recovery=None,
                runtime_observability=None,
            )

        self.assertFalse((self.project_root / "runs" / "blocking-complete" / "reports" / "APP-A-MVP-001.json").exists())

    def test_preview_resolved_inputs_includes_resolved_handoff_packages(self) -> None:
        scaffold_planning_run(self.project_root, "handoff-packages", ["frontend", "backend"])
        run_dir = self.project_root / "runs" / "handoff-packages"
        workspace_path = (
            self.project_root / ".orchestrator-worktrees" / "handoff-packages" / "tasks" / "APP-A-BACKEND-001"
        )
        handoff_payload = {
            "schema": "handoff-payload.v1",
            "summary": "Backend contract ready for frontend consumption.",
            "deliverables": ["docs/handoffs/backend-contract.json"],
            "artifacts": [{"path": "docs/handoffs/backend-contract.json", "status": "created"}],
        }
        write_json(workspace_path / "docs" / "handoffs" / "backend-contract.json", handoff_payload)
        write_json(
            run_dir / "executions" / "APP-A-BACKEND-001.json",
            {
                "task_id": "APP-A-BACKEND-001",
                "status": "ready_for_bundle_review",
                "workspace_path": str(workspace_path),
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-BACKEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "handoff-packages",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "APP-A-BACKEND-001",
                "agent_role": "objectives.app-a.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Backend delivered the contract handoff package.",
                "artifacts": [{"path": "docs/handoffs/backend-contract.json", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff_id = "app-a-backend-to-frontend"
        write_json(
            run_dir / "collaboration-plans" / f"{handoff_id}.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "handoff-packages",
                "phase": "discovery",
                "objective_id": "app-a",
                "handoff_id": handoff_id,
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-A-BACKEND-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "contract",
                "reason": "Frontend needs the backend contract bundle.",
                "deliverables": ["docs/handoffs/backend-contract.json"],
                "blocking": True,
                "shared_asset_ids": ["app-a:contract"],
                "to_task_ids": ["APP-A-FRONTEND-001"],
                "status": "satisfied",
                "satisfied_by_task_ids": ["APP-A-BACKEND-001"],
                "missing_deliverables": [],
                "status_reason": "Source task APP-A-BACKEND-001 satisfied the handoff.",
                "last_checked_at": None,
            },
        )
        consumer_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-packages",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume the backend contract handoff.",
            "inputs": [],
            "expected_outputs": ["docs/frontend-contract-review.md"],
            "done_when": ["contract inputs are resolved"],
            "depends_on": [],
            "handoff_dependencies": [handoff_id],
            "validation": [],
            "collaboration_rules": [],
        }

        resolved = preview_resolved_inputs(self.project_root, "handoff-packages", consumer_task)

        handoff_package = resolved["Resolved Handoff Packages"][handoff_id]
        self.assertEqual(handoff_package["status"], "satisfied")
        self.assertEqual(
            handoff_package["source_report_path"],
            "runs/handoff-packages/reports/APP-A-BACKEND-001.json",
        )
        self.assertEqual(handoff_package["source_summary"], "Backend delivered the contract handoff package.")
        self.assertEqual(len(handoff_package["delivered_payloads"]), 1)
        self.assertEqual(
            handoff_package["delivered_payloads"][0]["preview"]["summary"],
            "Backend contract ready for frontend consumption.",
        )

    def test_compact_resolved_inputs_for_prompt_truncates_nested_payloads(self) -> None:
        payload = {
            "summary": "x" * 600,
            "artifact_previews": [
                {"path": "docs/a.md", "preview": "a" * 500},
                {"path": "docs/b.md", "preview": "b" * 500},
                {"path": "docs/c.md", "preview": "c" * 500},
            ],
            "details": {
                "one": "1" * 500,
                "two": "2" * 500,
                "three": "3" * 500,
                "four": "4" * 500,
                "five": "5" * 500,
                "six": "6" * 500,
                "seven": "7" * 500,
            },
            "items": ["i1", "i2", "i3", "i4", "i5"],
        }

        compacted = compact_resolved_inputs_for_prompt(payload)

        self.assertLessEqual(len(compacted["summary"]), 260)
        self.assertEqual(len(compacted["artifact_previews"]), 2)
        self.assertEqual(compacted["items"][-1], {"truncated_items": 2})
        self.assertEqual(compacted["details"]["truncated_fields"], 1)

    def test_build_dependency_preview_section_limits_preview_volume(self) -> None:
        section = build_dependency_preview_section(
            {
                "Input A": {
                    "artifact_previews": [
                        {"path": "docs/a.md", "preview": "A" * 500},
                        {"path": "docs/b.md", "preview": "B" * 500},
                    ],
                    "task_id": "TASK-A",
                },
                "Input B": {
                    "artifact_previews": [
                        {"path": "docs/c.md", "preview": "C" * 500},
                    ],
                    "task_id": "TASK-B",
                },
            }
        )

        self.assertNotIn("```text", section)
        self.assertEqual(section.count("preview:"), 2)
        self.assertIn("source: `TASK-A`", section)

    def test_normalize_task_execution_metadata_replaces_nonexistent_owned_paths_with_existing_scope_hints(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-repair", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        feature_root = self.project_root / "apps" / "todo" / "frontend" / "src" / "features" / "todo"
        feature_root.mkdir(parents=True, exist_ok=True)
        (feature_root / "existing.ts").write_text("export const ready = true;\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-001",
                    "objective": "Wire the MVP data layer.",
                    "expected_outputs": ["apps/todo/frontend/src/features/todo/new-data.ts"],
                    "owned_paths": ["apps/todo/frontend/src/api/**"],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertNotIn("apps/todo/frontend/src/api/**", owned_paths)
        self.assertIn("apps/todo/frontend/src/features/todo/new-data.ts", owned_paths)
        self.assertTrue(any(path.startswith("apps/todo/frontend") for path in owned_paths))

    def test_normalize_task_execution_metadata_strips_redundant_self_checks_and_assertions_for_discovery_write_tasks(self) -> None:
        payload = {
            "phase": "discovery",
            "tasks": [
                {
                    "task_id": "BACKEND-DISC-001",
                    "objective": "Write the backend discovery brief.",
                    "expected_outputs": [
                        "apps/todo/backend/discovery/backend-discovery-brief.md",
                        {
                            "kind": "assertion",
                            "output_id": "backend.discovery.bundle_asserted",
                            "path": None,
                            "asset_id": None,
                            "description": "Backend discovery bundle exists and includes required sections.",
                            "evidence": {
                                "validation_ids": [
                                    "scope-brief-file-exists",
                                    "scope-brief-has-core-sections",
                                ],
                                "artifact_paths": ["apps/todo/backend/discovery/backend-discovery-brief.md"],
                            },
                        },
                    ],
                    "owned_paths": ["apps/todo/backend/discovery/backend-discovery-brief.md"],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [
                        {
                            "id": "scope-brief-file-exists",
                            "command": "test -f backend/discovery/backend-discovery-brief.md",
                        },
                        {
                            "id": "scope-brief-has-core-sections",
                            "command": "rg -n '^## (MVP Scope|API Surface Assumptions|Explicit Exclusions)$' backend/discovery/backend-discovery-brief.md",
                        },
                    ],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "sandbox_mode": "read-only",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "backend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        self.assertEqual(payload["tasks"][0]["validation"], [])
        self.assertEqual(
            payload["tasks"][0]["expected_outputs"],
            [
                {
                    "kind": "artifact",
                    "output_id": "artifact:apps/todo/backend/discovery/backend-discovery-brief.md",
                    "path": "apps/todo/backend/discovery/backend-discovery-brief.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
        )

    def test_normalize_task_execution_metadata_ignores_slash_delimited_prose_in_expected_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-prose-filter", ["frontend"])
        feature_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        feature_root.mkdir(parents=True, exist_ok=True)
        (feature_root / "api.ts").write_text("export const api = {};\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-002",
                    "objective": "Wire the frontend todo API client.",
                    "expected_outputs": [
                        "apps/todo/frontend/src/api.ts",
                        "Keep the /api/todos and /api/todos/:id flows aligned with the approved contract.",
                    ],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertIn("apps/todo/frontend/src/api.ts", owned_paths)
        self.assertNotIn(
            "Keep the /api/todos and /api/todos/:id flows aligned with the approved contract.",
            owned_paths,
        )
        self.assertFalse(any(path.startswith("Keep the /api/todos") for path in owned_paths))

    def test_normalize_task_execution_metadata_infers_owned_paths_from_concrete_outputs_only(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-infer-filter", ["backend"])
        backend_root = self.project_root / "apps" / "todo" / "backend" / "src"
        backend_root.mkdir(parents=True, exist_ok=True)
        (backend_root / "server.js").write_text("module.exports = {};\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-003",
                    "objective": "Implement the backend todo routes.",
                    "expected_outputs": [
                        "apps/todo/backend/src/server.js",
                        "Backend preserves /api/todos and /api/todos/:id behavior.",
                    ],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "backend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertEqual(owned_paths, ["apps/todo/backend/src/server.js"])

    def test_normalize_task_execution_metadata_extracts_embedded_repo_paths_from_prose_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-embedded-output", ["frontend"])
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src" / "api"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "todosClient.js").write_text("module.exports = {};\n", encoding="utf-8")
        test_root = self.project_root / "apps" / "todo" / "frontend" / "test"
        test_root.mkdir(parents=True, exist_ok=True)
        (test_root / "todosClient.test.js").write_text("module.exports = {};\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-004",
                    "objective": "Wire the todo API client.",
                    "expected_outputs": [
                        "Same-origin CRUD client helpers in apps/todo/frontend/src/api/todosClient.js for list, create, update, and delete against /api/todos",
                        "Contract-focused client tests in apps/todo/frontend/test/todosClient.test.js covering approved write fields and response shaping",
                    ],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertEqual(
            owned_paths,
            [
                "apps/todo/frontend/src/api/todosClient.js",
                "apps/todo/frontend/test/todosClient.test.js",
            ],
        )

    def test_normalize_task_execution_metadata_drops_broad_owned_globs_when_outputs_name_concrete_files(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-broad-prune", ["frontend"])
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src" / "api"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "todosClient.js").write_text("module.exports = {};\n", encoding="utf-8")
        test_root = self.project_root / "apps" / "todo" / "frontend" / "test"
        test_root.mkdir(parents=True, exist_ok=True)
        (test_root / "todosClient.test.js").write_text("module.exports = {};\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-005",
                    "objective": "Wire the todo API client.",
                    "expected_outputs": [
                        "Same-origin CRUD client helpers in apps/todo/frontend/src/api/todosClient.js for list, create, update, and delete against /api/todos",
                        "Contract-focused client tests in apps/todo/frontend/test/todosClient.test.js covering approved write fields and response shaping",
                    ],
                    "owned_paths": [
                        "apps/todo/frontend/**",
                        "apps/todo/frontend/src/**",
                        "apps/todo/frontend/test/**",
                    ],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertNotIn("apps/todo/frontend/**", owned_paths)
        self.assertNotIn("apps/todo/frontend/src/**", owned_paths)
        self.assertNotIn("apps/todo/frontend/test/**", owned_paths)
        self.assertEqual(
            owned_paths,
            [
                "apps/todo/frontend/src/api/todosClient.js",
                "apps/todo/frontend/test/todosClient.test.js",
            ],
        )

    def test_normalize_task_execution_metadata_ignores_http_route_fragments_in_output_prose(self) -> None:
        scaffold_planning_run(self.project_root, "owned-path-route-fragments", ["frontend"])
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-006",
                    "objective": "Wire the CRUD contract.",
                    "expected_outputs": [
                        "Client request helpers that only send title on create and title and/or completed on update for /api/todos and /api/todos/:id.",
                        "Updated apps/todo/frontend/src/index.js to consume backend responses as source of truth.",
                    ],
                    "owned_paths": ["apps/todo/frontend/**"],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        owned_paths = payload["tasks"][0]["owned_paths"]
        self.assertEqual(owned_paths, ["apps/todo/frontend/src/index.js"])
        self.assertFalse(any(path.startswith("api/todos") for path in owned_paths))

    def test_normalize_task_execution_metadata_derives_owned_paths_from_created_outputs_and_existing_writes(self) -> None:
        scaffold_planning_run(self.project_root, "explicit-write-intent", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const existing = true;\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-EXPLICIT-001",
                    "objective": "Create a new data module and update the existing entrypoint.",
                    "expected_outputs": ["apps/todo/frontend/src/features/todos/newData.ts"],
                    "writes_existing_paths": ["apps/todo/frontend/src/index.js"],
                    "owned_paths": ["apps/todo/frontend/**"],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        task = payload["tasks"][0]
        self.assertEqual(
            task["owned_paths"],
            [
                "apps/todo/frontend/src/features/todos/newData.ts",
                "apps/todo/frontend/src/index.js",
            ],
        )
        self.assertEqual(task["writes_existing_paths"], ["apps/todo/frontend/src/index.js"])

    def test_normalize_task_execution_metadata_rejects_missing_writes_existing_paths(self) -> None:
        scaffold_planning_run(self.project_root, "missing-existing-write", ["frontend"])
        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-EXPLICIT-002",
                    "objective": "Update a missing file.",
                    "expected_outputs": [],
                    "writes_existing_paths": ["apps/todo/frontend/src/missing.js"],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_task_execution_metadata(
                self.project_root,
                "app-a",
                "frontend",
                payload,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("writes_existing_paths referenced a missing file", str(ctx.exception))

    def test_normalize_task_execution_metadata_rejects_existing_required_output_without_writes_existing_path(self) -> None:
        scaffold_planning_run(self.project_root, "missing-existing-required-output-write", ["frontend"])
        existing_output = self.project_root / "apps" / "todo" / "frontend" / "src" / "index.js"
        existing_output.parent.mkdir(parents=True, exist_ok=True)
        existing_output.write_text("module.exports = {};\n", encoding="utf-8")
        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-EXPLICIT-EXISTING-001",
                    "objective": "Update the existing frontend entrypoint.",
                    "expected_outputs": ["apps/todo/frontend/src/index.js"],
                    "writes_existing_paths": [],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_task_execution_metadata(
                self.project_root,
                "app-a",
                "frontend",
                payload,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("must list already-existing required output files in writes_existing_paths", str(ctx.exception))

    def test_effective_sandbox_mode_elevates_write_tasks_from_read_only_default(self) -> None:
        task = {
            "execution_mode": "isolated_write",
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "backend_discovery_brief",
                    "path": "apps/todo/backend/discovery/backend-discovery-brief.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "writes_existing_paths": [],
            "owned_paths": ["apps/todo/backend/discovery/backend-discovery-brief.md"],
            "sandbox_mode": "read-only",
        }

        self.assertEqual(effective_sandbox_mode(task, "read-only"), "workspace-write")

    def test_normalize_task_execution_metadata_allows_writes_existing_paths_from_run_integration_workspace(self) -> None:
        run_id = "integration-existing-write"
        scaffold_planning_run(self.project_root, run_id, ["backend"])
        integration_contract = (
            self.project_root
            / ".orchestrator-worktrees"
            / run_id
            / "integration"
            / "apps"
            / "todo"
            / "backend"
            / "design"
            / "todo-api-contract.md"
        )
        integration_contract.parent.mkdir(parents=True, exist_ok=True)
        integration_contract.write_text("# API Contract\n", encoding="utf-8")
        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-BACKEND-001",
                    "objective": "Implement backend persistence against the accepted design contract.",
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "backend_sqlite_store",
                            "path": "apps/todo/backend/src/persistence/sqlite-store.ts",
                            "asset_id": "asset.backend.sqlite-store",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": ["apps/todo/backend/design/todo-api-contract.md"],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": ["apps/todo/backend/design/todo-api-contract.md"],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "backend",
            payload,
            run_id=run_id,
            default_sandbox_mode="workspace-write",
        )

        self.assertEqual(payload["tasks"][0]["writes_existing_paths"], ["apps/todo/backend/design/todo-api-contract.md"])
        self.assertEqual(
            payload["tasks"][0]["owned_paths"],
            [
                "apps/todo/backend/src/persistence/sqlite-store.ts",
                "apps/todo/backend/design/todo-api-contract.md",
            ],
        )

    def test_normalize_task_execution_metadata_allows_writes_existing_paths_from_prior_task_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "planned-existing-write", ["frontend"])
        payload = {
            "phase": "discovery",
            "tasks": [
                {
                    "task_id": "APP-A-DISC-001",
                    "objective": "Draft the discovery brief.",
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_discovery_brief_initial",
                            "path": "apps/todo/reports/discovery/app-a/frontend-discovery-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": [],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                },
                {
                    "task_id": "APP-A-DISC-002",
                    "objective": "Refine the discovery brief from the prior task output.",
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_discovery_brief",
                            "path": "apps/todo/reports/discovery/app-a/frontend-discovery-brief.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": ["apps/todo/reports/discovery/app-a/frontend-discovery-brief.md"],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": ["APP-A-DISC-001"],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": ["Output of APP-A-DISC-001"],
                    "execution_mode": "isolated_write",
                },
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        self.assertEqual(payload["tasks"][1]["writes_existing_paths"], ["apps/todo/reports/discovery/app-a/frontend-discovery-brief.md"])
        self.assertEqual(payload["tasks"][1]["owned_paths"], ["apps/todo/reports/discovery/app-a/frontend-discovery-brief.md"])

    def test_normalize_task_execution_metadata_allows_writes_existing_paths_from_same_plan_outputs_out_of_order(self) -> None:
        scaffold_planning_run(self.project_root, "planned-existing-write-out-of-order", ["frontend"])
        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-002",
                    "objective": "Extend the generated app shell with interactions.",
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_interactions",
                            "path": "apps/todo/frontend/src/components/TodoList.jsx",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": ["apps/todo/frontend/src/App.tsx"],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": ["APP-A-MVP-001"],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": ["Output of APP-A-MVP-001"],
                    "execution_mode": "isolated_write",
                },
                {
                    "task_id": "APP-A-MVP-001",
                    "objective": "Create the app shell file.",
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_app_shell",
                            "path": "apps/todo/frontend/src/App.tsx",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": [],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                },
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "frontend",
            payload,
            default_sandbox_mode="workspace-write",
        )

        self.assertEqual(payload["tasks"][0]["writes_existing_paths"], ["apps/todo/frontend/src/App.tsx"])
        self.assertCountEqual(
            payload["tasks"][0]["owned_paths"],
            [
                "apps/todo/frontend/src/components/TodoList.jsx",
                "apps/todo/frontend/src/App.tsx",
            ],
        )

    def test_normalize_task_execution_metadata_rejects_directory_root_outputs_for_mvp_build_writes(self) -> None:
        scaffold_planning_run(self.project_root, "broad-output-root", ["frontend"])
        todo_root = self.project_root / "apps" / "todo"
        todo_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-EXPLICIT-003",
                    "objective": "Implement the frontend by claiming the whole app root.",
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "frontend_mvp_source",
                            "path": "apps/todo",
                            "asset_id": "asset.frontend.todo_mvp_source",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "writes_existing_paths": [],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_task_execution_metadata(
                self.project_root,
                "app-a",
                "frontend",
                payload,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("must declare concrete file outputs", str(ctx.exception))

    def test_normalize_task_execution_metadata_rejects_outputs_outside_capability_workspace(self) -> None:
        scaffold_planning_run(self.project_root, "workspace-boundary", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const existing = true;\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-EXPLICIT-004",
                    "objective": "Write into an invented sibling source tree.",
                    "expected_outputs": ["apps/todo/src/App.jsx"],
                    "writes_existing_paths": [],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_task_execution_metadata(
                self.project_root,
                "app-a",
                "frontend",
                payload,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("must keep implementation paths inside the capability workspace", str(ctx.exception))

    def test_normalize_task_execution_metadata_rejects_middleware_ownership_of_frontend_or_backend_paths(self) -> None:
        scaffold_planning_run(self.project_root, "middleware-owned-path-guard", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        runtime_root = self.project_root / "apps" / "todo" / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        frontend_root = self.project_root / "apps" / "todo" / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")
        backend_root = self.project_root / "apps" / "todo" / "backend" / "src"
        backend_root.mkdir(parents=True, exist_ok=True)
        (backend_root / "server.js").write_text("module.exports = {};\n", encoding="utf-8")

        for forbidden_owned_path in [
            "apps/todo/frontend/src/index.js",
            "apps/todo/backend/src/server.js",
        ]:
            payload = {
                "phase": "mvp-build",
                "tasks": [
                    {
                    "task_id": "APP-A-MVP-INT-001",
                    "objective": "Wire the integrated runtime.",
                    "expected_outputs": ["apps/todo/runtime/integration-checklist.md"],
                    "writes_existing_paths": [forbidden_owned_path],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                        "collaboration_rules": [],
                        "inputs": [],
                        "execution_mode": "isolated_write",
                    }
                ],
            }

            with self.subTest(forbidden_owned_path=forbidden_owned_path):
                with self.assertRaises(ExecutorError) as ctx:
                    normalize_task_execution_metadata(
                        self.project_root,
                        "app-a",
                        "middleware",
                        payload,
                        default_sandbox_mode="workspace-write",
                    )

                self.assertTrue(
                    "may not own frontend or backend paths" in str(ctx.exception)
                    or "must keep implementation paths inside the capability workspace" in str(ctx.exception)
                )

    def test_normalize_task_execution_metadata_allows_middleware_owned_shared_root_manifest(self) -> None:
        scaffold_planning_run(self.project_root, "middleware-shared-root-manifest", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        app_root = self.project_root / "apps" / "todo"
        app_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / "package.json").write_text('{"name":"todo"}\n', encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-INT-ROOT-001",
                    "objective": "Update the shared workspace manifest for integration-owned runtime wiring.",
                    "expected_outputs": [
                        "apps/todo/orchestrator/roles/objectives/app-a/mvp-build/middleware-build-handoff.md"
                    ],
                    "writes_existing_paths": ["package.json"],
                    "owned_paths": [],
                    "shared_asset_ids": ["todo-workspace-manifest"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        normalize_task_execution_metadata(
            self.project_root,
            "app-a",
            "middleware",
            payload,
            default_sandbox_mode="workspace-write",
        )
        task = payload["tasks"][0]
        self.assertEqual(task["writes_existing_paths"], ["package.json"])
        self.assertIn("package.json", task["owned_paths"])

    def test_normalize_task_execution_metadata_rejects_frontend_edit_of_middleware_owned_shared_root_manifest(self) -> None:
        scaffold_planning_run(self.project_root, "frontend-shared-root-manifest", ["frontend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        app_root = self.project_root / "apps" / "todo"
        app_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / "package.json").write_text('{"name":"todo"}\n', encoding="utf-8")
        frontend_root = app_root / "frontend" / "src"
        frontend_root.mkdir(parents=True, exist_ok=True)
        (frontend_root / "index.js").write_text("export const ready = true;\n", encoding="utf-8")

        payload = {
            "phase": "mvp-build",
            "tasks": [
                {
                    "task_id": "APP-A-MVP-FE-ROOT-001",
                    "objective": "Incorrectly edit the shared app manifest from the frontend lane.",
                    "expected_outputs": [
                        "apps/todo/frontend/src/App.tsx"
                    ],
                    "writes_existing_paths": ["package.json"],
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "inputs": [],
                    "execution_mode": "isolated_write",
                }
            ],
        }

        with self.assertRaises(ExecutorError) as ctx:
            normalize_task_execution_metadata(
                self.project_root,
                "app-a",
                "frontend",
                payload,
                default_sandbox_mode="workspace-write",
            )

        self.assertIn("must keep implementation paths inside the capability workspace", str(ctx.exception))

    def test_schedule_tasks_retries_existing_blocked_report(self) -> None:
        scaffold_smoke_test(self.project_root, "retry-blocked")
        run_dir = self.project_root / "runs" / "retry-blocked"
        task = read_json(run_dir / "tasks" / "APP-A-SMOKE-001.json")
        write_managed_report(
            self.project_root,
            "retry-blocked",
            "APP-A-SMOKE-001",
            status="blocked",
            summary="Blocked on an earlier bad input resolution.",
        )
        calls: list[str] = []

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            calls.append(task_id)
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} rerun successfully.",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "retry-blocked",
                [task],
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=1,
            )

        self.assertEqual(calls, ["APP-A-SMOKE-001"])
        self.assertFalse(summary["skipped_existing"])
        self.assertEqual(summary["executed"][0]["task_id"], "APP-A-SMOKE-001")
        self.assertEqual(summary["executed"][0]["status"], "ready_for_bundle_review")

    def test_waiting_dependency_events_are_emitted_once_until_blockers_change(self) -> None:
        scaffold_smoke_test(self.project_root, "dependency-noise")
        run_dir = self.project_root / "runs" / "dependency-noise"
        app_a = read_json(run_dir / "tasks" / "APP-A-SMOKE-001.json")
        app_b = read_json(run_dir / "tasks" / "APP-B-SMOKE-001.json")
        app_b["depends_on"] = ["APP-A-SMOKE-001"]
        write_json(run_dir / "tasks" / "APP-B-SMOKE-001.json", app_b)

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            if task_id == "APP-A-SMOKE-001":
                time.sleep(0.25)
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "dependency-noise",
                [app_a, app_b],
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        self.assertFalse(summary["failures"])
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        waiting_events = [
            event
            for event in events
            if event["event_type"] == "task.waiting_dependencies" and event["activity_id"] == "APP-B-SMOKE-001"
        ]
        resolved_events = [
            event
            for event in events
            if event["event_type"] == "task.dependencies_resolved" and event["activity_id"] == "APP-B-SMOKE-001"
        ]
        self.assertEqual(len(waiting_events), 1)
        self.assertEqual(len(resolved_events), 1)

    def test_waiting_handoff_events_are_emitted_once_until_handoff_resolves(self) -> None:
        scaffold_planning_run(self.project_root, "handoff-noise", ["frontend", "backend"])
        run_dir = self.project_root / "runs" / "handoff-noise"
        source_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-noise",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "execution_mode": "isolated_write",
            "parallel_policy": "allow",
            "owned_paths": [],
            "shared_asset_ids": ["app-a:api-contract"],
            "handoff_dependencies": [],
            "objective": "Publish the API contract.",
            "inputs": [],
            "expected_outputs": ["docs/contracts/app-a-api.md"],
            "done_when": ["contract published"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        consumer_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-noise",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "backend",
            "task_id": "APP-A-BACKEND-001",
            "assigned_role": "objectives.app-a.backend-worker",
            "manager_role": "objectives.app-a.backend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "shared_asset_ids": ["app-a:api-contract"],
            "handoff_dependencies": ["app-a-frontend-api-contract"],
            "objective": "Consume the published API contract.",
            "inputs": [],
            "expected_outputs": ["backend notes"],
            "done_when": ["contract consumed"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(run_dir / "tasks" / "APP-A-FRONTEND-001.json", source_task)
        write_json(run_dir / "tasks" / "APP-A-BACKEND-001.json", consumer_task)
        write_json(
            run_dir / "collaboration-plans" / "app-a-frontend-api-contract.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "handoff-noise",
                "phase": "discovery",
                "objective_id": "app-a",
                "handoff_id": "app-a-frontend-api-contract",
                "from_capability": "frontend",
                "to_capability": "backend",
                "from_task_id": "APP-A-FRONTEND-001",
                "to_role": "objectives.app-a.backend-manager",
                "handoff_type": "contract",
                "reason": "Backend depends on the frontend contract.",
                "deliverables": ["docs/contracts/app-a-api.md"],
                "blocking": True,
                "shared_asset_ids": ["app-a:api-contract"],
                "to_task_ids": ["APP-A-BACKEND-001"],
                "status": "planned",
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            if task_id == "APP-A-FRONTEND-001":
                time.sleep(0.25)
                artifact_path = project_root / "docs" / "contracts" / "app-a-api.md"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text("contract", encoding="utf-8")
                artifacts = [{"path": "docs/contracts/app-a-api.md", "status": "created"}]
            else:
                artifacts = []
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
                artifacts=artifacts,
            )

        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            schedule_tasks(
                self.project_root,
                "handoff-noise",
                [source_task, consumer_task],
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=30,
                max_concurrency=2,
            )

        events = read_json_lines(run_dir / "live" / "events.jsonl")
        waiting_events = [
            event
            for event in events
            if event["event_type"] == "task.waiting_handoffs" and event["activity_id"] == "APP-A-BACKEND-001"
        ]
        resolved_events = [
            event
            for event in events
            if event["event_type"] == "task.handoffs_resolved" and event["activity_id"] == "APP-A-BACKEND-001"
        ]
        self.assertEqual(len(waiting_events), 1)
        self.assertEqual(len(resolved_events), 1)

    def test_worktree_isolation_and_landing_merge_accepted_changes(self) -> None:
        init_git_repo(self.project_root)
        run_dir = initialize_run(self.project_root, "git-run", "# Goal\n\n## Objectives\n- App A")
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "git-run",
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
        suggest_team_proposals(self.project_root, "git-run")
        generate_role_files(self.project_root, "git-run", approve=True)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "git-run",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-MVP-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Write isolated file",
            "inputs": [],
            "expected_outputs": ["apps/todo/sample.txt"],
            "done_when": ["file exists"],
            "execution_mode": "isolated_write",
            "parallel_policy": "allow",
            "owned_paths": ["apps/todo/sample.txt"],
            "shared_asset_ids": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "working_directory": None,
            "additional_directories": [],
            "sandbox_mode": "workspace-write",
        }
        write_json(run_dir / "tasks" / "APP-A-MVP-001.json", task)
        write_json(
            run_dir / "reports" / "APP-A-MVP-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "git-run",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "APP-A-MVP-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "done",
                "artifacts": [{"path": "apps/todo/sample.txt", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        bundle = {
            "schema": "review-bundle.v1",
            "run_id": "git-run",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "bundle_id": "app-a-mvp-bundle",
            "assembled_by": "objectives.app-a.objective-manager",
            "reviewed_by": "objectives.app-a.acceptance-manager",
            "included_tasks": ["APP-A-MVP-001"],
            "status": "accepted",
            "required_checks": [],
            "rejection_reasons": [],
        }
        write_json(run_dir / "bundles" / "app-a-mvp-bundle.json", bundle)

        integration = ensure_run_integration_workspace(self.project_root, "git-run")
        workspace = ensure_task_workspace(self.project_root, "git-run", "APP-A-MVP-001")
        self.assertTrue(integration.workspace_path.exists())
        self.assertTrue(workspace.workspace_path.exists())
        (workspace.workspace_path / "apps" / "todo").mkdir(parents=True, exist_ok=True)
        (workspace.workspace_path / "apps" / "todo" / "sample.txt").write_text("from task\n", encoding="utf-8")
        commit_result = commit_task_workspace(workspace, "APP-A-MVP-001")
        self.assertTrue(commit_result["committed"])

        landing = land_accepted_bundle(self.project_root, "git-run", bundle)
        self.assertEqual(landing["status"], "accepted")
        merged_text = (integration.workspace_path / "apps" / "todo" / "sample.txt").read_text(encoding="utf-8")
        self.assertEqual(merged_text, "from task\n")

    def test_ensure_run_integration_workspace_tolerates_branch_creation_race(self) -> None:
        init_git_repo(self.project_root)
        repo_root = self.project_root.resolve()
        workspace_path = self.project_root / ".orchestrator-worktrees" / "race-run" / "integration"
        branch_name = "codex/run-race-run"
        show_ref_calls = {"count": 0}

        def fake_git(cwd: Path, args: list[str], *, check: bool = True):
            if args == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(["git", *args], 0, stdout=f"{repo_root}\n", stderr="")
            if args[:3] == ["show-ref", "--verify", "--quiet"]:
                show_ref_calls["count"] += 1
                return subprocess.CompletedProcess(
                    ["git", *args],
                    0 if show_ref_calls["count"] > 1 else 1,
                    stdout="",
                    stderr="",
                )
            if args == ["branch", branch_name, "HEAD"]:
                return subprocess.CompletedProcess(
                    ["git", *args],
                    128,
                    stdout="",
                    stderr=f"fatal: a branch named '{branch_name}' already exists",
                )
            if args == ["worktree", "add", str(workspace_path), branch_name]:
                workspace_path.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected git call: {args}")

        with patch("company_orchestrator.worktree_manager.git", side_effect=fake_git):
            workspace = ensure_run_integration_workspace(self.project_root, "race-run")

        self.assertEqual(workspace.branch_name, branch_name)
        self.assertEqual(workspace.workspace_path, workspace_path)
        self.assertTrue(workspace.workspace_path.exists())

    def test_reconcile_for_command_ignores_completed_bundle_landing_incidents(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "reconcile-landed-bundle", ["frontend"])
        run_dir = self.project_root / "runs" / "reconcile-landed-bundle"
        write_json(
            run_dir / "tasks" / "WRITE-001.json",
            {
                "schema": "task-assignment.v1",
                "run_id": "reconcile-landed-bundle",
                "phase": "discovery",
                "objective_id": "app-a",
                "capability": "frontend",
                "working_directory": None,
                "sandbox_mode": "workspace-write",
                "additional_directories": [],
                "execution_mode": "isolated_write",
                "parallel_policy": "serialize",
                "owned_paths": ["docs/landed.md"],
                "writes_existing_paths": [],
                "shared_asset_ids": [],
                "task_id": "WRITE-001",
                "assigned_role": "objectives.app-a.frontend-worker",
                "manager_role": "objectives.app-a.frontend-manager",
                "acceptance_role": "objectives.app-a.acceptance-manager",
                "objective": "Write and land a simple artifact.",
                "inputs": [],
                "expected_outputs": ["docs/landed.md"],
                "done_when": ["artifact exists"],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
            },
        )
        write_json(
            run_dir / "reports" / "WRITE-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "reconcile-landed-bundle",
                "phase": "discovery",
                "objective_id": "app-a",
                "task_id": "WRITE-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Artifact ready.",
                "artifacts": [{"path": "docs/landed.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "docs.landed",
                        "path": "docs/landed.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        bundle = {
            "schema": "review-bundle.v1",
            "run_id": "reconcile-landed-bundle",
            "phase": "discovery",
            "objective_id": "app-a",
            "bundle_id": "landed-bundle",
            "assembled_by": "objectives.app-a.frontend-manager",
            "reviewed_by": "objectives.app-a.acceptance-manager",
            "included_tasks": ["WRITE-001"],
            "status": "accepted",
            "required_checks": [],
            "rejection_reasons": [],
        }
        write_json(run_dir / "bundles" / "landed-bundle.json", bundle)

        integration = ensure_run_integration_workspace(self.project_root, "reconcile-landed-bundle")
        workspace = ensure_task_workspace(self.project_root, "reconcile-landed-bundle", "WRITE-001")
        (workspace.workspace_path / "docs").mkdir(parents=True, exist_ok=True)
        (workspace.workspace_path / "docs" / "landed.md").write_text("landed\n", encoding="utf-8")
        commit_result = commit_task_workspace(workspace, "WRITE-001")
        self.assertTrue(commit_result["committed"])

        landing = land_accepted_bundle(self.project_root, "reconcile-landed-bundle", bundle)
        self.assertEqual(landing["status"], "accepted")
        self.assertEqual((integration.workspace_path / "docs" / "landed.md").read_text(encoding="utf-8"), "landed\n")

        # Simulate the interrupted post-landing bookkeeping case: the landing commit exists,
        # but landing_results were never written back into the bundle file.
        write_json(run_dir / "bundles" / "landed-bundle.json", bundle)

        summary = reconcile_for_command(self.project_root, "reconcile-landed-bundle", apply=True)
        self.assertEqual(summary["blocked"], [])

    def test_plan_objective_materializes_tasks_from_manager_plan(self) -> None:
        scaffold_planning_run(self.project_root, "planned", ["frontend"])
        capability_plan = {
            "schema": "capability-plan.v1",
            "run_id": "planned",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "summary": "Frontend discovery plan for app-a",
            "tasks": [
                {
                    "task_id": "APP-A-DISC-001",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Identify the discovery boundary for app-a.",
                    "inputs": ["runs/planned/goal.md"],
                    "expected_outputs": ["boundary notes"],
                    "done_when": ["boundary is described"],
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": [],
                    "validation": [{"id": "manager-check", "command": "true"}],
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
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "depends_on": ["APP-A-DISC-001"],
                    "validation": [{"id": "manager-check", "command": "true"}],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only"
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "discovery-bundle-1",
                    "task_ids": ["APP-A-DISC-001", "APP-A-DISC-002"],
                    "summary": "Complete app-a discovery package"
                }
            ],
            "dependency_notes": ["Task 2 depends on task 1"],
            "collaboration_handoffs": []
        }
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"frontend-thread-123"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses) as planner:
            summary = plan_objective(self.project_root, "planned", "app-a")
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(summary["planning_mode"], "single_capability_fast_path")
        self.assertEqual(summary["task_ids"], ["APP-A-DISC-001", "APP-A-DISC-002"])
        planned_task = read_json(self.project_root / "runs" / "planned" / "tasks" / "APP-A-DISC-001.json")
        self.assertEqual(planned_task["manager_role"], "objectives.app-a.frontend-manager")
        self.assertEqual(planned_task["acceptance_role"], "objectives.app-a.acceptance-manager")
        outline = read_json(self.project_root / "runs" / "planned" / "manager-plans" / "discovery-app-a.outline.json")
        self.assertEqual(len(outline["capability_lanes"]), 1)
        self.assertEqual(outline["capability_lanes"][0]["capability"], "frontend")
        self.assertEqual(outline["collaboration_edges"], [])
        manager_plan = read_json(self.project_root / "runs" / "planned" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["bundle_plan"][0]["bundle_id"], "app-a-discovery-bundle-1")

    def test_plan_objective_replace_archives_stale_runtime_graph_and_refreshes_phase_summary(self) -> None:
        scaffold_planning_run(self.project_root, "planned-replace", ["frontend"])
        old_capability_plan = capability_plan_for_objective("planned-replace", "app-a", "frontend")
        old_capability_plan["tasks"][0]["task_id"] = "APP-A-OLD-001"
        old_capability_plan["bundle_plan"][0]["bundle_id"] = "old-frontend-bundle"
        old_capability_plan["bundle_plan"][0]["task_ids"] = ["APP-A-OLD-001"]
        new_capability_plan = capability_plan_for_objective("planned-replace", "app-a", "frontend")
        new_capability_plan["tasks"][0]["task_id"] = "APP-A-NEW-001"
        new_capability_plan["bundle_plan"][0]["bundle_id"] = "new-frontend-bundle"
        new_capability_plan["bundle_plan"][0]["task_ids"] = ["APP-A-NEW-001"]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"frontend-thread-old"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(old_capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"frontend-thread-new"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(new_capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        run_dir = self.project_root / "runs" / "planned-replace"
        (run_dir / "bundles").mkdir(exist_ok=True)
        (run_dir / "reports").mkdir(exist_ok=True)
        (run_dir / "executions").mkdir(exist_ok=True)
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses) as planner:
            first_summary = plan_objective(self.project_root, "planned-replace", "app-a")
            self.assertEqual(first_summary["task_ids"], ["APP-A-OLD-001"])
            ensure_activity(
                self.project_root,
                "planned-replace",
                activity_id="APP-A-OLD-001",
                kind="task_execution",
                entity_id="APP-A-OLD-001",
                phase="discovery",
                objective_id="app-a",
                display_name="APP-A-OLD-001",
                assigned_role="objectives.app-a.frontend-worker",
                status="queued",
                current_activity="Queued task APP-A-OLD-001 for execution.",
                output_path="runs/planned-replace/reports/APP-A-OLD-001.json",
                stdout_path="runs/planned-replace/executions/APP-A-OLD-001.stdout.jsonl",
                stderr_path="runs/planned-replace/executions/APP-A-OLD-001.stderr.log",
            )
            ensure_activity(
                self.project_root,
                "planned-replace",
                activity_id="APP-A-STALE-001",
                kind="task_execution",
                entity_id="APP-A-STALE-001",
                phase="discovery",
                objective_id="app-a",
                display_name="APP-A-STALE-001",
                assigned_role="objectives.app-a.frontend-worker",
                status="blocked",
                current_activity="Blocked by stale graph.",
                status_reason="Missing required handoff deliverables.",
                output_path="runs/planned-replace/reports/APP-A-STALE-001.json",
                stdout_path="runs/planned-replace/executions/APP-A-STALE-001.stdout.jsonl",
                stderr_path="runs/planned-replace/executions/APP-A-STALE-001.stderr.log",
            )
            write_json(run_dir / "reports" / "APP-A-OLD-001.json", {"task_id": "APP-A-OLD-001", "status": "ready_for_bundle_review"})
            write_json(run_dir / "reports" / "APP-A-STALE-001.json", {"task_id": "APP-A-STALE-001", "status": "blocked"})
            write_json(run_dir / "executions" / "APP-A-OLD-001.json", {"task_id": "APP-A-OLD-001", "status": "ready_for_bundle_review"})
            write_json(run_dir / "executions" / "APP-A-STALE-001.json", {"task_id": "APP-A-STALE-001", "status": "blocked"})
            (run_dir / "executions" / "APP-A-OLD-001.stdout.jsonl").write_text("", encoding="utf-8")
            (run_dir / "executions" / "APP-A-STALE-001.stdout.jsonl").write_text("", encoding="utf-8")
            write_json(
                run_dir / "bundles" / "app-a-stale-bundle.json",
                {
                    "bundle_id": "app-a-stale-bundle",
                    "phase": "discovery",
                    "objective_id": "app-a",
                    "included_tasks": ["APP-A-OLD-001"],
                    "status": "planned",
                },
            )
            with patch("company_orchestrator.objective_planner.cleanup_phase_task_worktrees") as cleanup_worktrees:
                replaced_summary = plan_objective(self.project_root, "planned-replace", "app-a", replace=True)

        self.assertEqual(planner.call_count, 2)
        self.assertEqual(first_summary["planning_mode"], "single_capability_fast_path")
        self.assertEqual(replaced_summary["planning_mode"], "single_capability_fast_path")
        self.assertEqual(replaced_summary["task_ids"], ["APP-A-NEW-001"])
        self.assertEqual(
            read_json(run_dir / "manager-plans" / "discovery-phase-plan-summary.json")["planned_objectives"][0]["task_ids"],
            ["APP-A-NEW-001"],
        )
        self.assertFalse((run_dir / "tasks" / "APP-A-OLD-001.json").exists())
        self.assertFalse((run_dir / "live" / "activities" / "APP-A-OLD-001.json").exists())
        self.assertFalse((run_dir / "live" / "activities" / "APP-A-STALE-001.json").exists())
        self.assertFalse((run_dir / "reports" / "APP-A-OLD-001.json").exists())
        self.assertFalse((run_dir / "reports" / "APP-A-STALE-001.json").exists())
        self.assertFalse((run_dir / "executions" / "APP-A-OLD-001.json").exists())
        self.assertFalse((run_dir / "executions" / "APP-A-STALE-001.json").exists())
        self.assertFalse((run_dir / "bundles" / "app-a-stale-bundle.json").exists())
        self.assertTrue((run_dir / "tasks" / "APP-A-NEW-001.json").exists())
        archive_root = run_dir / "archive" / "objective-replans" / "discovery" / "app-a"
        self.assertTrue(any(archive_root.glob("*/live/activities/APP-A-OLD-001.json")))
        self.assertTrue(any(archive_root.glob("*/live/activities/APP-A-STALE-001.json")))
        self.assertTrue(any(archive_root.glob("*/reports/APP-A-OLD-001.json")))
        cleaned_ids = cleanup_worktrees.call_args.args[2]
        self.assertIn("APP-A-OLD-001", cleaned_ids)
        self.assertIn("APP-A-STALE-001", cleaned_ids)

    def test_plan_objective_streams_live_activity_updates_and_events(self) -> None:
        scaffold_planning_run(self.project_root, "planned-live", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-live", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-live", "app-a", "frontend")
        objective_lines = [
            '{"type":"thread.started","thread_id":"plan-thread-live"}',
            '{"type":"turn.started"}',
            json_line_event(
                "item.started",
                {"id": "cmd-1", "type": "command_execution", "command": "plan objective"},
            ),
            json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
        ]
        capability_stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"capability-thread-live"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        call_count = {"value": 0}

        def side_effect(*_: object, **kwargs: object):
            index = call_count["value"]
            call_count["value"] += 1
            callback = kwargs["on_stdout_line"]
            if index == 0:
                for line in objective_lines:
                    callback(line)
                return completed_process(stdout="\n".join(objective_lines), stderr="", returncode=0)
            return completed_process(stdout=capability_stdout, stderr="", returncode=0)

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

    def test_plan_objective_records_observability_artifacts(self) -> None:
        scaffold_planning_run(self.project_root, "planned-observability", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-observability", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-observability", "app-a", "frontend")
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-thread-obs"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":11,"cached_input_tokens":1,"output_tokens":6}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-obs"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":7,"cached_input_tokens":0,"output_tokens":4}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            plan_objective(self.project_root, "planned-observability", "app-a")

        llm_calls = read_json_lines(self.project_root / "runs" / "planned-observability" / "live" / "llm-calls.jsonl")
        self.assertEqual(len(llm_calls), 2)
        self.assertEqual({call["kind"] for call in llm_calls}, {"objective_plan", "capability_plan"})

        run_observability = read_json(
            self.project_root / "runs" / "planned-observability" / "live" / "observability.json"
        )
        self.assertEqual(run_observability["total_calls"], 2)
        self.assertEqual(run_observability["completed_calls"], 2)
        self.assertEqual(run_observability["calls_by_kind"]["objective_plan"], 1)
        self.assertEqual(run_observability["calls_by_kind"]["capability_plan"], 1)

    def test_cold_start_planning_tuning_reduces_concurrency_for_large_goal(self) -> None:
        run_dir = initialize_run(
            self.project_root,
            "cold-start-large",
            "# Large Goal\n\n## Summary\n\n"
            + ("This is a larger planning goal. " * 120)
            + "\n\n## Objectives\n- App A\n- App B\n- App C\n- App D",
        )
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "cold-start-large",
            "objectives": [
                {"objective_id": "app-a", "title": "App A", "summary": "A", "status": "approved", "capabilities": ["frontend"]},
                {"objective_id": "app-b", "title": "App B", "summary": "B", "status": "approved", "capabilities": ["backend"]},
                {"objective_id": "app-c", "title": "App C", "summary": "C", "status": "approved", "capabilities": ["general"]},
                {"objective_id": "app-d", "title": "App D", "summary": "D", "status": "approved", "capabilities": ["general"]},
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        tuning = recommend_runtime_tuning(
            self.project_root,
            "cold-start-large",
            phase="discovery",
            action_kind="planning",
            requested_max_concurrency=3,
        )
        self.assertEqual(tuning["effective_max_concurrency"], 2)
        self.assertIn("Cold-start planning heuristic", tuning["reason"])

    def test_render_capability_prompt_trims_unrelated_lanes(self) -> None:
        scaffold_planning_run(self.project_root, "compact-capability", ["frontend", "backend", "general"])
        outline = objective_outline_for_objective(
            "compact-capability",
            "app-a",
            ["frontend", "backend", "general"],
            collaboration_edges=[
                {
                    "edge_id": "edge-fe-be",
                    "from_capability": "frontend",
                    "to_capability": "backend",
                    "to_role": "objectives.app-a.backend-manager",
                    "handoff_type": "contract",
                    "deliverables": ["Frontend delivers a booking contract summary."],
                    "blocking": True,
                    "shared_asset_ids": ["booking-contract"],
                }
            ],
        )
        payload = build_capability_prompt_payload(
            self.project_root,
            "compact-capability",
            "app-a",
            "frontend",
            outline,
        )
        lane_capabilities = [lane["capability"] for lane in payload["objective_outline"]["capability_lanes"]]
        self.assertEqual(lane_capabilities, ["frontend", "backend"])
        self.assertEqual(len(payload["required_outbound_handoffs"]), 1)
        self.assertEqual(
            payload["required_outbound_handoffs"][0]["deliverables"],
            ["Frontend delivers a booking contract summary."],
        )
        self.assertEqual(payload["required_inbound_handoffs"], [])

    def test_render_capability_prompt_splits_inbound_and_outbound_handoffs(self) -> None:
        scaffold_planning_run(self.project_root, "compact-handoffs", ["frontend", "backend", "middleware"])
        outline = objective_outline_for_objective(
            "compact-handoffs",
            "app-a",
            ["frontend", "backend", "middleware"],
            collaboration_edges=[
                {
                    "edge_id": "edge-fe-mw",
                    "from_capability": "frontend",
                    "to_capability": "middleware",
                    "to_role": "objectives.app-a.middleware-manager",
                    "handoff_type": "consumer_needs",
                    "deliverables": ["Frontend delivers consumer needs."],
                    "blocking": True,
                    "shared_asset_ids": ["consumer-needs"],
                },
                {
                    "edge_id": "edge-mw-be",
                    "from_capability": "middleware",
                    "to_capability": "backend",
                    "to_role": "objectives.app-a.backend-manager",
                    "handoff_type": "provider_constraints",
                    "deliverables": ["Middleware delivers provider constraints."],
                    "blocking": True,
                    "shared_asset_ids": ["provider-constraints"],
                },
            ],
        )

        payload = build_capability_prompt_payload(
            self.project_root,
            "compact-handoffs",
            "app-a",
            "middleware",
            outline,
        )

        self.assertEqual(
            [handoff["edge_id"] for handoff in payload["required_inbound_handoffs"]],
            ["edge-fe-mw"],
        )
        self.assertEqual(
            [handoff["edge_id"] for handoff in payload["required_outbound_handoffs"]],
            ["edge-mw-be"],
        )

    def test_build_capability_planning_payload_surfaces_exact_allowed_outputs_and_handoffs(self) -> None:
        scaffold_planning_run(self.project_root, "exact-output-payload", ["frontend", "backend"])
        outline = objective_outline_for_objective("exact-output-payload", "app-a", ["frontend", "backend"])
        frontend_lane = next(lane for lane in outline["capability_lanes"] if lane["capability"] == "frontend")
        frontend_lane["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend-app-shell",
                "path": "apps/app-a/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "frontend-test-runner",
                "path": "apps/app-a/frontend/src/test-runner.ts",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]
        outline["collaboration_edges"] = [
            {
                "edge_id": "edge-fe-be",
                "from_capability": "frontend",
                "to_capability": "backend",
                "to_role": "objectives.app-a.backend-manager",
                "handoff_type": "review_bundle",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "frontend-review-bundle",
                        "path": "apps/app-a/frontend/review/frontend-review-bundle.json",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["frontend-review-surface"],
            }
        ]

        payload = build_capability_planning_payload(
            self.project_root,
            "exact-output-payload",
            "app-a",
            "frontend",
            outline,
        )

        self.assertEqual(
            [item["output_id"] for item in payload["allowed_final_outputs_exact"]],
            ["frontend-app-shell", "frontend-test-runner"],
        )
        self.assertEqual(
            [
                item["output_id"]
                for item in payload["required_outbound_handoffs_exact"][0]["deliverables"]
            ],
            ["frontend-review-bundle"],
        )

    def test_render_capability_prompt_includes_exact_output_contract_section(self) -> None:
        scaffold_planning_run(self.project_root, "exact-output-prompt", ["frontend", "backend"])
        outline = objective_outline_for_objective("exact-output-prompt", "app-a", ["frontend", "backend"])
        existing_output = self.project_root / "apps" / "app-a" / "frontend" / "src" / "App.tsx"
        existing_output.parent.mkdir(parents=True, exist_ok=True)
        existing_output.write_text("export const App = () => null;\n", encoding="utf-8")
        frontend_lane = next(lane for lane in outline["capability_lanes"] if lane["capability"] == "frontend")
        frontend_lane["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend-app-shell",
                "path": "apps/app-a/frontend/src/App.tsx",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        outline["collaboration_edges"] = [
            {
                "edge_id": "edge-fe-be",
                "from_capability": "frontend",
                "to_capability": "backend",
                "to_role": "objectives.app-a.backend-manager",
                "handoff_type": "review_bundle",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "frontend-review-bundle",
                        "path": "apps/app-a/frontend/review/frontend-review-bundle.json",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["frontend-review-surface"],
            }
        ]

        metadata = render_capability_planning_prompt(
            self.project_root,
            "exact-output-prompt",
            "app-a",
            "frontend",
            outline,
        )
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text(encoding="utf-8")

        self.assertIn("# Exact Output Contract", prompt_text)
        self.assertIn("Your plan must cover exactly the final lane outputs listed in `Allowed Final Outputs`.", prompt_text)
        self.assertIn("`frontend-app-shell` (artifact) -> path `apps/app-a/frontend/src/App.tsx`", prompt_text)
        self.assertIn("## Existing Required Paths", prompt_text)
        self.assertIn("`frontend-app-shell` -> existing file `apps/app-a/frontend/src/App.tsx`", prompt_text)
        self.assertIn("`edge-fe-be` -> `backend` via `objectives.app-a.backend-manager`", prompt_text)
        self.assertIn("deliverable_output_ids: `frontend-review-bundle`", prompt_text)
        self.assertIn("If a file does not already exist and your task will create it, declare it in `expected_outputs`, not `writes_existing_paths`.", prompt_text)
        self.assertIn("If a same-phase dependency comes from another task in this capability lane, reference it as `Output of <task-id>`", prompt_text)
        self.assertIn("If a same-phase dependency comes from an inbound handoff deliverable, reference it with the exact `Planning Inputs.required_inbound_handoffs[...]` path", prompt_text)
        self.assertIn("Do not place nonexistent future repo paths from same-phase work into task inputs.", prompt_text)

    def test_build_capability_planning_payload_surfaces_existing_required_output_paths(self) -> None:
        scaffold_planning_run(self.project_root, "existing-required-outputs", ["frontend"])
        existing_output = self.project_root / "apps" / "todo" / "frontend" / "src" / "index.js"
        existing_output.parent.mkdir(parents=True, exist_ok=True)
        existing_output.write_text("module.exports = {};\n", encoding="utf-8")
        outline = objective_outline_for_objective("existing-required-outputs", "app-a", ["frontend"])
        frontend_lane = next(lane for lane in outline["capability_lanes"] if lane["capability"] == "frontend")
        frontend_lane["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "frontend-entrypoint",
                "path": "apps/todo/frontend/src/index.js",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]

        payload = build_capability_planning_payload(
            self.project_root,
            "existing-required-outputs",
            "app-a",
            "frontend",
            outline,
        )

        self.assertEqual(
            payload["existing_required_output_paths_exact"],
            [{"output_id": "frontend-entrypoint", "path": "apps/todo/frontend/src/index.js"}],
        )

    def test_note_activity_stream_updates_inflight_observability_summary(self) -> None:
        scaffold_smoke_test(self.project_root, "stream-observability")
        ensure_activity(
            self.project_root,
            "stream-observability",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="running",
            progress_stage="running",
            current_activity="Working.",
            prompt_path="runs/stream-observability/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/stream-observability/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/stream-observability/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/stream-observability/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            process_metadata={"pid": os.getpid(), "started_at": "2026-03-10T00:00:00Z", "command": "codex exec", "cwd": str(self.project_root)},
        )
        note_activity_stream(self.project_root, "stream-observability", "APP-A-SMOKE-001", stdout_bytes=12, stderr_bytes=4)
        activity = read_activity(self.project_root, "stream-observability", "APP-A-SMOKE-001")
        self.assertEqual(activity["observability"]["stream_stdout_bytes"], 12)
        self.assertEqual(activity["observability"]["stream_stderr_bytes"], 4)
        self.assertIsNotNone(activity["observability"]["last_signal_at"])
        summary = refresh_run_observability(self.project_root, "stream-observability")
        self.assertEqual(summary["active_processes"], 1)
        self.assertEqual(summary["active_stream_stdout_bytes"], 12)
        self.assertEqual(summary["active_stream_stderr_bytes"], 4)

    def test_recovery_ignores_prompt_metadata_when_scanning_capability_plans(self) -> None:
        scaffold_planning_run(self.project_root, "recover-prompt-json", ["frontend"])
        activity = {
            "activity_id": "plan:discovery:app-a",
            "kind": "objective_plan",
            "phase": "discovery",
            "output_path": "runs/recover-prompt-json/manager-plans/discovery-app-a.json",
            "stdout_path": "runs/recover-prompt-json/manager-plans/discovery-app-a.stdout.jsonl",
            "stderr_path": "runs/recover-prompt-json/manager-plans/discovery-app-a.stderr.log",
        }
        plans_dir = self.project_root / "runs" / "recover-prompt-json" / "manager-plans"
        write_json(plans_dir / "discovery-app-a.prompt.json", {"prompt_path": "x", "prompt_char_count": 100})
        (plans_dir / "discovery-app-a.stdout.jsonl").write_text('{"type":"turn.started"}\n', encoding="utf-8")
        details = inspect_planning_artifacts(self.project_root, "recover-prompt-json", activity)
        self.assertFalse(any(".prompt.json" in detail for detail in details["details"]))

    def test_plan_objective_normalizes_model_generated_run_id(self) -> None:
        scaffold_planning_run(self.project_root, "planned-run-id", ["frontend"])
        objective_outline = objective_outline_for_objective("wrong-run-id", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-run-id", "app-a", "frontend")
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-run-id"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-run-id"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            summary = plan_objective(self.project_root, "planned-run-id", "app-a")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["from"], "wrong-run-id")
        self.assertEqual(summary["identity_adjustments"]["run_id"]["to"], "planned-run-id")
        manager_plan = read_json(self.project_root / "runs" / "planned-run-id" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["run_id"], "planned-run-id")

    def test_plan_objective_prefixes_bundle_ids_with_objective_id(self) -> None:
        scaffold_planning_run(self.project_root, "planned-bundles", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-bundles", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-bundles", "app-a", "frontend")
        capability_plan["bundle_plan"] = [
            {
                "bundle_id": "bundle-discovery-core",
                "task_ids": ["APP-A-FRONTEND-001"],
                "summary": "Unscoped bundle id from model",
            }
        ]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-bundle-id"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-bundle-id"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            summary = plan_objective(self.project_root, "planned-bundles", "app-a")
        self.assertEqual(summary["bundle_ids"], ["app-a-bundle-discovery-core"])
        manager_plan = read_json(self.project_root / "runs" / "planned-bundles" / "manager-plans" / "discovery-app-a.json")
        self.assertEqual(manager_plan["bundle_plan"][0]["bundle_id"], "app-a-bundle-discovery-core")

    def test_plan_objective_aggregates_capability_manager_plans(self) -> None:
        scaffold_planning_run(self.project_root, "planned-capability", ["frontend", "backend"])
        objective_outline = objective_outline_for_objective("planned-capability", "app-a", ["frontend", "backend"])
        frontend_plan = capability_plan_for_objective("planned-capability", "app-a", "frontend")
        backend_plan = capability_plan_for_objective("planned-capability", "app-a", "backend")

        def side_effect(*args: object, **kwargs: object):
            command_text = " ".join(str(part) for part in args[0])
            if "objective-outline.v1.json" in command_text:
                payload = objective_outline
                thread_id = "objective-thread"
            elif "discovery-app-a-backend" in command_text:
                payload = backend_plan
                thread_id = "backend-thread"
            else:
                payload = frontend_plan
                thread_id = "frontend-thread"
            stdout = "\n".join(
                [
                    f'{{"type":"thread.started","thread_id":"{thread_id}"}}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            )
            return completed_process(stdout=stdout, stderr="", returncode=0)

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect):
            summary = plan_objective(self.project_root, "planned-capability", "app-a")
        self.assertEqual(summary["planning_mode"], "capability_managed")
        self.assertEqual(len(summary["capability_summaries"]), 2)
        task_ids = summary["task_ids"]
        self.assertIn("APP-A-FRONTEND-001", task_ids)
        self.assertIn("APP-A-BACKEND-001", task_ids)
        manager_plan = read_json(
            self.project_root / "runs" / "planned-capability" / "manager-plans" / "discovery-app-a.json"
        )
        self.assertEqual(manager_plan["schema"], "objective-plan.v1")
        self.assertEqual(
            {task["task_id"] for task in manager_plan["tasks"]},
            {"APP-A-FRONTEND-001", "APP-A-BACKEND-001"},
        )
        outline = read_json(
            self.project_root / "runs" / "planned-capability" / "manager-plans" / "discovery-app-a.outline.json"
        )
        self.assertEqual(len(outline["capability_lanes"]), 2)
        capability_activity = read_json(
            self.project_root
            / "runs"
            / "planned-capability"
            / "live"
            / "activities"
            / "plan__discovery__app-a__frontend.json"
        )
        self.assertEqual(capability_activity["kind"], "capability_plan")

    def test_plan_objective_persists_normalized_task_execution_metadata(self) -> None:
        scaffold_planning_run(self.project_root, "planned-task-persistence", ["backend"])
        objective_outline = objective_outline_for_objective("planned-task-persistence", "app-a", ["backend"])
        backend_plan = capability_plan_for_objective("planned-task-persistence", "app-a", "backend")
        backend_task = backend_plan["tasks"][0]
        backend_task["expected_outputs"] = [
            "backend-discovery-review-bundle",
            "apps/todo/backend/discovery/backend-discovery-review-bundle.json",
        ]
        backend_task["owned_paths"] = ["apps/todo/backend/discovery/backend-discovery-review-bundle.json"]
        backend_task["validation"] = [
            {
                "id": "bundle-file-exists",
                "command": "test -f backend/discovery/backend-discovery-review-bundle.json",
            }
        ]
        backend_task["sandbox_mode"] = "workspace-write"

        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"backend-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(backend_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            plan_objective(self.project_root, "planned-task-persistence", "app-a")

        manager_plan = read_json(
            self.project_root / "runs" / "planned-task-persistence" / "manager-plans" / "discovery-app-a.json"
        )
        persisted_task = next(task for task in manager_plan["tasks"] if task["task_id"] == "APP-A-BACKEND-001")
        self.assertEqual(persisted_task["execution_mode"], "isolated_write")
        self.assertEqual(persisted_task["sandbox_mode"], "workspace-write")
        self.assertEqual(
            persisted_task["validation"][0]["command"],
            "test -f apps/todo/backend/discovery/backend-discovery-review-bundle.json",
        )

        task_payload = read_json(
            self.project_root / "runs" / "planned-task-persistence" / "tasks" / "APP-A-BACKEND-001.json"
        )
        self.assertEqual(task_payload["execution_mode"], "isolated_write")
        self.assertEqual(task_payload["sandbox_mode"], "workspace-write")
        self.assertEqual(
            task_payload["validation"][0]["command"],
            "test -f apps/todo/backend/discovery/backend-discovery-review-bundle.json",
        )

    def test_plan_objective_rejects_missing_task_output_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "planned-missing-output", ["middleware"])
        objective_outline = objective_outline_for_objective("planned-missing-output", "app-a", ["middleware"])
        middleware_plan = capability_plan_for_objective("planned-missing-output", "app-a", "middleware")
        middleware_task = middleware_plan["tasks"][0]
        middleware_task["inputs"] = ["Output of MISSING-UPSTREAM-TASK"]
        middleware_task["depends_on"] = ["MISSING-UPSTREAM-TASK"]
        middleware_task["sandbox_mode"] = "workspace-write"

        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"middleware-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(middleware_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            with self.assertRaisesRegex(ExecutorError, "Output of MISSING-UPSTREAM-TASK"):
                plan_objective(self.project_root, "planned-missing-output", "app-a")

    def test_plan_objective_splits_asset_in_path_output_descriptors(self) -> None:
        scaffold_planning_run(self.project_root, "planned-output-descriptor", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-output-descriptor", "app-a", ["frontend"])
        frontend_plan = capability_plan_for_objective("planned-output-descriptor", "app-a", "frontend")
        frontend_task = frontend_plan["tasks"][0]
        mixed_descriptor = "asset.contract.frontend-todo-integration.v1 in apps/todo/frontend/frontend-todo-integration.v1.json"
        frontend_task["expected_outputs"] = [mixed_descriptor]
        frontend_task["owned_paths"] = [mixed_descriptor]
        frontend_task["validation"] = [
            {
                "id": "validate-integration-contract-shape",
                "command": "node -e \"const doc=require('./frontend-todo-integration.v1.json'); if (!doc) process.exit(1);\"",
            }
        ]
        frontend_task["sandbox_mode"] = "workspace-write"

        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"frontend-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(frontend_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            plan_objective(self.project_root, "planned-output-descriptor", "app-a")

        manager_plan = read_json(
            self.project_root / "runs" / "planned-output-descriptor" / "manager-plans" / "discovery-app-a.json"
        )
        persisted_task = next(task for task in manager_plan["tasks"] if task["task_id"] == "APP-A-FRONTEND-001")
        self.assertEqual(
            persisted_task["expected_outputs"],
            [
                "asset.contract.frontend-todo-integration.v1",
                "apps/todo/frontend/frontend-todo-integration.v1.json",
            ],
        )
        self.assertEqual(
            persisted_task["owned_paths"][0],
            "apps/todo/frontend/frontend-todo-integration.v1.json",
        )
        self.assertIn(
            "apps/todo/frontend/frontend-todo-integration.v1.json",
            persisted_task["validation"][0]["command"],
        )

        task_payload = read_json(
            self.project_root / "runs" / "planned-output-descriptor" / "tasks" / "APP-A-FRONTEND-001.json"
        )
        self.assertEqual(
            task_payload["expected_outputs"],
            [
                "asset.contract.frontend-todo-integration.v1",
                "apps/todo/frontend/frontend-todo-integration.v1.json",
            ],
        )
        self.assertEqual(
            task_payload["owned_paths"][0],
            "apps/todo/frontend/frontend-todo-integration.v1.json",
        )
        self.assertIn(
            "apps/todo/frontend/frontend-todo-integration.v1.json",
            task_payload["validation"][0]["command"],
        )

    def test_plan_objective_materializes_cross_lane_handoffs_and_report_summary(self) -> None:
        scaffold_planning_run(self.project_root, "planned-handoffs", ["frontend", "backend"])
        objective_outline = objective_outline_for_objective(
            "planned-handoffs",
            "app-a",
            ["frontend", "backend"],
            collaboration_edges=[
                {
                    "edge_id": "api-contract",
                    "from_capability": "frontend",
                    "to_capability": "backend",
                    "to_role": "objectives.app-a.backend-manager",
                    "handoff_type": "contract",
                    "reason": "Backend needs the frontend-owned API contract before implementation planning.",
                    "deliverables": ["docs/contracts/app-a-api.md"],
                    "blocking": True,
                    "shared_asset_ids": ["app-a:api-contract"],
                }
            ],
        )
        frontend_plan = capability_plan_for_objective(
            "planned-handoffs",
            "app-a",
            "frontend",
            collaboration_handoffs=[
                {
                    "handoff_id": "api-contract",
                    "from_capability": "frontend",
                    "to_capability": "backend",
                    "from_task_id": "APP-A-FRONTEND-001",
                    "to_role": "objectives.app-a.backend-manager",
                    "handoff_type": "contract",
                    "reason": "Publish the API contract for backend planning.",
                    "deliverable_output_ids": ["artifact:docs/contracts/app-a-api.md"],
                    "blocking": True,
                    "shared_asset_ids": ["app-a:api-contract"],
                }
            ],
        )
        frontend_plan["tasks"][0]["expected_outputs"] = ["docs/contracts/app-a-api.md"]
        backend_plan = capability_plan_for_objective("planned-handoffs", "app-a", "backend")
        backend_plan["tasks"][0]["depends_on"] = ["APP-A-FRONTEND-001"]
        backend_plan["tasks"][0]["inputs"] = ["Output of APP-A-FRONTEND-001"]

        def side_effect(*args: object, **kwargs: object):
            command_text = " ".join(str(part) for part in args[0])
            if "objective-outline.v1.json" in command_text:
                payload = objective_outline
                thread_id = "objective-thread"
            elif "discovery-app-a-backend" in command_text:
                payload = backend_plan
                thread_id = "backend-thread"
            else:
                payload = frontend_plan
                thread_id = "frontend-thread"
            stdout = "\n".join(
                [
                    f'{{"type":"thread.started","thread_id":"{thread_id}"}}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            )
            return completed_process(stdout=stdout, stderr="", returncode=0)

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect):
            summary = plan_objective(self.project_root, "planned-handoffs", "app-a")

        self.assertEqual(summary["handoff_ids"], ["app-a-frontend-api-contract"])
        manager_plan = read_json(
            self.project_root / "runs" / "planned-handoffs" / "manager-plans" / "discovery-app-a.json"
        )
        self.assertEqual(manager_plan["collaboration_handoffs"][0]["handoff_id"], "app-a-frontend-api-contract")
        backend_task = next(task for task in manager_plan["tasks"] if task["task_id"] == "APP-A-BACKEND-001")
        self.assertIn("app-a:api-contract", backend_task["shared_asset_ids"])
        self.assertEqual(backend_task["handoff_dependencies"], ["app-a-frontend-api-contract"])
        handoff_path = (
            self.project_root
            / "runs"
            / "planned-handoffs"
            / "collaboration-plans"
            / "app-a-frontend-api-contract.json"
        )
        self.assertTrue(handoff_path.exists())
        handoff_payload = read_json(handoff_path)
        self.assertEqual(handoff_payload["to_task_ids"], ["APP-A-BACKEND-001"])

        report, _ = generate_phase_report(self.project_root, "planned-handoffs")
        self.assertEqual(report["collaboration_summary"]["total_handoffs"], 1)
        self.assertEqual(report["collaboration_summary"]["blocking_handoffs"], 1)
        self.assertEqual(report["collaboration_summary"]["pending_handoffs"], 1)
        self.assertEqual(report["collaboration_summary"]["satisfied_handoffs"], 0)
        self.assertEqual(report["collaboration_summary"]["blocked_handoffs"], 0)

    def test_plan_objective_filters_non_task_dependencies_and_review_handoff_fallbacks(self) -> None:
        scaffold_planning_run(self.project_root, "planned-sanitize", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-sanitize", "app-a", ["frontend"])
        capability_plan = {
            "schema": "capability-plan.v1",
            "run_id": "planned-sanitize",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "summary": "Sanitized frontend discovery plan",
            "tasks": [
                {
                    "task_id": "FE-DISC-01-scope-brief",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write the scope brief.",
                    "inputs": ["Planning Inputs.objective.summary"],
                    "expected_outputs": ["frontend-mvp-scope-brief-v1"],
                    "done_when": ["scope is documented"],
                    "depends_on": ["Approved planning inputs for scope, constraints, and success criteria"],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
                {
                    "task_id": "FE-DISC-02-boundary-and-contract",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write the boundary and contract notes.",
                    "inputs": ["Planning Inputs.objective.summary"],
                    "expected_outputs": ["frontend-contract-question-log-v1"],
                    "done_when": ["contract gaps are documented"],
                    "depends_on": ["Approved planning inputs for scope, constraints, and success criteria"],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
                {
                    "task_id": "FE-DISC-03-unknowns-and-task-graph",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Write the unknowns and task graph notes.",
                    "inputs": ["Planning Inputs.objective.summary"],
                    "expected_outputs": ["frontend-unknowns-register-v1"],
                    "done_when": ["unknowns are documented"],
                    "depends_on": ["Approved planning inputs for scope, constraints, and success criteria"],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
                {
                    "task_id": "FE-DISC-04-acceptance-bundle",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Bundle discovery outputs for review.",
                    "inputs": [
                        "Output of FE-DISC-01-scope-brief",
                        "Output of FE-DISC-02-boundary-and-contract",
                        "Output of FE-DISC-03-unknowns-and-task-graph",
                    ],
                    "expected_outputs": ["frontend-discovery-review-bundle-v1"],
                    "done_when": ["bundle is ready"],
                    "depends_on": [
                        "FE-DISC-01-scope-brief",
                        "FE-DISC-02-boundary-and-contract",
                        "FE-DISC-03-unknowns-and-task-graph",
                    ],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "read-only",
                },
            ],
            "bundle_plan": [
                {
                    "bundle_id": "frontend-discovery-core",
                    "task_ids": [
                        "FE-DISC-01-scope-brief",
                        "FE-DISC-02-boundary-and-contract",
                        "FE-DISC-03-unknowns-and-task-graph",
                    ],
                    "summary": "Core discovery docs",
                },
                {
                    "bundle_id": "frontend-discovery-acceptance",
                    "task_ids": ["FE-DISC-04-acceptance-bundle"],
                    "summary": "Acceptance bundle",
                },
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [
                {
                    "handoff_id": "frontend-to-acceptance-manager-discovery-review",
                    "from_capability": "frontend",
                    "to_capability": "frontend",
                    "from_task_id": "FE-DISC-04-acceptance-bundle",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Acceptance manager review",
                    "deliverables": ["frontend-discovery-review-bundle-v1"],
                    "blocking": True,
                    "shared_asset_ids": ["frontend-discovery-review-bundle-v1"],
                },
                {
                    "handoff_id": "frontend-to-objective-manager-contract-request",
                    "from_capability": "frontend",
                    "to_capability": "frontend",
                    "from_task_id": "FE-DISC-02-boundary-and-contract",
                    "to_role": "objectives.app-a.objective-manager",
                    "handoff_type": "collaboration_request",
                    "reason": "Contract question escalation",
                    "deliverables": ["frontend-contract-question-log-v1"],
                    "blocking": True,
                    "shared_asset_ids": ["frontend-contract-question-log-v1"],
                },
            ],
        }
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"outline-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            plan_objective(self.project_root, "planned-sanitize", "app-a")

        task_1 = read_json(self.project_root / "runs" / "planned-sanitize" / "tasks" / "FE-DISC-01-scope-brief.json")
        task_2 = read_json(self.project_root / "runs" / "planned-sanitize" / "tasks" / "FE-DISC-02-boundary-and-contract.json")
        task_4 = read_json(self.project_root / "runs" / "planned-sanitize" / "tasks" / "FE-DISC-04-acceptance-bundle.json")
        review_handoff_path = next(
            (
                self.project_root
                / "runs"
                / "planned-sanitize"
                / "collaboration-plans"
            ).glob("*acceptance-manager-discovery-review.json")
        )
        review_handoff = read_json(review_handoff_path)

        self.assertEqual(task_1["depends_on"], [])
        self.assertEqual(task_2["depends_on"], [])
        self.assertEqual(task_1["handoff_dependencies"], [])
        self.assertEqual(task_2["handoff_dependencies"], [])
        self.assertEqual(task_4["handoff_dependencies"], ["app-a-frontend-frontend-to-objective-manager-contract-request"])
        self.assertEqual(review_handoff["to_task_ids"], [])

    def test_attach_handoff_dependencies_ignores_same_capability_shared_assets_without_explicit_downstream_link(self) -> None:
        tasks = [
            {
                "task_id": "LANE-001",
                "objective_id": "app-a",
                "capability": "frontend",
                "shared_asset_ids": ["app-a:frontend:handoff"],
                "depends_on": [],
                "inputs": [],
                "handoff_dependencies": [],
            },
            {
                "task_id": "LANE-002",
                "objective_id": "app-a",
                "capability": "frontend",
                "shared_asset_ids": ["app-a:frontend:handoff"],
                "depends_on": [],
                "inputs": [],
                "handoff_dependencies": [],
            },
            {
                "task_id": "LANE-003",
                "objective_id": "app-a",
                "capability": "frontend",
                "shared_asset_ids": ["app-a:frontend:handoff"],
                "depends_on": ["LANE-001"],
                "inputs": [],
                "handoff_dependencies": [],
            },
        ]
        handoffs = [
            {
                "handoff_id": "app-a-frontend-review",
                "objective_id": "app-a",
                "from_capability": "frontend",
                "to_capability": "frontend",
                "from_task_id": "LANE-001",
                "to_role": "objectives.app-a.objective-manager",
                "handoff_type": "collaboration_request",
                "reason": "Escalate a shared question.",
                "deliverables": ["frontend-question-log-v1"],
                "blocking": True,
                "shared_asset_ids": ["app-a:frontend:handoff"],
            }
        ]

        attach_handoff_dependencies(tasks, handoffs)

        self.assertEqual(tasks[0]["handoff_dependencies"], [])
        self.assertEqual(tasks[1]["handoff_dependencies"], [])
        self.assertEqual(tasks[2]["handoff_dependencies"], ["app-a-frontend-review"])
        self.assertEqual(handoffs[0]["to_task_ids"], ["LANE-003"])

    def test_blocking_handoffs_ignore_same_capability_shared_asset_overlap_without_target(self) -> None:
        task = {
            "task_id": "LANE-002",
            "objective_id": "app-a",
            "capability": "frontend",
            "shared_asset_ids": ["app-a:frontend:handoff"],
            "handoff_dependencies": [],
        }
        handoffs_by_id = {
            "app-a-frontend-review": {
                "handoff_id": "app-a-frontend-review",
                "objective_id": "app-a",
                "from_capability": "frontend",
                "to_capability": "frontend",
                "from_task_id": "LANE-001",
                "to_role": "objectives.app-a.objective-manager",
                "handoff_type": "collaboration_request",
                "reason": "Escalate a shared question.",
                "deliverables": ["frontend-question-log-v1"],
                "blocking": True,
                "shared_asset_ids": ["app-a:frontend:handoff"],
                "to_task_ids": [],
            }
        }

        blockers = blocking_handoffs_for_task(task, handoffs_by_id)

        self.assertEqual(blockers, [])

    def test_list_handoffs_skips_invalid_json_files(self) -> None:
        scaffold_planning_run(self.project_root, "handoff-race", ["frontend"])
        collaboration_dir = self.project_root / "runs" / "handoff-race" / "collaboration-plans"
        collaboration_dir.mkdir(parents=True, exist_ok=True)
        valid_handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "handoff-race",
            "phase": "discovery",
            "objective_id": "app-a",
            "handoff_id": "HOF-VALID",
            "from_capability": "frontend",
            "to_capability": "backend",
            "from_task_id": "APP-A-FRONTEND-001",
            "to_role": "objectives.app-a.backend-worker",
            "handoff_type": "contract",
            "reason": "Share a contract",
            "deliverables": ["docs/contract.md"],
            "blocking": True,
            "shared_asset_ids": ["asset.contract"],
            "status": "planned",
            "to_task_ids": ["APP-A-BACKEND-001"],
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }
        write_json(collaboration_dir / "HOF-VALID.json", valid_handoff)
        (collaboration_dir / "HOF-BROKEN.json").write_text("", encoding="utf-8")

        handoffs = list_handoffs(self.project_root / "runs" / "handoff-race", phase="discovery")

        self.assertEqual([handoff["handoff_id"] for handoff in handoffs], ["HOF-VALID"])

    def test_plan_objective_runs_capability_managers_concurrently(self) -> None:
        scaffold_planning_run(self.project_root, "planned-capability-parallel", ["frontend", "backend"])
        objective_outline = objective_outline_for_objective("planned-capability-parallel", "app-a", ["frontend", "backend"])
        frontend_plan = capability_plan_for_objective("planned-capability-parallel", "app-a", "frontend")
        backend_plan = capability_plan_for_objective("planned-capability-parallel", "app-a", "backend")
        lock = threading.Lock()
        active_calls = 0
        max_active = 0

        def side_effect(*args: object, **kwargs: object):
            nonlocal active_calls, max_active
            command_text = " ".join(str(part) for part in args[0])
            if "objective-outline.v1.json" in command_text:
                payload = objective_outline
            elif "discovery-app-a-backend" in command_text:
                payload = backend_plan
            else:
                payload = frontend_plan
            with lock:
                active_calls += 1
                max_active = max(max_active, active_calls)
            try:
                time.sleep(0.05)
                stdout = "\n".join(
                    [
                        '{"type":"thread.started","thread_id":"parallel-plan-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                )
                return completed_process(stdout=stdout, stderr="", returncode=0)
            finally:
                with lock:
                    active_calls -= 1

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect):
            summary = plan_objective(self.project_root, "planned-capability-parallel", "app-a", max_concurrency=2)

        self.assertEqual(summary["max_concurrency"], 2)
        self.assertGreaterEqual(max_active, 2)

    def test_plan_objective_rejects_unresolved_generated_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "planned-unresolved", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-unresolved", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-unresolved", "app-a", "frontend")
        capability_plan["tasks"][0]["inputs"] = ["Completely imaginary planning input"]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-unresolved"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-unresolved"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            with self.assertRaisesRegex(ExecutorError, "unresolved input refs for task APP-A-FRONTEND-001"):
                plan_objective(self.project_root, "planned-unresolved", "app-a")

    def test_plan_objective_accepts_runtime_context_assigned_manager_role_alias(self) -> None:
        scaffold_planning_run(self.project_root, "planned-manager-alias", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-manager-alias", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-manager-alias", "app-a", "frontend")
        capability_plan["tasks"][0]["inputs"] = [
            "Planning Inputs.goal_markdown",
            "Runtime Context.assigned_manager_role",
        ]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-manager-alias"}',
                        '{"type":"turn.started"}',
                        json_line_event(
                            "item.completed",
                            {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)},
                        ),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-manager-alias"}',
                        '{"type":"turn.started"}',
                        json_line_event(
                            "item.completed",
                            {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)},
                        ),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            summary = plan_objective(self.project_root, "planned-manager-alias", "app-a")

        self.assertEqual(summary["objective_id"], "app-a")
        self.assertEqual(summary["task_ids"], ["APP-A-FRONTEND-001"])

    def test_plan_objective_canonicalizes_dotted_numeric_input_refs(self) -> None:
        scaffold_planning_run(self.project_root, "planned-dotted-inputs", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-dotted-inputs", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-dotted-inputs", "app-a", "frontend")
        capability_plan["tasks"][0]["expected_outputs"] = [
            {
                "kind": "assertion",
                "output_id": "frontend.discovery.scope-brief",
                "path": None,
                "asset_id": None,
                "description": "Frontend discovery scope brief.",
                "evidence": {"validation_ids": [], "artifact_paths": []},
            }
        ]
        capability_plan["tasks"][0]["inputs"] = [
            "Planning Inputs.capability_scope_hints.shared_asset_hints.0",
            "Planning Inputs.required_outbound_handoffs.0.deliverables.0",
        ]
        capability_plan["collaboration_handoffs"] = [
            {
                "handoff_id": "frontend-to-middleware-discovery-inputs",
                "from_capability": "frontend",
                "to_capability": "acceptance",
                "from_task_id": "APP-A-FRONTEND-001",
                "to_role": "objectives.app-a.acceptance-manager",
                "handoff_type": "discovery-input-handoff",
                "reason": "Middleware needs the finalized frontend discovery packet.",
                "deliverable_output_ids": ["frontend.discovery.scope-brief"],
                "blocking": True,
                "shared_asset_ids": ["frontend.discovery.scope-brief"],
            }
        ]
        objective_outline["collaboration_edges"] = [
            {
                "edge_id": "edge-fe-mw",
                "from_capability": "frontend",
                "to_capability": "acceptance",
                "to_role": "objectives.app-a.acceptance-manager",
                "handoff_type": "discovery-input-handoff",
                "reason": "Middleware needs the finalized frontend discovery packet.",
                "deliverables": [
                    {
                        "kind": "assertion",
                        "output_id": "frontend.discovery.scope-brief",
                        "path": None,
                        "asset_id": None,
                        "description": "Frontend discovery scope brief.",
                        "evidence": {"validation_ids": [], "artifact_paths": []},
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["frontend.discovery.scope-brief"],
            }
        ]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-dotted"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-dotted"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            plan_objective(self.project_root, "planned-dotted-inputs", "app-a")
        task = read_json(self.project_root / "runs" / "planned-dotted-inputs" / "tasks" / "APP-A-FRONTEND-001.json")
        self.assertIn("Planning Inputs.capability_scope_hints.shared_asset_hints[0]", task["inputs"])
        self.assertIn("Planning Inputs.required_outbound_handoffs[0].deliverables[0]", task["inputs"])
        self.assertNotIn("Planning Inputs.capability_scope_hints.shared_asset_hints.0", task["inputs"])

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

    def test_plan_objective_marks_parent_activity_failed_when_capability_planning_fails(self) -> None:
        import subprocess

        scaffold_planning_run(self.project_root, "planned-capability-timeout", ["frontend"])
        outline = objective_outline_for_objective("planned-capability-timeout", "app-a", ["frontend"])
        outline_stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"objective-thread"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        timeout_error = subprocess.TimeoutExpired(
            cmd=["codex", "exec"],
            timeout=7,
            output=b'{"type":"thread.started","thread_id":"capability-timeout"}\n',
            stderr=b"capability manager still reasoning\n",
        )
        responses = [
            completed_process(stdout=outline_stdout, stderr="", returncode=0),
            timeout_error,
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            with self.assertRaisesRegex(ExecutorError, "timed out after 7 seconds while planning app-a:frontend"):
                plan_objective(self.project_root, "planned-capability-timeout", "app-a", timeout_seconds=7)
        activity = read_activity(self.project_root, "planned-capability-timeout", "plan:discovery:app-a")
        self.assertEqual(activity["status"], "failed")
        self.assertEqual(activity["status_reason"], "capability_planning_failed")
        self.assertIn("timed out after 7 seconds", activity["current_activity"])

    def test_plan_objective_retries_timeout_when_using_policy_defaults(self) -> None:
        import subprocess

        scaffold_planning_run(self.project_root, "planned-timeout-retry", ["frontend"])
        outline = objective_outline_for_objective("planned-timeout-retry", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-timeout-retry", "app-a", "frontend")
        timeout_error = subprocess.TimeoutExpired(
            cmd=["codex", "exec"],
            timeout=600,
            output=b'{"type":"thread.started","thread_id":"plan-timeout-retry"}\n',
            stderr=b"manager still reasoning\n",
        )
        objective_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-timeout-retry"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        capability_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"capability-timeout-retry"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=[timeout_error, objective_completed, capability_completed]):
            summary = plan_objective(self.project_root, "planned-timeout-retry", "app-a", timeout_seconds=None)
        self.assertIn(summary["recovery_action"], {"timeout_retry", "planning_repair"})
        activity = read_activity(self.project_root, "planned-timeout-retry", "plan:discovery:app-a")
        self.assertEqual(activity["status"], "recovered")
        events = read_json_lines(self.project_root / "runs" / "planned-timeout-retry" / "live" / "events.jsonl")
        self.assertIn("planning.timeout_retry_scheduled", {event["event_type"] for event in events})

    def test_plan_objective_retries_missing_final_agent_message_once(self) -> None:
        scaffold_planning_run(self.project_root, "planned-missing-final-message", ["frontend"])
        outline = objective_outline_for_objective("planned-missing-final-message", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-missing-final-message", "app-a", "frontend")
        missing_final_message = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-missing-final"}',
                    '{"type":"turn.started"}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        objective_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-retry-success"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        capability_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"capability-success"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        with patch(
            "company_orchestrator.objective_planner.run_codex_command",
            side_effect=[missing_final_message, objective_completed, capability_completed],
        ):
            summary = plan_objective(self.project_root, "planned-missing-final-message", "app-a")
        self.assertIn(summary["recovery_action"], {"missing_final_message_retry", "planning_repair"})
        activity = read_activity(self.project_root, "planned-missing-final-message", "plan:discovery:app-a")
        self.assertEqual(activity["status"], "recovered")
        events = read_json_lines(self.project_root / "runs" / "planned-missing-final-message" / "live" / "events.jsonl")
        self.assertIn("planning.retry_scheduled", {event["event_type"] for event in events})

    def test_plan_objective_retries_stalled_turn_once(self) -> None:
        scaffold_planning_run(self.project_root, "planned-stall-retry", ["frontend"])
        outline = objective_outline_for_objective("planned-stall-retry", "app-a", ["frontend"])
        capability_plan = capability_plan_for_objective("planned-stall-retry", "app-a", "frontend")
        stalled_process = CodexProcessStall(
            cmd=["codex", "exec"],
            stall_seconds=120,
            reason="stall_after_turn_started",
            output="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-stalled"}',
                    '{"type":"turn.started"}',
                ]
            ),
            stderr="",
        )
        objective_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-stall-retry-success"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        capability_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"capability-stall-retry-success"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        with patch(
            "company_orchestrator.objective_planner.run_codex_command",
            side_effect=[stalled_process, objective_completed, capability_completed],
        ):
            summary = plan_objective(self.project_root, "planned-stall-retry", "app-a")
        self.assertIn(summary["recovery_action"], {"stall_retry", "planning_repair"})
        activity = read_activity(self.project_root, "planned-stall-retry", "plan:discovery:app-a")
        self.assertEqual(activity["status"], "recovered")
        events = read_json_lines(self.project_root / "runs" / "planned-stall-retry" / "live" / "events.jsonl")
        event_types = {event["event_type"] for event in events}
        self.assertIn("planning.stall_detected", event_types)
        self.assertIn("planning.retry_scheduled", event_types)

    def test_plan_objective_repairs_post_validation_capability_error_once(self) -> None:
        scaffold_planning_run(self.project_root, "planned-post-validation-repair", ["frontend"])
        outline = objective_outline_for_objective("planned-post-validation-repair", "app-a", ["frontend"])
        invalid_capability_plan = capability_plan_for_objective("planned-post-validation-repair", "app-a", "frontend")
        invalid_capability_plan["tasks"][0]["inputs"] = ["required_outbound_handoffs_exact[0]"]
        repaired_capability_plan = capability_plan_for_objective("planned-post-validation-repair", "app-a", "frontend")
        objective_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"objective-post-validation"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        invalid_capability_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"capability-invalid-post-validation"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(invalid_capability_plan)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        repaired_capability_completed = completed_process(
            stdout="\n".join(
                [
                    '{"type":"thread.started","thread_id":"capability-repaired-post-validation"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(repaired_capability_plan)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                ]
            ),
            stderr="",
            returncode=0,
        )
        with patch(
            "company_orchestrator.objective_planner.run_codex_command",
            side_effect=[objective_completed, invalid_capability_completed, repaired_capability_completed],
        ):
            summary = plan_objective(self.project_root, "planned-post-validation-repair", "app-a")
        self.assertEqual(summary["recovery_action"], "planning_repair")
        activity = read_activity(self.project_root, "planned-post-validation-repair", "plan:discovery:app-a:frontend")
        self.assertEqual(activity["status"], "recovered")
        events = read_json_lines(self.project_root / "runs" / "planned-post-validation-repair" / "live" / "events.jsonl")
        self.assertIn("planning.repair_requested", {event["event_type"] for event in events})

    def test_plan_objective_repairs_invalid_objective_outline_once(self) -> None:
        scaffold_planning_run(self.project_root, "planned-outline-repair", ["frontend"])
        run_dir = self.project_root / "runs" / "planned-outline-repair"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        write_json(run_dir / "phase-plan.json", phase_plan)
        invalid_outline = objective_outline_for_objective("planned-outline-repair", "app-a", ["frontend"])
        invalid_outline["phase"] = "polish"
        invalid_outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "frontend-runtime",
                "path": None,
                "asset_id": "asset.frontend.runtime",
                "description": None,
                "evidence": None,
            }
        ]
        repaired_outline = objective_outline_for_objective("planned-outline-repair", "app-a", ["frontend"])
        repaired_outline["phase"] = "polish"
        capability_plan = capability_plan_for_objective("planned-outline-repair", "app-a", "frontend")
        capability_plan["phase"] = "polish"
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-outline-invalid"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(invalid_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-outline-repair"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(repaired_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-plan-valid"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            summary = plan_objective(self.project_root, "planned-outline-repair", "app-a")

        self.assertEqual(summary["recovery_action"], "planning_repair")
        events = read_json_lines(self.project_root / "runs" / "planned-outline-repair" / "live" / "events.jsonl")
        self.assertIn("planning.repair_requested", {event["event_type"] for event in events})
        self.assertIn("planning.repair_completed", {event["event_type"] for event in events})

    def test_plan_objective_repairs_invalid_capability_plan_once(self) -> None:
        scaffold_planning_run(self.project_root, "planned-capability-repair", ["frontend"])
        run_dir = self.project_root / "runs" / "planned-capability-repair"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        write_json(run_dir / "phase-plan.json", phase_plan)
        outline = objective_outline_for_objective("planned-capability-repair", "app-a", ["frontend"])
        outline["phase"] = "polish"
        invalid_capability_plan = capability_plan_for_objective("planned-capability-repair", "app-a", "frontend")
        invalid_capability_plan["phase"] = "polish"
        invalid_capability_plan["tasks"][0]["inputs"] = ["apps/todo/docs/discovery/missing-upstream-brief.md"]
        repaired_capability_plan = json.loads(json.dumps(invalid_capability_plan))
        repaired_capability_plan["phase"] = "polish"
        repaired_capability_plan["tasks"][0]["inputs"] = ["Planning Inputs.goal_markdown"]
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"objective-outline-valid"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-plan-invalid"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(invalid_capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-plan-repair"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(repaired_capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
            summary = plan_objective(self.project_root, "planned-capability-repair", "app-a")

        self.assertEqual(summary["capability_summaries"][0]["recovery_action"], "planning_repair")
        task = read_json(self.project_root / "runs" / "planned-capability-repair" / "tasks" / "APP-A-FRONTEND-001.json")
        self.assertEqual(task["inputs"], ["Planning Inputs.goal_markdown"])
        events = read_json_lines(self.project_root / "runs" / "planned-capability-repair" / "live" / "events.jsonl")
        self.assertIn("planning.repair_requested", {event["event_type"] for event in events})
        self.assertIn("planning.repair_completed", {event["event_type"] for event in events})

    def test_render_capability_planning_prompt_includes_manager_repair_context(self) -> None:
        scaffold_planning_run(self.project_root, "bundle-repair-prompt", ["frontend"])
        objective_outline = objective_outline_for_objective("bundle-repair-prompt", "app-a", ["frontend"])
        metadata = render_capability_planning_prompt(
            self.project_root,
            "bundle-repair-prompt",
            "app-a",
            "frontend",
            objective_outline,
            repair_context={
                "source": "bundle_review",
                "reason": "Repair the plan after bundle rejection.",
                "bundle_id": "frontend-review-bundle",
                "included_task_ids": ["APP-A-FRONTEND-001"],
                "rejection_reasons": ["APP-A-FRONTEND-001: validation smoke did not pass"],
            },
        )
        prompt_text = (self.project_root / metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("# Manager Repair Context", prompt_text)
        self.assertIn("frontend-review-bundle", prompt_text)
        self.assertIn("validation smoke did not pass", prompt_text)

    def test_render_capability_planning_prompt_compacts_polish_release_repair_context(self) -> None:
        scaffold_planning_run(self.project_root, "compact-release-repair-prompt", ["middleware"])
        run_dir = self.project_root / "runs" / "compact-release-repair-prompt"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete" if item["phase"] != "polish" else "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        repeated = " ".join(["release repair context"] * 120)
        (run_dir / "goal.md").write_text(
            "\n".join(
                [
                    "# Goal",
                    "",
                    "## Summary",
                    repeated,
                    "",
                    "## Success Criteria",
                    repeated,
                    "",
                    "## Polish Expectations",
                    repeated,
                    "",
                    "## Objective Details",
                    "### App A",
                    repeated,
                ]
            ),
            encoding="utf-8",
        )
        objective_outline = objective_outline_for_objective("compact-release-repair-prompt", "app-a", ["middleware"])
        objective_outline["phase"] = "polish"
        objective_outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "middleware-release-note",
                "path": "apps/todo/runtime/src/frontend-server.js",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        write_json(
            run_dir / "reports" / "middleware-mvp-delivery-package.json",
            {
                "schema": "completion-report.v1",
                "run_id": "compact-release-repair-prompt",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "MIDDLEWARE-MVP-001",
                "agent_role": "objectives.app-a.middleware-worker",
                "status": "ready_for_bundle_review",
                "summary": "Middleware MVP delivery package.",
                "artifacts": [],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
            },
        )
        normal_metadata = render_capability_planning_prompt(
            self.project_root,
            "compact-release-repair-prompt",
            "app-a",
            "middleware",
            objective_outline,
        )
        repair_metadata = render_capability_planning_prompt(
            self.project_root,
            "compact-release-repair-prompt",
            "app-a",
            "middleware",
            objective_outline,
            repair_context={
                "source": "polish_release_validation",
                "reason": "Fix the runtime connectivity failure.",
                "compact_prompt": True,
                "focus_paths": ["apps/todo/runtime/src/frontend-server.js"],
                "rejection_reasons": ["runtime connectivity failed [paths: apps/todo/runtime/src/frontend-server.js]"],
            },
        )
        repair_prompt = (self.project_root / repair_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertLess(repair_metadata["prompt_char_count"], normal_metadata["prompt_char_count"])
        self.assertIn("apps/todo/runtime/src/frontend-server.js", repair_prompt)
        self.assertIn('"phase_report_paths": []', repair_prompt)
        self.assertIn("# Canonical Release Repair Inputs", repair_prompt)
        self.assertIn("Planning Inputs.release_repair_inputs", repair_prompt)

    def test_plan_capability_retries_stalled_polish_release_repair_with_compact_prompt(self) -> None:
        scaffold_planning_run(self.project_root, "compact-release-repair-retry", ["middleware"])
        objective_outline = objective_outline_for_objective("compact-release-repair-retry", "app-a", ["middleware"])
        captured_prompts: list[dict[str, object]] = []
        original_renderer = render_capability_planning_prompt

        def capture_renderer(*args, **kwargs):
            metadata = original_renderer(*args, **kwargs)
            captured_prompts.append(
                {
                    "metadata": metadata,
                    "repair_context": dict(kwargs.get("repair_context") or {}),
                }
            )
            return metadata

        repaired_plan = capability_plan_for_objective("compact-release-repair-retry", "app-a", "middleware")
        stalled_message = (
            "Planning activity for app-a:middleware stalled after 150 seconds "
            "(stall_after_turn_started); resume-phase is recommended."
        )
        with (
            patch("company_orchestrator.objective_planner.render_capability_planning_prompt", side_effect=capture_renderer),
            patch(
                "company_orchestrator.objective_planner.execute_planning_activity",
                side_effect=[
                    ExecutorError(stalled_message),
                    {
                        "payload": repaired_plan,
                        "events": [],
                        "stdout_path": "runs/compact-release-repair-retry/manager-plans/discovery-app-a-middleware.stdout.jsonl",
                        "stderr_path": "runs/compact-release-repair-retry/manager-plans/discovery-app-a-middleware.stderr.log",
                        "last_message_path": "runs/compact-release-repair-retry/manager-plans/discovery-app-a-middleware.last-message.json",
                        "identity_adjustments": {},
                        "attempt": 2,
                        "recovery_action": None,
                    },
                ],
            ),
        ):
            summary, _ = plan_capability(
                self.project_root,
                "compact-release-repair-retry",
                "app-a",
                "middleware",
                objective_outline=objective_outline,
                replace=True,
                sandbox_mode="read-only",
                codex_path="codex",
                timeout_seconds=600,
                planning_limiter=PlanningLimiter(1),
                repair_context={
                    "source": "polish_release_validation",
                    "reason": "Fix the integrated runtime release validation failure.",
                    "compact_prompt": True,
                    "focus_paths": ["apps/todo/runtime/src/frontend-server.js"],
                    "rejection_reasons": ["runtime connectivity failed [paths: apps/todo/runtime/src/frontend-server.js]"],
                },
            )

        self.assertEqual(summary["recovery_action"], "compact_repair_retry")
        self.assertEqual(len(captured_prompts), 2)
        self.assertTrue(captured_prompts[1]["repair_context"].get("compact_retry_used"))
        self.assertLess(
            captured_prompts[1]["metadata"]["prompt_char_count"],
            captured_prompts[0]["metadata"]["prompt_char_count"],
        )
        events = read_json_lines(
            self.project_root / "runs" / "compact-release-repair-retry" / "live" / "events.jsonl"
        )
        self.assertIn("planning.retry_scheduled", {event["event_type"] for event in events})

    def test_plan_objective_reuses_existing_outline_for_single_capability_polish_release_repair(self) -> None:
        scaffold_planning_run(self.project_root, "reuse-outline-release-repair", ["frontend"])
        run_dir = self.project_root / "runs" / "reuse-outline-release-repair"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete" if item["phase"] != "polish" else "active"
        write_json(run_dir / "phase-plan.json", phase_plan)

        outline = objective_outline_for_objective("reuse-outline-release-repair", "app-a", ["frontend"])
        outline["phase"] = "polish"
        outline["capability_lanes"][0]["inputs"] = [
            "runs/phase5-todo-full-018/reports/frontend-mvp-delivery.json",
            "apps/todo/orchestrator/roles/objectives/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/approved/frontend-polish-contract.md",
        ]
        write_json(run_dir / "manager-plans" / "polish-app-a.outline.json", outline)
        write_json(
            run_dir / "reports" / "frontend-mvp-delivery.json",
            {
                "schema": "completion-report.v1",
                "run_id": "reuse-outline-release-repair",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "FRONTEND-MVP-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Frontend MVP delivery.",
                "artifacts": [],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
            },
        )
        approved_contract = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items" / "approved" / "frontend-polish-contract.md"
        approved_contract.parent.mkdir(parents=True, exist_ok=True)
        approved_contract.write_text("approved polish contract", encoding="utf-8")

        aggregated_plan = {
            "schema": "objective-plan.v1",
            "run_id": "reuse-outline-release-repair",
            "phase": "polish",
            "objective_id": "app-a",
            "summary": "Polish repair plan for app-a",
            "tasks": [],
            "bundle_plan": [],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }
        captured_outline: dict[str, Any] = {}

        def capture_plan_capabilities(*args, **kwargs):
            captured_outline.update(kwargs["objective_outline"])
            return ([{"capability": "frontend", "recovery_action": "planning_repair"}], [capability_plan_for_objective("reuse-outline-release-repair", "app-a", "frontend")])

        with (
            patch("company_orchestrator.objective_planner.render_objective_planning_prompt", side_effect=AssertionError("objective prompt should not render")),
            patch(
                "company_orchestrator.objective_planner.plan_capabilities_for_objective",
                side_effect=capture_plan_capabilities,
            ),
            patch("company_orchestrator.objective_planner.aggregate_capability_plans", return_value=aggregated_plan),
            patch("company_orchestrator.objective_planner.validate_objective_plan_contents"),
            patch("company_orchestrator.objective_planner.validate_planned_task_inputs"),
            patch("company_orchestrator.objective_planner.materialize_objective_plan"),
        ):
            summary = plan_objective(
                self.project_root,
                "reuse-outline-release-repair",
                "app-a",
                replace=True,
                repair_context={
                    "source": "polish_release_validation",
                    "reason": "Repair the frontend release validation failure.",
                    "compact_prompt": True,
                },
        )

        self.assertEqual(summary["recovery_action"], "reused_valid_outline_for_release_repair")
        sanitized_inputs = captured_outline["capability_lanes"][0]["inputs"]
        self.assertNotIn("runs/phase5-todo-full-018/reports/frontend-mvp-delivery.json", sanitized_inputs)
        self.assertIn(
            "Planning Inputs.release_repair_inputs.report_frontend_mvp_delivery.path",
            sanitized_inputs,
        )
        self.assertIn(str(approved_contract.relative_to(self.project_root)), sanitized_inputs)
        persisted_outline = read_json(run_dir / "manager-plans" / "polish-app-a.outline.json")
        self.assertEqual(
            persisted_outline["capability_lanes"][0]["inputs"],
            sanitized_inputs,
        )
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        self.assertIn("planning.reuse_outline", {event["event_type"] for event in events})

    def test_finalize_objective_bundle_repairs_rejected_bundle_once(self) -> None:
        scaffold_planning_run(self.project_root, "bundle-repair", ["frontend"])
        run_dir = self.project_root / "runs" / "bundle-repair"
        task = {
            "schema": "task-assignment.v1",
            "run_id": "bundle-repair",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Repairable task.",
            "inputs": ["Planning Inputs.goal_markdown"],
            "expected_outputs": [],
            "done_when": ["task is complete"],
            "depends_on": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
            "validation": [],
            "collaboration_rules": [],
            "working_directory": None,
            "additional_directories": [],
            "sandbox_mode": "read-only",
        }
        write_json(run_dir / "tasks" / "APP-A-FRONTEND-001.json", task)
        report = {
            "schema": "completion-report.v1",
            "run_id": "bundle-repair",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-FRONTEND-001",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Task finished but bundle should reject once.",
            "artifacts": [],
            "validation_results": [{"id": "smoke", "status": "passed", "evidence": "ok"}],
            "open_issues": [],
            "change_requests": [],
        }
        write_json(run_dir / "reports" / "APP-A-FRONTEND-001.json", report)
        repaired_bundle = {
            "bundle_id": "frontend-review-bundle",
            "phase": "discovery",
            "objective_id": "app-a",
            "included_tasks": ["APP-A-FRONTEND-001"],
            "status": "accepted",
            "rejection_reasons": [],
        }

        with (
            patch("company_orchestrator.management.objective_plan_has_no_phase_work", return_value=False),
            patch(
                "company_orchestrator.management.objective_bundle_specs",
                return_value=[{"bundle_id": "frontend-review-bundle", "task_ids": ["APP-A-FRONTEND-001"], "summary": "bundle"}],
            ),
            patch("company_orchestrator.management.assemble_review_bundle"),
            patch(
                "company_orchestrator.management.review_bundle",
                side_effect=[
                    {
                        "bundle_id": "frontend-review-bundle",
                        "phase": "discovery",
                        "objective_id": "app-a",
                        "included_tasks": ["APP-A-FRONTEND-001"],
                        "status": "rejected",
                        "rejection_reasons": ["APP-A-FRONTEND-001: validation smoke did not pass"],
                    },
                    repaired_bundle,
                ],
            ),
            patch(
                "company_orchestrator.management.land_accepted_bundle",
                return_value={"status": "accepted", "bundle": repaired_bundle},
            ),
            patch(
                "company_orchestrator.management.plan_objective",
                return_value={"objective_id": "app-a", "recovery_action": "bundle_repair"},
            ) as plan_objective_mock,
            patch(
                "company_orchestrator.management.schedule_tasks",
                return_value={"phase": "discovery", "executed": [], "failures": []},
            ) as schedule_mock,
        ):
            summary = finalize_objective_bundle(
                self.project_root,
                "bundle-repair",
                "discovery",
                "app-a",
                sandbox_mode="workspace-write",
                codex_path="codex",
                timeout_seconds=600,
                max_concurrency=2,
            )

        self.assertEqual(summary["status"], "accepted")
        plan_kwargs = plan_objective_mock.call_args.kwargs
        self.assertTrue(plan_kwargs["replace"])
        self.assertEqual(plan_kwargs["repair_context"]["source"], "bundle_review")
        self.assertEqual(plan_kwargs["repair_context"]["bundle_id"], "frontend-review-bundle")
        self.assertIn("validation smoke did not pass", plan_kwargs["repair_context"]["rejection_reasons"][0])
        schedule_mock.assert_called_once()
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        self.assertIn("bundle.repair_requested", {event["event_type"] for event in events})
        self.assertIn("bundle.repair_completed", {event["event_type"] for event in events})

    def test_finalize_objective_bundle_stops_after_single_bundle_repair_attempt(self) -> None:
        scaffold_planning_run(self.project_root, "bundle-repair-fail", ["frontend"])
        run_dir = self.project_root / "runs" / "bundle-repair-fail"
        task = {
            "schema": "task-assignment.v1",
            "run_id": "bundle-repair-fail",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.objective-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Repairable task.",
            "inputs": ["Planning Inputs.goal_markdown"],
            "expected_outputs": [],
            "done_when": ["task is complete"],
            "depends_on": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
            "validation": [],
            "collaboration_rules": [],
            "working_directory": None,
            "additional_directories": [],
            "sandbox_mode": "read-only",
        }
        write_json(run_dir / "tasks" / "APP-A-FRONTEND-001.json", task)
        report = {
            "schema": "completion-report.v1",
            "run_id": "bundle-repair-fail",
            "phase": "discovery",
            "objective_id": "app-a",
            "task_id": "APP-A-FRONTEND-001",
            "agent_role": "objectives.app-a.frontend-worker",
            "status": "ready_for_bundle_review",
            "summary": "Task finished but bundle still rejects.",
            "artifacts": [],
            "validation_results": [{"id": "smoke", "status": "passed", "evidence": "ok"}],
            "open_issues": [],
            "change_requests": [],
        }
        write_json(run_dir / "reports" / "APP-A-FRONTEND-001.json", report)
        rejected_bundle = {
            "bundle_id": "frontend-review-bundle",
            "phase": "discovery",
            "objective_id": "app-a",
            "included_tasks": ["APP-A-FRONTEND-001"],
            "status": "rejected",
            "rejection_reasons": ["APP-A-FRONTEND-001: validation smoke did not pass"],
        }

        with (
            patch("company_orchestrator.management.objective_plan_has_no_phase_work", return_value=False),
            patch(
                "company_orchestrator.management.objective_bundle_specs",
                return_value=[{"bundle_id": "frontend-review-bundle", "task_ids": ["APP-A-FRONTEND-001"], "summary": "bundle"}],
            ),
            patch("company_orchestrator.management.assemble_review_bundle"),
            patch(
                "company_orchestrator.management.review_bundle",
                side_effect=[rejected_bundle, rejected_bundle],
            ),
            patch("company_orchestrator.management.plan_objective", return_value={"objective_id": "app-a"}),
            patch(
                "company_orchestrator.management.schedule_tasks",
                return_value={"phase": "discovery", "executed": [], "failures": []},
            ),
            patch("company_orchestrator.management.land_accepted_bundle"),
        ):
            summary = finalize_objective_bundle(
                self.project_root,
                "bundle-repair-fail",
                "discovery",
                "app-a",
                sandbox_mode="workspace-write",
                codex_path="codex",
                timeout_seconds=600,
                max_concurrency=2,
            )

        self.assertEqual(summary["status"], "rejected")
        events = read_json_lines(run_dir / "live" / "events.jsonl")
        self.assertEqual(
            1,
            sum(1 for event in events if event["event_type"] == "bundle.repair_requested"),
        )
        self.assertIn("bundle.repair_failed", {event["event_type"] for event in events})

    def test_run_phase_uses_manager_generated_bundle_plan(self) -> None:
        scaffold_planning_run(self.project_root, "planned-phase", ["frontend"])
        objective_outline = objective_outline_for_objective("planned-phase", "app-a", ["frontend"])
        capability_plan = {
            "schema": "capability-plan.v1",
            "run_id": "planned-phase",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "summary": "Discovery capability plan for app-a",
            "tasks": [
                {
                    "task_id": "APP-A-DISC-001",
                    "capability": "frontend",
                    "assigned_role": "objectives.app-a.frontend-worker",
                    "objective": "Task one.",
                    "inputs": [],
                    "expected_outputs": ["note 1"],
                    "done_when": ["task one complete"],
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
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
                    "execution_mode": "read_only",
                    "parallel_policy": "allow",
                    "owned_paths": [],
                    "shared_asset_ids": [],
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
                    "bundle_id": "discovery-bundle-1",
                    "task_ids": ["APP-A-DISC-001"],
                    "summary": "First discovery bundle"
                },
                {
                    "bundle_id": "discovery-bundle-2",
                    "task_ids": ["APP-A-DISC-002"],
                    "summary": "Second discovery bundle"
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": []
        }
        responses = [
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"plan-thread-456"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(objective_outline)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
            completed_process(
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"capability-thread-456"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(capability_plan)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                    ]
                ),
                stderr="",
                returncode=0,
            ),
        ]
        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=responses):
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
            command_text = " ".join(str(part) for part in args[0])
            objective_id = "app-b" if "discovery-app-b" in command_text else "app-a"
            capability = "backend" if objective_id == "app-b" else "frontend"
            payload = capability_plan_for_objective("plan-phase", objective_id, capability)
            stdout = "\n".join(
                [
                    '{"type":"thread.started","thread_id":"plan-thread"}',
                    '{"type":"turn.started"}',
                    json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
                ]
            )
            return completed_process(stdout=stdout, stderr="", returncode=0)

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect) as planner:
            summary = plan_phase(self.project_root, "plan-phase")

        self.assertEqual(len(summary["planned_objectives"]), 2)
        self.assertEqual(planner.call_count, 2)
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-A-FRONTEND-001.json").exists())
        self.assertTrue((self.project_root / "runs" / "plan-phase" / "tasks" / "APP-B-BACKEND-001.json").exists())

    def test_plan_phase_runs_objectives_concurrently(self) -> None:
        scaffold_dual_planning_run(self.project_root, "plan-phase-parallel")
        lock = threading.Lock()
        active_calls = 0
        max_active = 0

        def side_effect(*args: object, **kwargs: object):
            nonlocal active_calls, max_active
            command_text = " ".join(str(part) for part in args[0])
            objective_id = "app-b" if "discovery-app-b" in command_text else "app-a"
            capability = "backend" if objective_id == "app-b" else "frontend"
            payload = capability_plan_for_objective("plan-phase-parallel", objective_id, capability)
            with lock:
                active_calls += 1
                max_active = max(max_active, active_calls)
            try:
                time.sleep(0.05)
                stdout = "\n".join(
                    [
                        '{"type":"thread.started","thread_id":"parallel-phase-thread"}',
                        '{"type":"turn.started"}',
                        json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(payload)}),
                        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
                    ]
                )
                return completed_process(stdout=stdout, stderr="", returncode=0)
            finally:
                with lock:
                    active_calls -= 1

        with patch("company_orchestrator.objective_planner.run_codex_command", side_effect=side_effect) as planner:
            summary = plan_phase(self.project_root, "plan-phase-parallel", max_concurrency=2)

        self.assertEqual(summary["max_concurrency"], 2)
        self.assertEqual(len(summary["planned_objectives"]), 2)
        self.assertEqual(planner.call_count, 2)
        self.assertGreaterEqual(max_active, 2)

    def test_monitoring_renderers_show_sections_and_prompt_details(self) -> None:
        scaffold_smoke_test(self.project_root, "monitor")
        final_payload = {
            "summary": "Finished the smoke task.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
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
        self.assertIn("LLM Observability", run_output)
        self.assertIn("Active Task Activities", run_output)
        self.assertIn("Objective Progress", run_output)
        self.assertIn("OBJ-", run_output)
        self.assertIn("TSK-", run_output)
        self.assertIn("Activity History", run_output)
        self.assertIn("Elapsed", run_output)
        self.assertIn("Last Event", run_output)
        self.assertIn("Calls by kind", run_output)

        history = read_activity_history(self.project_root, "monitor")
        self.assertTrue(history)
        self.assertEqual(history[-1]["activity_id"], "APP-A-SMOKE-001")
        self.assertEqual(history[-1]["status"], "ready_for_bundle_review")

        console = Console(record=True, width=140)
        console.print(build_activity_detail(self.project_root, "monitor", "APP-A-SMOKE-001", events=10))
        detail_output = console.export_text()
        self.assertIn("Prompt", detail_output)
        self.assertIn("Task Assignment", detail_output)
        self.assertIn("Latest Events", detail_output)
        self.assertIn("Display ID", detail_output)
        self.assertIn("Elapsed:", detail_output)
        self.assertIn("Last event age:", detail_output)

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
        self.assertEqual(print_result_mock.call_count, 0)

    def test_cli_execute_task_watch_with_json_prints_payload(self) -> None:
        scaffold_smoke_test(self.project_root, "watch-cli-json")
        expected = {"task_id": "APP-A-SMOKE-001", "status": "ready_for_bundle_review"}
        argv = [
            "company-orchestrator",
            "--project-root",
            str(self.project_root),
            "--json",
            "execute-task",
            "watch-cli-json",
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
        self.assertEqual(print_result_mock.call_count, 1)
        self.assertEqual(Path(print_result_mock.call_args.args[0]).resolve(), self.project_root.resolve())
        self.assertEqual(print_result_mock.call_args.args[1], expected)
        self.assertEqual(print_result_mock.call_args.kwargs["run_id"], "watch-cli-json")
        self.assertTrue(print_result_mock.call_args.kwargs["leading_blank_line"])
        self.assertTrue(print_result_mock.call_args.kwargs["json_output"])

    def test_bootstrap_run_initializes_run_and_invokes_first_plan(self) -> None:
        goal_path = REPO_ROOT / "apps" / "todo" / "goal-draft.md"
        goal_text = goal_path.read_text(encoding="utf-8")
        summary = bootstrap_run(self.project_root, "bootstrap-run", goal_text)
        self.assertEqual(summary["objective_count"], 3)
        self.assertTrue((self.project_root / "runs" / "bootstrap-run" / "goal.md").exists())
        self.assertTrue((self.project_root / "runs" / "bootstrap-run" / "team-registry.json").exists())

        argv = [
            "company-orchestrator",
            "--project-root",
            str(self.project_root),
            "bootstrap-run",
            "bootstrap-cli",
            str(goal_path),
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("company_orchestrator.cli.plan_phase", return_value={"phase": "discovery"}) as planned,
            patch("company_orchestrator.cli.print_result") as print_result_mock,
        ):
            cli_main()
        planned.assert_called_once()
        payload = print_result_mock.call_args.args[1]
        self.assertIn("bootstrap", payload)
        self.assertIn("planning", payload)
        self.assertFalse(print_result_mock.call_args.kwargs["json_output"])

    def test_format_result_summary_prefers_compact_human_output(self) -> None:
        summary = format_result_summary(
            {
                "run_id": "demo",
                "phase": "discovery",
                "run_status": "ready_for_review",
                "run_status_reason": "Discovery phase report recommends advance and is waiting for human approval.",
                "phase_recommendation": "advance",
                "review_doc_path": "runs/demo/phase-reports/discovery.md",
                "next_action_command": "python3 -m company_orchestrator approve-phase demo discovery",
                "next_action_reason": "Review the phase report, then record approval to unlock advancement.",
                "objectives": {"demo": {"status": "accepted"}},
                "scheduled": {"executed": ["too much detail"]},
            }
        )
        self.assertIn("Run: demo", summary)
        self.assertIn("Status: ready_for_review", summary)
        self.assertIn("Review doc: runs/demo/phase-reports/discovery.md", summary)
        self.assertNotIn("scheduled", summary)
        self.assertNotIn("objectives", summary)

    def test_planning_prompts_use_compact_goal_context(self) -> None:
        scaffold_planning_run(self.project_root, "compact-prompts", ["frontend"])
        objective_metadata = render_objective_planning_prompt(self.project_root, "compact-prompts", "app-a")
        objective_prompt = (self.project_root / objective_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn('"goal_context"', objective_prompt)
        self.assertNotIn('"goal_markdown"', objective_prompt)

        objective_outline = objective_outline_for_objective("compact-prompts", "app-a", ["frontend"])
        capability_metadata = render_capability_planning_prompt(
            self.project_root,
            "compact-prompts",
            "app-a",
            "frontend",
            objective_outline,
        )
        capability_prompt = (self.project_root / capability_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn('"goal_context"', capability_prompt)
        self.assertNotIn('"goal_markdown"', capability_prompt)
        self.assertIn('"relevant_collaboration_edges"', capability_prompt)

    def test_planning_prompts_use_aggressive_compaction_after_slow_planning_calls(self) -> None:
        scaffold_planning_run(self.project_root, "adaptive-compaction", ["frontend"])
        run_dir = self.project_root / "runs" / "adaptive-compaction"
        goal_text = "\n".join(
            [
                "# Goal",
                "",
                "## Summary",
                "A verbose planning goal.",
                "",
                "## Objectives",
                "- App A",
                "",
                "## Objective Details",
            ]
            + [f"### Detail {index}\nThis is extra detail section {index}." for index in range(1, 8)]
        )
        write_json(run_dir / "phase-plan.json", read_json(run_dir / "phase-plan.json"))
        (run_dir / "goal.md").write_text(goal_text, encoding="utf-8")
        record_llm_call(
            self.project_root,
            "adaptive-compaction",
            phase="discovery",
            activity_id="plan:discovery:app-a",
            kind="objective_plan",
            attempt=1,
            started_at="2026-03-12T00:00:00Z",
            completed_at="2026-03-12T00:05:30Z",
            latency_ms=330000,
            queue_wait_ms=0,
            prompt_char_count=24000,
            prompt_line_count=600,
            prompt_bytes=24000,
            timed_out=True,
            retry_scheduled=False,
            success=False,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=600,
            error="timeout",
            label="app-a",
        )

        objective_metadata = render_objective_planning_prompt(self.project_root, "adaptive-compaction", "app-a")
        self.assertEqual(objective_metadata["compaction_profile"], "aggressive")
        self.assertIn("aggressive compaction", objective_metadata["compaction_reason"].lower())

        objective_outline = objective_outline_for_objective("adaptive-compaction", "app-a", ["frontend"])
        capability_metadata = render_capability_planning_prompt(
            self.project_root,
            "adaptive-compaction",
            "app-a",
            "frontend",
            objective_outline,
        )
        self.assertEqual(capability_metadata["compaction_profile"], "aggressive")

    def test_aggressive_compaction_shrinks_goal_sections_and_outline_text(self) -> None:
        scaffold_planning_run(self.project_root, "aggressive-shrink", ["frontend"])
        run_dir = self.project_root / "runs" / "aggressive-shrink"
        repeated = " ".join(["very detailed planning context"] * 80)
        goal_text = "\n".join(
            [
                "# Goal",
                "",
                "## Summary",
                repeated,
                "",
                "## Objectives",
                "- App A",
                "",
                "## Discovery Expectations",
                repeated,
                "",
                "## Objective Details",
                "### App A",
                repeated,
            ]
        )
        (run_dir / "goal.md").write_text(goal_text, encoding="utf-8")
        record_llm_call(
            self.project_root,
            "aggressive-shrink",
            phase="discovery",
            activity_id="plan:discovery:app-a",
            kind="objective_plan",
            attempt=1,
            started_at="2026-03-12T00:00:00Z",
            completed_at="2026-03-12T00:05:30Z",
            latency_ms=330000,
            queue_wait_ms=0,
            prompt_char_count=24000,
            prompt_line_count=600,
            prompt_bytes=24000,
            timed_out=True,
            retry_scheduled=False,
            success=False,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            stdout_bytes=0,
            stderr_bytes=0,
            timeout_seconds=600,
            error="timeout",
            label="app-a",
        )
        objective_metadata = render_objective_planning_prompt(self.project_root, "aggressive-shrink", "app-a")
        objective_prompt = (self.project_root / objective_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("very detailed planning context", objective_prompt)
        self.assertNotIn(repeated, objective_prompt)

        objective_outline = objective_outline_for_objective("aggressive-shrink", "app-a", ["frontend"])
        objective_outline["summary"] = repeated
        objective_outline["dependency_notes"] = [repeated, repeated]
        capability_metadata = render_capability_planning_prompt(
            self.project_root,
            "aggressive-shrink",
            "app-a",
            "frontend",
            objective_outline,
        )
        capability_prompt = (self.project_root / capability_metadata["prompt_path"]).read_text(encoding="utf-8")
        self.assertNotIn(f'"summary": "{repeated}"', capability_prompt)

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
                "legacy_dependency_notes": [f"{phase} dependency"],
                "open_issues": [f"{phase} issue"],
                "legacy_follow_ups": [],
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

    def test_reconcile_run_marks_stale_task_interrupted_when_process_is_missing(self) -> None:
        scaffold_smoke_test(self.project_root, "recover-stale")
        ensure_activity(
            self.project_root,
            "recover-stale",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="finalizing",
            progress_stage="finalizing",
            current_activity="Codex turn completed.",
            prompt_path="runs/recover-stale/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/recover-stale/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/recover-stale/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/recover-stale/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            process_metadata={"pid": 999999, "started_at": "2026-03-10T00:00:00Z", "command": "codex exec", "cwd": str(self.project_root)},
        )
        summary = reconcile_run(self.project_root, "recover-stale", apply=True)
        activity = read_activity(self.project_root, "recover-stale", "APP-A-SMOKE-001")
        self.assertEqual(activity["status"], "interrupted")
        self.assertIn("Process missing", activity["status_reason"])
        self.assertEqual(summary["activities"][0]["status"], "interrupted")

    def test_reconcile_run_marks_stale_task_recovered_from_existing_report(self) -> None:
        scaffold_smoke_test(self.project_root, "recover-report")
        write_managed_report(
            self.project_root,
            "recover-report",
            "APP-A-SMOKE-001",
            status="ready_for_bundle_review",
            summary="Recovered from persisted report.",
        )
        ensure_activity(
            self.project_root,
            "recover-report",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="finalizing",
            progress_stage="finalizing",
            current_activity="Codex turn completed.",
            prompt_path="runs/recover-report/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/recover-report/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/recover-report/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/recover-report/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            process_metadata={"pid": 999999, "started_at": "2026-03-10T00:00:00Z", "command": "codex exec", "cwd": str(self.project_root)},
        )
        reconcile_run(self.project_root, "recover-report", apply=True)
        activity = read_activity(self.project_root, "recover-report", "APP-A-SMOKE-001")
        self.assertEqual(activity["status"], "ready_for_bundle_review")
        self.assertIsNotNone(activity["recovered_at"])
        self.assertEqual(activity["recovery_action"], "validated_artifact")

    def test_reconcile_run_preserves_blocked_status_from_existing_report(self) -> None:
        scaffold_smoke_test(self.project_root, "recover-blocked")
        write_managed_report(
            self.project_root,
            "recover-blocked",
            "APP-A-SMOKE-001",
            status="blocked",
            summary="Recovered blocked report.",
        )
        ensure_activity(
            self.project_root,
            "recover-blocked",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="finalizing",
            progress_stage="finalizing",
            current_activity="Codex turn completed.",
            prompt_path="runs/recover-blocked/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/recover-blocked/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/recover-blocked/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/recover-blocked/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            process_metadata={"pid": 999999, "started_at": "2026-03-10T00:00:00Z", "command": "codex exec", "cwd": str(self.project_root)},
        )
        reconcile_run(self.project_root, "recover-blocked", apply=True)
        activity = read_activity(self.project_root, "recover-blocked", "APP-A-SMOKE-001")
        self.assertEqual(activity["status"], "blocked")
        self.assertIsNotNone(activity["recovered_at"])
        self.assertEqual(activity["recovery_action"], "validated_artifact")

    def test_reconcile_for_command_allows_recoverable_blocked_task_report(self) -> None:
        scaffold_smoke_test(self.project_root, "recover-blocked-command")
        write_managed_report(
            self.project_root,
            "recover-blocked-command",
            "APP-A-SMOKE-001",
            status="blocked",
            summary="Recoverable blocked task report.",
        )
        ensure_activity(
            self.project_root,
            "recover-blocked-command",
            activity_id="APP-A-SMOKE-001",
            kind="task_execution",
            entity_id="APP-A-SMOKE-001",
            phase="discovery",
            objective_id="app-a",
            display_name="APP-A-SMOKE-001",
            assigned_role="objectives.app-a.frontend-worker",
            status="finalizing",
            progress_stage="finalizing",
            current_activity="Codex turn completed.",
            prompt_path="runs/recover-blocked-command/prompt-logs/APP-A-SMOKE-001.prompt.md",
            stdout_path="runs/recover-blocked-command/executions/APP-A-SMOKE-001.stdout.jsonl",
            stderr_path="runs/recover-blocked-command/executions/APP-A-SMOKE-001.stderr.log",
            output_path="runs/recover-blocked-command/reports/APP-A-SMOKE-001.json",
            dependency_blockers=[],
            process_metadata={"pid": 999999, "started_at": "2026-03-10T00:00:00Z", "command": "codex exec", "cwd": str(self.project_root)},
        )
        summary = reconcile_for_command(self.project_root, "recover-blocked-command", apply=True)
        self.assertFalse(summary["blocked"])
        activity = read_activity(self.project_root, "recover-blocked-command", "APP-A-SMOKE-001")
        self.assertEqual(activity["status"], "blocked")

    def test_infer_execution_metadata_treats_discovery_file_outputs_as_writes(self) -> None:
        inferred = infer_execution_metadata(
            phase="discovery",
            task_id="DISC-WRITE-001",
            expected_outputs=["apps/todo/docs/objectives/app-a/discovery-note.md"],
        )
        self.assertEqual(inferred["execution_mode"], "isolated_write")
        self.assertEqual(inferred["parallel_policy"], "serialize")
        self.assertEqual(inferred["owned_paths"], ["apps/todo/docs/objectives/app-a/discovery-note.md"])

    def test_plan_objective_reuses_valid_outline_and_capability_plan_artifacts(self) -> None:
        scaffold_planning_run(self.project_root, "recover-plan", ["frontend"])
        plans_dir = self.project_root / "runs" / "recover-plan" / "manager-plans"
        write_json(
            plans_dir / "discovery-app-a.outline.json",
            objective_outline_for_objective("recover-plan", "app-a", ["frontend"]),
        )
        write_json(
            plans_dir / "discovery-app-a-frontend.json",
            capability_plan_for_objective("recover-plan", "app-a", "frontend"),
        )
        with patch("company_orchestrator.objective_planner.run_codex_command") as planner:
            summary = plan_objective(self.project_root, "recover-plan", "app-a")
        planner.assert_not_called()
        self.assertEqual(summary["recovery_action"], "reused_valid_outline")
        self.assertEqual(summary["capability_summaries"][0]["recovery_action"], "reused_valid_capability_plan")
        self.assertTrue((self.project_root / "runs" / "recover-plan" / "tasks" / "APP-A-FRONTEND-001.json").exists())

    def test_plan_objective_reuses_capability_last_message_when_plan_json_missing(self) -> None:
        scaffold_planning_run(self.project_root, "recover-capability-last-message", ["frontend"])
        plans_dir = self.project_root / "runs" / "recover-capability-last-message" / "manager-plans"
        write_json(
            plans_dir / "discovery-app-a.outline.json",
            objective_outline_for_objective("recover-capability-last-message", "app-a", ["frontend"]),
        )
        capability_plan = capability_plan_for_objective("recover-capability-last-message", "app-a", "frontend")
        write_json(
            plans_dir / "discovery-app-a-frontend.last-message.json",
            capability_plan,
        )
        with patch("company_orchestrator.objective_planner.run_codex_command") as planner:
            summary = plan_objective(self.project_root, "recover-capability-last-message", "app-a")
        planner.assert_not_called()
        self.assertEqual(
            summary["capability_summaries"][0]["recovery_action"],
            "reused_last_message_capability_plan",
        )
        recovered_plan = read_json(plans_dir / "discovery-app-a-frontend.json")
        self.assertEqual(recovered_plan["schema"], "capability-plan.v1")

    def test_execute_task_retry_refreshes_existing_isolated_worktree(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "retry-write", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "retry-write",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["docs/retry.md"],
            "shared_asset_ids": [],
            "task_id": "WRITE-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Write something in an isolated workspace.",
            "inputs": [],
            "expected_outputs": ["docs/retry.md"],
            "done_when": ["task complete"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(self.project_root / "runs" / "retry-write" / "tasks" / "WRITE-001.json", task)
        final_payload = {
            "summary": "Finished the isolated write task.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "docs/retry.md", "status": "updated"}],
            "validation_results": [],
            "legacy_dependency_notes": [],
            "open_issues": [],
            "legacy_follow_ups": [],
            "context_echo": None,
            "collaboration_request": None,
        }
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-write"}',
                '{"type":"turn.started"}',
                json_line_event("item.completed", {"id": "item_0", "type": "agent_message", "text": json.dumps(final_payload)}),
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}',
            ]
        )
        completed = completed_process(stdout=stdout, stderr="", returncode=0)
        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            execute_task(self.project_root, "retry-write", "WRITE-001")

        (self.project_root / "runs" / "retry-write" / "reports" / "WRITE-001.json").unlink()
        update_activity(
            self.project_root,
            "retry-write",
            "WRITE-001",
            status="interrupted",
            progress_stage="interrupted",
            status_reason="Simulated interruption for retry.",
        )

        with patch("company_orchestrator.executor.run_codex_command", return_value=completed):
            summary = execute_task(self.project_root, "retry-write", "WRITE-001")

        self.assertEqual(summary["attempt"], 2)
        self.assertEqual(summary["recovery_action"], "refreshed_workspace")
        self.assertFalse(summary["workspace_reused"])

    def test_prepare_task_runtime_refreshes_retry_workspace_from_integration_branch(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "retry-refresh", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "retry-refresh",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["docs/retry-refresh.md"],
            "shared_asset_ids": [],
            "task_id": "WRITE-REFRESH-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Verify retry refreshes the isolated workspace.",
            "inputs": [],
            "expected_outputs": ["docs/retry-refresh.md"],
            "done_when": ["workspace is ready"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }

        first_runtime = prepare_task_runtime(
            self.project_root,
            "retry-refresh",
            task,
            runtime=TaskExecutionRuntime(attempt=1),
        )
        first_workspace = Path(first_runtime.workspace_path or "")
        self.assertTrue(first_workspace.exists())
        self.assertFalse((first_workspace / "docs" / "handoffs" / "backend-contract.json").exists())

        integration_workspace = ensure_run_integration_workspace(self.project_root, "retry-refresh")
        updated_payload = {
            "schema": "handoff-payload.v1",
            "summary": "Fresh handoff from the integration branch.",
        }
        write_json(integration_workspace.workspace_path / "docs" / "handoffs" / "backend-contract.json", updated_payload)
        subprocess.run(
            ["git", "add", "-A"],
            cwd=integration_workspace.workspace_path,
            capture_output=True,
            check=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "integration update"],
            cwd=integration_workspace.workspace_path,
            capture_output=True,
            check=True,
            text=True,
        )

        retry_runtime = prepare_task_runtime(
            self.project_root,
            "retry-refresh",
            task,
            runtime=TaskExecutionRuntime(attempt=2),
        )

        refreshed_artifact = Path(retry_runtime.workspace_path or "") / "docs" / "handoffs" / "backend-contract.json"
        self.assertEqual(retry_runtime.recovery_action, "refreshed_workspace")
        self.assertFalse(retry_runtime.workspace_reused)
        self.assertTrue(refreshed_artifact.exists())
        self.assertEqual(read_json(refreshed_artifact), updated_payload)

    def test_prepare_task_runtime_creates_read_workspace_for_output_dependent_read_only_task(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "read-deps", ["frontend"])

        upstream_workspace = ensure_task_workspace(self.project_root, "read-deps", "UPSTREAM-001")
        upstream_artifact = upstream_workspace.workspace_path / "docs" / "handoffs" / "contract.md"
        upstream_artifact.parent.mkdir(parents=True, exist_ok=True)
        upstream_artifact.write_text("upstream handoff\n", encoding="utf-8")

        write_json(
            self.project_root / "runs" / "read-deps" / "reports" / "UPSTREAM-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "read-deps",
                "phase": "design",
                "objective_id": "app-a",
                "task_id": "UPSTREAM-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Upstream handoff ready.",
                "artifacts": [{"path": "docs/handoffs/contract.md", "status": "created"}],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            self.project_root / "runs" / "read-deps" / "executions" / "UPSTREAM-001.json",
            {
                "task_id": "UPSTREAM-001",
                "workspace_path": str(upstream_workspace.workspace_path),
            },
        )

        downstream_task = {
            "schema": "task-assignment.v1",
            "run_id": "read-deps",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "read_only",
            "parallel_policy": "serialize",
            "owned_paths": [],
            "shared_asset_ids": [],
            "task_id": "READ-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Package upstream handoff context without writing files.",
            "inputs": ["Output of UPSTREAM-001"],
            "expected_outputs": ["shared_asset:app-a:frontend:handoff"],
            "done_when": ["handoff package is prepared"],
            "depends_on": ["UPSTREAM-001"],
            "validation": [{"id": "upstream-contract-present", "command": "test -f docs/handoffs/contract.md"}],
            "collaboration_rules": [],
            "handoff_dependencies": [],
        }

        runtime = prepare_task_runtime(
            self.project_root,
            "read-deps",
            downstream_task,
            runtime=TaskExecutionRuntime(attempt=1),
        )

        self.assertIsNotNone(runtime.workspace_path)
        workspace_path = Path(runtime.workspace_path or "")
        self.assertTrue(workspace_path.exists())

        materialize_task_context_files(self.project_root, "read-deps", downstream_task, workspace_path)

        mirrored_artifact = workspace_path / "docs" / "handoffs" / "contract.md"
        self.assertTrue(mirrored_artifact.exists())
        self.assertEqual(mirrored_artifact.read_text(encoding="utf-8"), "upstream handoff\n")

    def test_prepare_task_runtime_creates_read_workspace_for_explicit_file_input_task(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "read-explicit-inputs", ["backend"])

        integration_workspace = ensure_run_integration_workspace(self.project_root, "read-explicit-inputs")
        contract_path = integration_workspace.workspace_path / "apps" / "todo" / "backend" / "design" / "todo-api-contract.yaml"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text("openapi: 3.1.0\ninfo:\n  title: Todo API\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=integration_workspace.workspace_path,
            capture_output=True,
            check=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "integration artifacts"],
            cwd=integration_workspace.workspace_path,
            capture_output=True,
            check=True,
            text=True,
        )

        task = {
            "schema": "task-assignment.v1",
            "run_id": "read-explicit-inputs",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "backend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
            "task_id": "READ-EXPLICIT-001",
            "assigned_role": "objectives.app-a.backend-worker",
            "manager_role": "objectives.app-a.backend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Inspect the landed backend API contract artifact.",
            "inputs": ["apps/todo/backend/design/todo-api-contract.yaml"],
            "expected_outputs": [],
            "done_when": ["the canonical contract input is readable from the task workspace"],
            "depends_on": [],
            "validation": [
                {
                    "id": "contract_present",
                    "command": "test -f apps/todo/backend/design/todo-api-contract.yaml",
                }
            ],
            "collaboration_rules": [],
            "handoff_dependencies": [],
        }

        runtime = prepare_task_runtime(
            self.project_root,
            "read-explicit-inputs",
            task,
            runtime=TaskExecutionRuntime(attempt=1),
        )

        self.assertIsNotNone(runtime.workspace_path)
        workspace_path = Path(runtime.workspace_path or "")
        self.assertTrue(workspace_path.exists())
        mirrored_contract = workspace_path / "apps" / "todo" / "backend" / "design" / "todo-api-contract.yaml"
        self.assertTrue(mirrored_contract.exists())
        self.assertEqual(mirrored_contract.read_text(encoding="utf-8"), "openapi: 3.1.0\ninfo:\n  title: Todo API\n")

    def test_materialize_task_context_files_mirrors_explicit_run_inputs_into_workspace(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "explicit-inputs", ["frontend"])
        phase_report_path = self.project_root / "runs" / "explicit-inputs" / "phase-reports" / "discovery.md"
        phase_report_path.parent.mkdir(parents=True, exist_ok=True)
        phase_report_path.write_text("# Discovery\n\nphase report\n", encoding="utf-8")

        task = {
            "schema": "task-assignment.v1",
            "run_id": "explicit-inputs",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": [],
            "shared_asset_ids": [],
            "task_id": "READ-RUN-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Inspect a phase report from the run folder.",
            "inputs": ["runs/explicit-inputs/phase-reports/discovery.md"],
            "expected_outputs": [],
            "done_when": ["phase report is available in the workspace"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": [],
        }

        runtime = prepare_task_runtime(
            self.project_root,
            "explicit-inputs",
            task,
            runtime=TaskExecutionRuntime(attempt=1),
        )

        self.assertIsNotNone(runtime.workspace_path)
        workspace_path = Path(runtime.workspace_path or "")
        materialize_task_context_files(self.project_root, "explicit-inputs", task, workspace_path)

        mirrored_phase_report = workspace_path / "runs" / "explicit-inputs" / "phase-reports" / "discovery.md"
        self.assertTrue(mirrored_phase_report.exists())
        self.assertEqual(mirrored_phase_report.read_text(encoding="utf-8"), "# Discovery\n\nphase report\n")

    def test_materialize_task_context_files_mirrors_release_repair_inputs_and_integration_workspace_sources(self) -> None:
        init_git_repo(self.project_root)
        run_id = "release-repair-workspace-inputs"
        scaffold_planning_run(self.project_root, run_id, ["middleware"])
        run_dir = self.project_root / "runs" / run_id

        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "polish"
        for item in phase_plan["phases"]:
            item["status"] = "complete" if item["phase"] != "polish" else "active"
        write_json(run_dir / "phase-plan.json", phase_plan)

        outline = objective_outline_for_objective(run_id, "app-a", ["middleware"])
        outline["phase"] = "polish"
        write_json(run_dir / "manager-plans" / "polish-app-a.outline.json", outline)

        integration_workspace = ensure_run_integration_workspace(self.project_root, run_id)
        contract_path = (
            integration_workspace.workspace_path
            / "apps"
            / "todo"
            / "backend"
            / "design"
            / "todo-api-contract.yaml"
        )
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text("openapi: 3.1.0\ninfo:\n  title: Todo API\n", encoding="utf-8")
        middleware_contract_path = (
            integration_workspace.workspace_path
            / "apps"
            / "todo"
            / "orchestrator"
            / "roles"
            / "objectives"
            / "app-a"
            / "approved"
            / "middleware-mvp-build-integration-contract.md"
        )
        middleware_contract_path.parent.mkdir(parents=True, exist_ok=True)
        middleware_contract_path.write_text(
            "# Middleware MVP Build Integration Contract\n\nThis is the authoritative markdown contract.\n",
            encoding="utf-8",
        )

        write_json(
            run_dir / "reports" / "middleware-mvp-delivery-package.json",
            {
                "schema": "completion-report.v1",
                "run_id": run_id,
                "phase": "mvp-build",
                "objective_id": "app-a",
                "task_id": "middleware-mvp-delivery-package",
                "agent_role": "objectives.app-a.middleware-worker",
                "status": "ready_for_bundle_review",
                "summary": "Middleware MVP delivery report.",
                "artifacts": [],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
            },
        )
        write_json(
            run_dir / "bundles" / "app-a-middleware-mvp-build-lane.json",
            {
                "bundle_id": "app-a-middleware-mvp-build-lane",
                "phase": "mvp-build",
                "objective_id": "app-a",
                "status": "accepted",
                "included_tasks": ["middleware-mvp-delivery-package"],
                "rejection_reasons": [],
            },
        )

        task = {
            "schema": "task-assignment.v1",
            "run_id": run_id,
            "phase": "polish",
            "objective_id": "app-a",
            "capability": "middleware",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "read_only",
            "parallel_policy": "serialize",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
            "task_id": "MID-POLISH-001",
            "assigned_role": "objectives.app-a.middleware-worker",
            "manager_role": "objectives.app-a.middleware-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Inspect inherited polish repair inputs.",
            "inputs": [
                "Planning Inputs.release_repair_inputs.report_middleware_mvp_delivery_package.path",
                "Planning Inputs.release_repair_inputs.artifact_middleware_mvp_build_integration_contract.path",
                "apps/todo/backend/design/todo-api-contract.yaml",
            ],
            "expected_outputs": [],
            "done_when": ["release repair inputs are readable from the task workspace"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": [],
        }

        resolved_inputs = preview_resolved_inputs(self.project_root, run_id, task)
        planning_input_payload = resolved_inputs[
            "Planning Inputs.release_repair_inputs.report_middleware_mvp_delivery_package.path"
        ]
        self.assertEqual(
            planning_input_payload["path"],
            f"runs/{run_id}/reports/middleware-mvp-delivery-package.json",
        )
        self.assertEqual(planning_input_payload["content"]["schema"], "completion-report.v1")
        contract_input_payload = resolved_inputs[
            "Planning Inputs.release_repair_inputs.artifact_middleware_mvp_build_integration_contract.path"
        ]
        self.assertEqual(
            contract_input_payload["path"],
            "apps/todo/orchestrator/roles/objectives/app-a/approved/middleware-mvp-build-integration-contract.md",
        )
        self.assertIn("# Middleware MVP Build Integration Contract", contract_input_payload["content"])
        self.assertIn("openapi: 3.1.0", resolved_inputs["apps/todo/backend/design/todo-api-contract.yaml"])

        runtime = prepare_task_runtime(
            self.project_root,
            run_id,
            task,
            runtime=TaskExecutionRuntime(attempt=1),
        )

        workspace_path = Path(runtime.workspace_path or "")
        self.assertTrue(workspace_path.exists())

        materialize_task_context_files(self.project_root, run_id, task, workspace_path)

        mirrored_report = workspace_path / "runs" / run_id / "reports" / "middleware-mvp-delivery-package.json"
        mirrored_markdown_contract = (
            workspace_path
            / "apps"
            / "todo"
            / "orchestrator"
            / "roles"
            / "objectives"
            / "app-a"
            / "approved"
            / "middleware-mvp-build-integration-contract.md"
        )
        mirrored_contract = workspace_path / "apps" / "todo" / "backend" / "design" / "todo-api-contract.yaml"
        self.assertTrue(mirrored_report.exists())
        self.assertTrue(mirrored_markdown_contract.exists())
        self.assertTrue(mirrored_contract.exists())
        self.assertEqual(read_json(mirrored_report)["schema"], "completion-report.v1")
        self.assertIn("# Middleware MVP Build Integration Contract", mirrored_markdown_contract.read_text(encoding="utf-8"))
        self.assertIn("openapi: 3.1.0", mirrored_contract.read_text(encoding="utf-8"))

    def test_materialize_task_context_files_precreates_declared_output_parent_directories(self) -> None:
        scaffold_planning_run(self.project_root, "precreate-output-dirs", ["frontend"])
        workspace_path = self.project_root / "workspace"
        workspace_path.mkdir()
        task = {
            "schema": "task-assignment.v1",
            "run_id": "precreate-output-dirs",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
            "task_id": "PRECREATE-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Emit the declared discovery bundle directly.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.discovery.bundle",
                    "path": "apps/todo/frontend/docs/discovery/bundle.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": ["bundle path is ready for direct authoring"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": [],
        }

        materialize_task_context_files(self.project_root, "precreate-output-dirs", task, workspace_path)

        self.assertTrue((workspace_path / "apps" / "todo" / "frontend" / "docs" / "discovery").exists())
        self.assertFalse((workspace_path / "apps" / "todo" / "frontend" / "docs" / "discovery" / "bundle.md").exists())

    def test_materialize_task_context_files_mirrors_handoff_artifacts_from_multiple_reports(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "handoff-artifact-materialization", ["frontend", "middleware"])

        frontend_contract_workspace = ensure_task_workspace(
            self.project_root,
            "handoff-artifact-materialization",
            "FRONT-001",
        )
        frontend_contract = (
            frontend_contract_workspace.workspace_path / "apps" / "todo" / "frontend" / "frontend-todo-integration.v1.json"
        )
        frontend_contract.parent.mkdir(parents=True, exist_ok=True)
        frontend_contract.write_text('{"artifact":"contract"}\n', encoding="utf-8")

        frontend_flow = frontend_contract_workspace.workspace_path / "apps" / "todo" / "frontend" / "frontend-ui-flow.v1.json"
        frontend_flow.parent.mkdir(parents=True, exist_ok=True)
        frontend_flow.write_text('{"artifact":"ui-flow"}\n', encoding="utf-8")

        write_json(
            self.project_root / "runs" / "handoff-artifact-materialization" / "executions" / "FRONT-001.json",
            {
                "task_id": "FRONT-001",
                "workspace_path": str(frontend_contract_workspace.workspace_path),
            },
        )

        write_json(
            self.project_root / "runs" / "handoff-artifact-materialization" / "reports" / "FRONT-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "handoff-artifact-materialization",
                "phase": "design",
                "objective_id": "app-a",
                "task_id": "FRONT-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Frontend handoff bundle ready.",
                "artifacts": [
                    {"path": "apps/todo/frontend/frontend-todo-integration.v1.json", "status": "created"},
                    {"path": "apps/todo/frontend/frontend-ui-flow.v1.json", "status": "created"},
                ],
                "produced_outputs": [
                    {
                        "kind": "asset",
                        "output_id": "asset.contract.frontend-todo-integration.v1",
                        "path": "apps/todo/frontend/frontend-todo-integration.v1.json",
                        "asset_id": "asset.contract.frontend-todo-integration.v1",
                        "description": None,
                        "evidence": None,
                    },
                    {
                        "kind": "asset",
                        "output_id": "asset.contract.frontend-ui-flow.v1",
                        "path": "apps/todo/frontend/frontend-ui-flow.v1.json",
                        "asset_id": "asset.contract.frontend-ui-flow.v1",
                        "description": None,
                        "evidence": None,
                    },
                ],
                "validation_results": [],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        write_json(
            self.project_root
            / "runs"
            / "handoff-artifact-materialization"
            / "collaboration-plans"
            / "app-a-frontend-to-middleware.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "handoff-artifact-materialization",
                "phase": "design",
                "objective_id": "app-a",
                "handoff_id": "app-a-frontend-to-middleware",
                "from_capability": "frontend",
                "to_capability": "middleware",
                "from_task_id": "FRONT-001",
                "to_role": "objectives.app-a.middleware-manager",
                "handoff_type": "interface_handoff",
                "reason": "Middleware needs the finalized frontend contract and UI flow.",
                "deliverables": [
                    {
                        "kind": "asset",
                        "output_id": "asset.contract.frontend-todo-integration.v1",
                        "path": "apps/todo/frontend/frontend-todo-integration.v1.json",
                        "asset_id": "asset.contract.frontend-todo-integration.v1",
                        "description": None,
                        "evidence": None,
                    },
                    {
                        "kind": "asset",
                        "output_id": "asset.contract.frontend-ui-flow.v1",
                        "path": "apps/todo/frontend/frontend-ui-flow.v1.json",
                        "asset_id": "asset.contract.frontend-ui-flow.v1",
                        "description": None,
                        "evidence": None,
                    },
                ],
                "blocking": True,
                "shared_asset_ids": [],
                "status": "satisfied",
                "status_reason": "Frontend artifacts are ready.",
                "to_task_ids": ["MID-001"],
                "satisfied_by_task_ids": ["FRONT-001"],
                "missing_deliverables": [],
                "last_checked_at": None,
            },
        )

        downstream_task = {
            "schema": "task-assignment.v1",
            "run_id": "handoff-artifact-materialization",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "middleware",
            "working_directory": None,
            "sandbox_mode": "read-only",
            "additional_directories": [],
            "execution_mode": "read_only",
            "parallel_policy": "serialize",
            "owned_paths": [],
            "shared_asset_ids": [],
            "task_id": "MID-001",
            "assigned_role": "objectives.app-a.middleware-worker",
            "manager_role": "objectives.app-a.middleware-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Review upstream frontend handoff artifacts.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": ["Both frontend artifacts are available in the workspace"],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": ["app-a-frontend-to-middleware"],
        }

        runtime = prepare_task_runtime(
            self.project_root,
            "handoff-artifact-materialization",
            downstream_task,
            runtime=TaskExecutionRuntime(attempt=1),
        )

        workspace_path = Path(runtime.workspace_path or "")
        self.assertTrue(workspace_path.exists())

        materialize_task_context_files(
            self.project_root,
            "handoff-artifact-materialization",
            downstream_task,
            workspace_path,
        )

        mirrored_contract = workspace_path / "apps" / "todo" / "frontend" / "frontend-todo-integration.v1.json"
        mirrored_flow = workspace_path / "apps" / "todo" / "frontend" / "frontend-ui-flow.v1.json"
        self.assertTrue(mirrored_contract.exists())
        self.assertTrue(mirrored_flow.exists())
        self.assertEqual(mirrored_contract.read_text(encoding="utf-8"), '{"artifact":"contract"}\n')
        self.assertEqual(mirrored_flow.read_text(encoding="utf-8"), '{"artifact":"ui-flow"}\n')

    def test_run_phase_stops_when_recovery_detects_blocked_bundle_landing(self) -> None:
        init_git_repo(self.project_root)
        scaffold_planning_run(self.project_root, "blocked-recovery", ["frontend"])
        write_json(
            self.project_root / "runs" / "blocked-recovery" / "tasks" / "WRITE-001.json",
            {
                "schema": "task-assignment.v1",
                "run_id": "blocked-recovery",
                "phase": "discovery",
                "objective_id": "app-a",
                "capability": "frontend",
                "working_directory": None,
                "sandbox_mode": "read-only",
                "additional_directories": [],
                "execution_mode": "isolated_write",
                "parallel_policy": "serialize",
                "owned_paths": ["docs/conflict.md"],
                "shared_asset_ids": [],
                "task_id": "WRITE-001",
                "assigned_role": "objectives.app-a.frontend-worker",
                "manager_role": "objectives.app-a.frontend-manager",
                "acceptance_role": "objectives.app-a.acceptance-manager",
                "objective": "Write a conflict file.",
                "inputs": [],
                "expected_outputs": ["docs/conflict.md"],
                "done_when": ["task complete"],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
            },
        )
        write_json(
            self.project_root / "runs" / "blocked-recovery" / "bundles" / "blocked-bundle.json",
            {
                "schema": "review-bundle.v1",
                "run_id": "blocked-recovery",
                "phase": "discovery",
                "objective_id": "app-a",
                "bundle_id": "blocked-bundle",
                "assembled_by": "objectives.app-a.frontend-manager",
                "reviewed_by": "objectives.app-a.acceptance-manager",
                "included_tasks": ["WRITE-001"],
                "status": "accepted",
                "required_checks": [],
            },
        )

        with self.assertRaises(RecoveryBlockedError):
            run_phase(self.project_root, "blocked-recovery")

        recovery_summary = read_json(self.project_root / "runs" / "blocked-recovery" / "recovery" / "blocked-bundle.json")
        self.assertEqual(recovery_summary["status"], "interrupted")

    def test_phase_report_recovery_summary_includes_blocked_bundle_incidents(self) -> None:
        scaffold_smoke_test(self.project_root, "recovery-report")
        record_event(
            self.project_root,
            "recovery-report",
            phase="discovery",
            activity_id=None,
            event_type="bundle.recovery_blocked",
            message="Bundle landing requires recovery.",
            payload={"bundle_id": "bundle-1", "recovery_path": "runs/recovery-report/recovery/bundle-1.json"},
        )
        report, _ = generate_phase_report(self.project_root, "recovery-report")
        incidents = report["recovery_summary"]["incidents"]
        self.assertIn(
            {
                "activity_id": "bundle:bundle-1",
                "status": "blocked",
                "reason": "Bundle landing requires recovery.",
                "artifact_path": "runs/recovery-report/recovery/bundle-1.json",
            },
            incidents,
        )

    def test_reconcile_for_command_ignores_blocked_bundles_without_landing_results(self) -> None:
        scaffold_smoke_test(self.project_root, "ignore-blocked-bundle")
        run_dir = self.project_root / "runs" / "ignore-blocked-bundle"
        write_json(
            run_dir / "tasks" / "WRITE-001.json",
            {
                "schema": "task-assignment.v1",
                "run_id": "ignore-blocked-bundle",
                "phase": "discovery",
                "objective_id": "app-a",
                "capability": "frontend",
                "working_directory": None,
                "sandbox_mode": "read-only",
                "additional_directories": [],
                "execution_mode": "isolated_write",
                "parallel_policy": "serialize",
                "owned_paths": ["docs/blocked.md"],
                "shared_asset_ids": [],
                "task_id": "WRITE-001",
                "assigned_role": "objectives.app-a.frontend-worker",
                "manager_role": "objectives.app-a.frontend-manager",
                "acceptance_role": "objectives.app-a.acceptance-manager",
                "objective": "Write a blocked file.",
                "inputs": [],
                "expected_outputs": ["docs/blocked.md"],
                "done_when": ["task complete"],
                "depends_on": [],
                "validation": [],
                "collaboration_rules": [],
            },
        )
        write_json(
            run_dir / "bundles" / "blocked-bundle.json",
            {
                "schema": "review-bundle.v1",
                "run_id": "ignore-blocked-bundle",
                "phase": "discovery",
                "objective_id": "app-a",
                "bundle_id": "blocked-bundle",
                "assembled_by": "objectives.app-a.frontend-manager",
                "reviewed_by": "objectives.app-a.acceptance-manager",
                "included_tasks": ["WRITE-001"],
                "status": "blocked",
                "required_checks": [],
                "rejection_reasons": ["stale blocked bundle"],
            },
        )

        summary = reconcile_for_command(self.project_root, "ignore-blocked-bundle", apply=True)
        self.assertEqual(summary["blocked"], [])
        self.assertFalse((run_dir / "recovery" / "blocked-bundle.json").exists())

    def test_task_assignment_accepts_structured_expected_outputs(self) -> None:
        task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-task",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a frontend contract and prove it validates.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.contract.doc",
                    "path": "docs/contracts/frontend-api.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "assertion",
                    "output_id": "frontend.contract.validated",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend contract validation passed.",
                    "evidence": {
                        "validation_ids": ["contract-check"],
                        "artifact_paths": ["docs/contracts/frontend-api.md"],
                    },
                },
            ],
            "done_when": ["task complete"],
            "depends_on": [],
            "validation": [{"id": "contract-check", "command": "test-contract"}],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["docs/contracts/frontend-api.md"],
            "shared_asset_ids": [],
        }

        validate_document(task, "task-assignment.v1", self.project_root)

    def test_materialize_executor_response_records_structured_produced_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "structured-produced-outputs", ["frontend"])
        artifact_path = self.project_root / "docs" / "handoffs" / "frontend-middleware.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("handoff", encoding="utf-8")
        task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-produced-outputs",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a discovery handoff package.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.handoff.doc",
                    "path": "docs/handoffs/frontend-middleware.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "assertion",
                    "output_id": "frontend.handoff.validated",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend handoff validation passed.",
                    "evidence": {
                        "validation_ids": ["handoff-check"],
                        "artifact_paths": ["docs/handoffs/frontend-middleware.md"],
                    },
                },
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [{"id": "handoff-check", "command": "validate-handoff"}],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Produced the handoff package.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "docs/handoffs/frontend-middleware.md", "status": "created"}],
            "validation_results": [{"id": "handoff-check", "status": "passed", "evidence": "handoff validated"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.handoff.doc",
                    "path": "docs/handoffs/frontend-middleware.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "assertion",
                    "output_id": "frontend.handoff.validated",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend handoff validation passed.",
                    "evidence": {
                        "validation_ids": ["handoff-check"],
                        "artifact_paths": ["docs/handoffs/frontend-middleware.md"],
                    },
                },
            ],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "discovery",
                "prompt_layers": ["orchestrator/roles/base/company.md"],
                "schema": "task-assignment.v1",
            },
            "collaboration_request": None,
        }

        report, _, _ = materialize_executor_response(
            self.project_root,
            "structured-produced-outputs",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(report["produced_outputs"], parsed_response["produced_outputs"])

    def test_materialize_executor_response_persists_structured_change_requests(self) -> None:
        scaffold_planning_run(self.project_root, "structured-change-requests", ["frontend"])
        run_dir = self.project_root / "runs" / "structured-change-requests"
        backend_task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-change-requests",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce the shared backend API contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(run_dir / "tasks" / "APP-B-BACK-001.json", backend_task)
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "structured-change-requests",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced backend contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        write_json(
            run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "structured-change-requests",
                "phase": "design",
                "objective_id": "app-b",
                "handoff_id": "handoff.backend-to-frontend",
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-B-BACK-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "interface_handoff",
                "reason": "Frontend consumes backend contract.",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["todo:api-contract"],
                "status": "planned",
                "to_task_ids": ["APP-A-FRONTEND-CHANGE-001"],
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-change-requests",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-CHANGE-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Validate the shared API contract usage.",
            "inputs": ["apps/todo/backend/design/api-contract.md"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        parsed_response = {
            "summary": "Blocked on an approved cross-boundary contract change.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "open_issues": ["Blocking: shared API contract must change before this task can complete."],
            "change_requests": [
                {
                    "change_category": "interface_contract",
                    "summary": "Align the request schema with the canonical backend contract.",
                    "blocking_reason": "The injected API contract and backend validation rules disagree on request fields.",
                    "why_local_resolution_is_invalid": "Completing this task locally would fork the shared API contract used by sibling objectives.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": [],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": [],
                    "impacted_task_ids": [],
                    "conflicting_input_refs": ["apps/todo/backend/design/api-contract.md"],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": False,
                    },
                }
            ],
            "produced_outputs": [],
            "context_echo": None,
            "collaboration_request": None,
        }

        report, _, change_request_ids = materialize_executor_response(
            self.project_root,
            "structured-change-requests",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(len(change_request_ids), 1)
        self.assertEqual(len(report["change_requests"]), 1)
        self.assertEqual(report["change_requests"][0]["approval"], {"mode": "auto", "status": "approved"})
        persisted = read_json(
            self.project_root
            / "runs"
            / "structured-change-requests"
            / "change-requests"
            / f"{change_request_ids[0]}.json"
        )
        self.assertEqual(persisted["source_task_id"], "APP-A-FRONTEND-CHANGE-001")
        self.assertEqual(persisted["change_category"], "interface_contract")
        self.assertEqual(persisted["affected_output_ids"], ["todo-api-contract"])
        self.assertEqual(persisted["affected_handoff_ids"], ["handoff.backend-to-frontend"])

    def test_materialize_executor_response_rejects_self_targeted_conflicting_input_refs(self) -> None:
        scaffold_planning_run(self.project_root, "self-targeted-change-request", ["frontend"])
        run_dir = self.project_root / "runs" / "self-targeted-change-request"
        task = {
            "schema": "task-assignment.v1",
            "run_id": "self-targeted-change-request",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-CHANGE-SELF",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Author frontend contract notes.",
            "inputs": ["apps/todo/frontend/design/frontend-consumer-contract.md"],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend-consumer-contract",
                    "path": "apps/todo/frontend/design/frontend-consumer-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        parsed_response = {
            "summary": "Blocked on a shared contract change.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "open_issues": ["Blocking: shared contract drift remains."],
            "change_requests": [
                {
                    "change_category": "interface_contract",
                    "summary": "Change the contract.",
                    "blocking_reason": "The contract conflicts.",
                    "why_local_resolution_is_invalid": "A local fix would fork the shared contract.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": [],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": [],
                    "impacted_task_ids": [],
                    "conflicting_input_refs": ["apps/todo/frontend/design/frontend-consumer-contract.md"],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": False,
                    },
                }
            ],
            "produced_outputs": [],
            "context_echo": None,
            "collaboration_request": None,
        }

        with self.assertRaisesRegex(ExecutorError, "cited only self-authored inputs"):
            materialize_executor_response(
                self.project_root,
                "self-targeted-change-request",
                task,
                parsed_response,
                runtime_warnings=[],
                runtime_recovery=None,
                runtime_observability=None,
            )

    def test_conflicting_input_refs_drive_true_consumer_impact_resolution(self) -> None:
        scaffold_dual_planning_run(self.project_root, "resolved-impact-graph")
        run_dir = self.project_root / "runs" / "resolved-impact-graph"
        producer_task = {
            "schema": "task-assignment.v1",
            "run_id": "resolved-impact-graph",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce the shared backend API contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["apps/todo/backend/design/api-contract.md"],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
        }
        source_task = {
            "schema": "task-assignment.v1",
            "run_id": "resolved-impact-graph",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume backend contract.",
            "inputs": ["apps/todo/backend/design/api-contract.md"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-B-BACK-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        downstream_task = {
            "schema": "task-assignment.v1",
            "run_id": "resolved-impact-graph",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-002",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Downstream frontend work.",
            "inputs": ["Output of APP-A-FRONT-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-A-FRONT-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        unrelated_task = {
            "schema": "task-assignment.v1",
            "run_id": "resolved-impact-graph",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-002",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Unrelated backend task.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        for task_payload in [producer_task, source_task, downstream_task, unrelated_task]:
            write_json(run_dir / "tasks" / f"{task_payload['task_id']}.json", task_payload)
        write_json(
            run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "resolved-impact-graph",
                "phase": "design",
                "objective_id": "app-b",
                "handoff_id": "handoff.backend-to-frontend",
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-B-BACK-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "interface_handoff",
                "reason": "Frontend consumes backend contract.",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["todo:api-contract"],
                "status": "planned",
                "to_task_ids": ["APP-A-FRONT-001"],
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "resolved-impact-graph",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced backend contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )

        parsed_response = {
            "summary": "Blocked on shared contract drift.",
            "status": "blocked",
            "artifacts": [],
            "validation_results": [],
            "open_issues": ["Blocking: shared contract drift remains."],
            "change_requests": [
                {
                    "change_category": "interface_contract",
                    "summary": "Shared API contract must change.",
                    "blocking_reason": "The backend contract conflicts with approved frontend behavior.",
                    "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
                    "blocking": True,
                    "goal_critical": True,
                    "affected_output_ids": [],
                    "affected_handoff_ids": [],
                    "impacted_objective_ids": [],
                    "impacted_task_ids": [],
                    "conflicting_input_refs": ["apps/todo/backend/design/api-contract.md"],
                    "required_reentry_phase": "design",
                    "impact": {
                        "goal_changed": False,
                        "scope_changed": False,
                        "boundary_changed": False,
                        "interface_changed": True,
                        "architecture_changed": False,
                        "team_changed": False,
                        "implementation_changed": False,
                    },
                }
            ],
            "produced_outputs": [],
            "context_echo": None,
            "collaboration_request": None,
        }

        report, _, change_request_ids = materialize_executor_response(
            self.project_root,
            "resolved-impact-graph",
            source_task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )
        self.assertEqual(report["change_requests"][0]["affected_output_ids"], ["todo-api-contract"])
        self.assertEqual(report["change_requests"][0]["affected_handoff_ids"], ["handoff.backend-to-frontend"])

        apply_approved_change_impacts(self.project_root, "resolved-impact-graph", change_request_ids)

        persisted = read_json(
            run_dir / "change-requests" / f"{change_request_ids[0]}.json"
        )
        self.assertEqual(persisted["impacted_objective_ids"], ["app-a"])
        self.assertEqual(persisted["impacted_task_ids"], ["APP-A-FRONT-001", "APP-A-FRONT-002"])
        self.assertEqual(read_activity(self.project_root, "resolved-impact-graph", "APP-A-FRONT-001")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "resolved-impact-graph", "APP-A-FRONT-002")["status"], "needs_revision")
        self.assertTrue((run_dir / "change-impacts" / f"{change_request_ids[0]}.json").exists())

    def test_apply_approved_change_impacts_marks_only_true_consumers_stale(self) -> None:
        scaffold_dual_planning_run(self.project_root, "impact-graph")
        run_dir = self.project_root / "runs" / "impact-graph"
        backend_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-graph",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce the shared backend API contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["apps/todo/backend/design/api-contract.md"],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
        }
        frontend_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-graph",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume the shared API contract in the frontend plan.",
            "inputs": ["Output of APP-B-BACK-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-B-BACK-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        frontend_downstream = {
            "schema": "task-assignment.v1",
            "run_id": "impact-graph",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-002",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Build downstream frontend artifacts from the contract plan.",
            "inputs": ["Output of APP-A-FRONT-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-A-FRONT-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        unrelated_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-graph",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-002",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Unrelated backend cleanup.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        for task in [backend_task, frontend_task, frontend_downstream, unrelated_task]:
            write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)

        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "impact-graph",
            "phase": "design",
            "objective_id": "app-b",
            "handoff_id": "handoff.backend-to-frontend",
            "from_capability": "backend",
            "to_capability": "frontend",
            "from_task_id": "APP-B-BACK-001",
            "to_role": "objectives.app-a.frontend-manager",
            "handoff_type": "interface_handoff",
            "reason": "Frontend consumes the backend API contract.",
            "deliverables": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "blocking": True,
            "shared_asset_ids": ["todo:api-contract"],
            "status": "planned",
            "to_task_ids": ["APP-A-FRONT-001"],
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }
        write_json(run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json", handoff)
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "impact-graph",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced the backend API contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        change_requests = persist_change_requests(
            self.project_root,
            "impact-graph",
            frontend_task,
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Shared API contract must change.",
                        "blocking_reason": "The backend API contract conflicts with the approved frontend integration behavior.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared contract consumed by sibling objectives.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": ["handoff.backend-to-frontend"],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            ),
        )

        impacts = apply_approved_change_impacts(
            self.project_root,
            "impact-graph",
            [change_requests[0]["change_id"]],
        )

        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["directly_impacted_task_ids"], ["APP-A-FRONT-001"])
        self.assertEqual(impacts[0]["impacted_task_ids"], ["APP-A-FRONT-001", "APP-A-FRONT-002"])
        self.assertEqual(impacts[0]["impacted_objective_ids"], ["app-a"])
        notifications = stale_task_notifications(self.project_root, "impact-graph", phase="design")
        self.assertEqual(sorted(notifications), ["APP-A-FRONT-001", "APP-A-FRONT-002"])
        self.assertEqual(read_activity(self.project_root, "impact-graph", "APP-A-FRONT-001")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "impact-graph", "APP-A-FRONT-002")["status"], "needs_revision")
        self.assertFalse((run_dir / "live" / "activities" / "APP-B-BACK-002.json").exists())

    def test_apply_approved_change_impacts_matches_consumers_by_input_path(self) -> None:
        scaffold_dual_planning_run(self.project_root, "impact-path-graph")
        run_dir = self.project_root / "runs" / "impact-path-graph"
        shared_contract_path = (
            "apps/todo/orchestrator/roles/objectives/"
            "basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/"
            "artifacts/middleware/integration-contract-discovery.md"
        )
        producer_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-path-graph",
            "phase": "design",
            "objective_id": "integration-objective",
            "capability": "middleware",
            "task_id": "MID-001",
            "assigned_role": "objectives.integration-objective.middleware-worker",
            "manager_role": "objectives.integration-objective.middleware-manager",
            "acceptance_role": "objectives.integration-objective.acceptance-manager",
            "objective": "Produce the shared integration contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "asset",
                    "output_id": "integration_contract_discovery_asset",
                    "path": shared_contract_path,
                    "asset_id": "todo:integration-contract-discovery",
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": [shared_contract_path],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:integration-contract-discovery"],
        }
        frontend_consumer = {
            "schema": "task-assignment.v1",
            "run_id": "impact-path-graph",
            "phase": "design",
            "objective_id": "frontend-objective",
            "capability": "frontend",
            "task_id": "FRONT-001",
            "assigned_role": "objectives.frontend-objective.frontend-worker",
            "manager_role": "objectives.frontend-objective.frontend-manager",
            "acceptance_role": "objectives.frontend-objective.acceptance-manager",
            "objective": "Consume the shared integration contract.",
            "inputs": [shared_contract_path],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:integration-contract-discovery"],
        }
        backend_consumer = {
            "schema": "task-assignment.v1",
            "run_id": "impact-path-graph",
            "phase": "design",
            "objective_id": "backend-objective",
            "capability": "backend",
            "task_id": "BACK-001",
            "assigned_role": "objectives.backend-objective.backend-worker",
            "manager_role": "objectives.backend-objective.backend-manager",
            "acceptance_role": "objectives.backend-objective.acceptance-manager",
            "objective": "Review the shared integration contract.",
            "inputs": [shared_contract_path],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:integration-contract-discovery"],
        }
        downstream_consumer = {
            "schema": "task-assignment.v1",
            "run_id": "impact-path-graph",
            "phase": "design",
            "objective_id": "frontend-objective",
            "capability": "frontend",
            "task_id": "FRONT-002",
            "assigned_role": "objectives.frontend-objective.frontend-worker",
            "manager_role": "objectives.frontend-objective.frontend-manager",
            "acceptance_role": "objectives.frontend-objective.acceptance-manager",
            "objective": "Continue downstream frontend work.",
            "inputs": ["Output of FRONT-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["FRONT-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        unrelated_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-path-graph",
            "phase": "design",
            "objective_id": "unrelated-objective",
            "capability": "ops",
            "task_id": "OPS-001",
            "assigned_role": "objectives.unrelated-objective.ops-worker",
            "manager_role": "objectives.unrelated-objective.ops-manager",
            "acceptance_role": "objectives.unrelated-objective.acceptance-manager",
            "objective": "Do unrelated work.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        for task_payload in [producer_task, frontend_consumer, backend_consumer, downstream_consumer, unrelated_task]:
            write_json(run_dir / "tasks" / f"{task_payload['task_id']}.json", task_payload)

        write_json(
            run_dir / "reports" / "MID-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "impact-path-graph",
                "phase": "design",
                "objective_id": "integration-objective",
                "task_id": "MID-001",
                "agent_role": "objectives.integration-objective.middleware-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced integration contract.",
                "artifacts": [{"path": shared_contract_path, "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "asset",
                        "output_id": "integration_contract_discovery_asset",
                        "path": shared_contract_path,
                        "asset_id": "todo:integration-contract-discovery",
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        change_request = {
            "schema": "change-request.v2",
            "run_id": "impact-path-graph",
            "change_id": "MID-001-chg-001",
            "source_task_id": "FRONT-001",
            "source_objective_id": "frontend-objective",
            "phase": "design",
            "change_category": "interface_contract",
            "summary": "Integration contract must change.",
            "blocking_reason": "The shared integration contract conflicts with frontend expectations.",
            "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
            "blocking": True,
            "goal_critical": True,
            "affected_output_ids": ["integration_contract_discovery_asset"],
            "affected_handoff_ids": [],
            "impacted_objective_ids": [],
            "impacted_task_ids": [],
            "required_reentry_phase": "design",
            "impact": {
                "goal_changed": False,
                "scope_changed": False,
                "boundary_changed": False,
                "interface_changed": True,
                "architecture_changed": False,
                "team_changed": False,
                "implementation_changed": False,
            },
            "approval": {"mode": "auto", "status": "approved"},
            "replacement_plan_revision": None,
        }
        validate_document(change_request, "change-request.v2", self.project_root)
        write_json(run_dir / "change-requests" / "MID-001-chg-001.json", change_request)

        impacts = apply_approved_change_impacts(
            self.project_root,
            "impact-path-graph",
            ["MID-001-chg-001"],
        )

        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["directly_impacted_task_ids"], ["BACK-001", "FRONT-001"])
        self.assertEqual(impacts[0]["impacted_task_ids"], ["BACK-001", "FRONT-001", "FRONT-002"])
        self.assertEqual(impacts[0]["impacted_objective_ids"], ["backend-objective", "frontend-objective"])
        persisted = read_json(run_dir / "change-requests" / "MID-001-chg-001.json")
        self.assertEqual(persisted["impacted_task_ids"], ["BACK-001", "FRONT-001", "FRONT-002"])
        self.assertEqual(persisted["impacted_objective_ids"], ["backend-objective", "frontend-objective"])
        self.assertEqual(read_activity(self.project_root, "impact-path-graph", "BACK-001")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "impact-path-graph", "FRONT-001")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "impact-path-graph", "FRONT-002")["status"], "needs_revision")
        self.assertFalse((run_dir / "live" / "activities" / "OPS-001.json").exists())

    def test_schedule_tasks_skips_stale_tasks_and_runs_unrelated_work(self) -> None:
        scaffold_dual_planning_run(self.project_root, "impact-schedule")
        run_dir = self.project_root / "runs" / "impact-schedule"
        producer_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-schedule",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce backend contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["apps/todo/backend/design/api-contract.md"],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
        }
        source_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-schedule",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume backend contract.",
            "inputs": ["Output of APP-B-BACK-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-B-BACK-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        downstream_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-schedule",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-002",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Downstream frontend work.",
            "inputs": ["Output of APP-A-FRONT-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-A-FRONT-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        unrelated_task = {
            "schema": "task-assignment.v1",
            "run_id": "impact-schedule",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-002",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Unrelated backend task.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        for task in [producer_task, source_task, downstream_task, unrelated_task]:
            write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        write_json(
            run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "impact-schedule",
                "phase": "design",
                "objective_id": "app-b",
                "handoff_id": "handoff.backend-to-frontend",
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-B-BACK-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "interface_handoff",
                "reason": "Frontend consumes backend contract.",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["todo:api-contract"],
                "status": "planned",
                "to_task_ids": ["APP-A-FRONT-001"],
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "impact-schedule",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced backend contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        write_json(
            run_dir / "reports" / "APP-A-FRONT-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "impact-schedule",
                "phase": "design",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONT-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "blocked",
                "summary": "Blocked on shared contract drift.",
                "artifacts": [],
                "validation_results": [],
                "open_issues": ["Blocking: shared contract drift remains."],
                "change_requests": [],
                "produced_outputs": [],
            },
        )
        change_requests = persist_change_requests(
            self.project_root,
            "impact-schedule",
            source_task,
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Shared API contract must change.",
                        "blocking_reason": "The backend contract conflicts with approved frontend behavior.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": ["handoff.backend-to-frontend"],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            ),
        )
        apply_approved_change_impacts(self.project_root, "impact-schedule", [change_requests[0]["change_id"]])

        def side_effect(project_root: Path, run_id: str, task_id: str, **_: object):
            return write_managed_report(
                project_root,
                run_id,
                task_id,
                status="ready_for_bundle_review",
                summary=f"{task_id} complete",
            )

        tasks = [source_task, downstream_task, unrelated_task]
        with patch("company_orchestrator.management.execute_task", side_effect=side_effect):
            summary = schedule_tasks(
                self.project_root,
                "impact-schedule",
                tasks,
                sandbox_mode="read-only",
                codex_path="codex",
                force=False,
                timeout_seconds=None,
                max_concurrency=2,
            )

        self.assertEqual([item["task_id"] for item in summary["executed"]], ["APP-B-BACK-002"])
        self.assertEqual(sorted(summary["stale_tasks"]), ["APP-A-FRONT-001", "APP-A-FRONT-002"])
        self.assertEqual(read_activity(self.project_root, "impact-schedule", "APP-A-FRONT-001")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "impact-schedule", "APP-A-FRONT-002")["status"], "needs_revision")
        self.assertEqual(read_activity(self.project_root, "impact-schedule", "APP-B-BACK-002")["status"], "ready_for_bundle_review")

    def test_apply_approved_changes_and_resume_replans_only_targeted_objectives(self) -> None:
        run_dir = initialize_run(self.project_root, "change-replan", "# Goal\n\n## Objectives\n- App A\n- App B\n- App C")
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "change-replan",
            "objectives": [
                {"objective_id": "app-a", "title": "App A", "summary": "App A", "status": "approved", "capabilities": ["frontend"]},
                {"objective_id": "app-b", "title": "App B", "summary": "App B", "status": "approved", "capabilities": ["backend"]},
                {"objective_id": "app-c", "title": "App C", "summary": "App C", "status": "approved", "capabilities": ["middleware"]},
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "change-replan")
        generate_role_files(self.project_root, "change-replan", approve=True)
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
                item["human_approved"] = True
            elif item["phase"] == "design":
                item["status"] = "complete"
                item["human_approved"] = True
            elif item["phase"] == "mvp-build":
                item["status"] = "active"
            else:
                item["status"] = "locked"
        write_json(run_dir / "phase-plan.json", phase_plan)
        producer_task = {
            "schema": "task-assignment.v1",
            "run_id": "change-replan",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce backend contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["apps/todo/backend/design/api-contract.md"],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
        }
        consumer_task = {
            "schema": "task-assignment.v1",
            "run_id": "change-replan",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume backend contract.",
            "inputs": ["Output of APP-B-BACK-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-B-BACK-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        unrelated_task = {
            "schema": "task-assignment.v1",
            "run_id": "change-replan",
            "phase": "mvp-build",
            "objective_id": "app-c",
            "capability": "middleware",
            "task_id": "APP-C-MW-001",
            "assigned_role": "objectives.app-c.middleware-worker",
            "manager_role": "objectives.app-c.middleware-manager",
            "acceptance_role": "objectives.app-c.acceptance-manager",
            "objective": "Unrelated middleware work.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        stale_backend_task = dict(unrelated_task)
        stale_backend_task.update(
            {
                "objective_id": "app-b",
                "capability": "backend",
                "task_id": "APP-B-BACK-002",
                "assigned_role": "objectives.app-b.backend-worker",
                "manager_role": "objectives.app-b.backend-manager",
                "acceptance_role": "objectives.app-b.acceptance-manager",
                "objective": "Stale backend build task.",
            }
        )
        stale_frontend_task = dict(unrelated_task)
        stale_frontend_task.update(
            {
                "objective_id": "app-a",
                "capability": "frontend",
                "task_id": "APP-A-FRONT-002",
                "assigned_role": "objectives.app-a.frontend-worker",
                "manager_role": "objectives.app-a.frontend-manager",
                "acceptance_role": "objectives.app-a.acceptance-manager",
                "objective": "Stale frontend build task.",
            }
        )
        for task in [producer_task, consumer_task, unrelated_task, stale_backend_task, stale_frontend_task]:
            write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        write_json(
            run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "change-replan",
                "phase": "design",
                "objective_id": "app-b",
                "handoff_id": "handoff.backend-to-frontend",
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-B-BACK-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "interface_handoff",
                "reason": "Frontend consumes backend contract.",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["todo:api-contract"],
                "status": "planned",
                "to_task_ids": ["APP-A-FRONT-001"],
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "change-replan",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced backend contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        change_requests = persist_change_requests(
            self.project_root,
            "change-replan",
            consumer_task,
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Shared API contract must change.",
                        "blocking_reason": "The backend contract conflicts with approved frontend behavior.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": ["handoff.backend-to-frontend"],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            ),
        )

        with patch(
            "company_orchestrator.change_replan.plan_objective",
            side_effect=lambda *args, **kwargs: {
                "objective_id": args[2],
                "phase": "design",
                "plan_path": f"runs/change-replan/manager-plans/design-{args[2]}.json",
            },
        ) as plan_mock, patch(
            "company_orchestrator.change_replan.run_phase",
            return_value={"run_id": "change-replan", "phase": "design", "scheduled": {"executed": []}},
        ) as run_phase_mock:
            summary = apply_approved_changes_and_resume(
                self.project_root,
                "change-replan",
                change_ids=[change_requests[0]["change_id"]],
                sandbox_mode="read-only",
                codex_path="codex",
                timeout_seconds=None,
                max_concurrency=2,
            )

        self.assertEqual(summary["reentry_phase"], "design")
        self.assertEqual(summary["replanned_objective_ids"], ["app-b", "app-a"])
        self.assertEqual([call.args[2] for call in plan_mock.call_args_list], ["app-b", "app-a"])
        self.assertTrue(all(call.kwargs["replace"] for call in plan_mock.call_args_list))
        run_phase_mock.assert_called_once()
        updated_request = read_json(run_dir / "change-requests" / f"{change_requests[0]['change_id']}.json")
        self.assertIsNotNone(updated_request["replacement_plan_revision"])
        self.assertEqual(active_approved_change_requests(self.project_root, "change-replan"), [])
        updated_phase_plan = read_json(run_dir / "phase-plan.json")
        self.assertEqual(updated_phase_plan["current_phase"], "design")
        self.assertEqual(next(item for item in updated_phase_plan["phases"] if item["phase"] == "design")["status"], "active")
        self.assertEqual(next(item for item in updated_phase_plan["phases"] if item["phase"] == "mvp-build")["status"], "locked")
        self.assertFalse((run_dir / "tasks" / "APP-B-BACK-002.json").exists())
        self.assertFalse((run_dir / "tasks" / "APP-A-FRONT-002.json").exists())
        self.assertTrue((run_dir / "tasks" / "APP-C-MW-001.json").exists())

    def test_apply_approved_changes_and_resume_keeps_request_active_when_producer_replan_fails(self) -> None:
        scaffold_dual_planning_run(self.project_root, "change-replan-fail")
        run_dir = self.project_root / "runs" / "change-replan-fail"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
                item["human_approved"] = True
            elif item["phase"] == "design":
                item["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        producer_task = {
            "schema": "task-assignment.v1",
            "run_id": "change-replan-fail",
            "phase": "design",
            "objective_id": "app-b",
            "capability": "backend",
            "task_id": "APP-B-BACK-001",
            "assigned_role": "objectives.app-b.backend-worker",
            "manager_role": "objectives.app-b.backend-manager",
            "acceptance_role": "objectives.app-b.acceptance-manager",
            "objective": "Produce backend contract.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "todo-api-contract",
                    "path": "apps/todo/backend/design/api-contract.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": ["apps/todo/backend/design/api-contract.md"],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
        }
        consumer_task = {
            "schema": "task-assignment.v1",
            "run_id": "change-replan-fail",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Consume backend contract.",
            "inputs": ["Output of APP-B-BACK-001"],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": ["APP-B-BACK-001"],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": ["todo:api-contract"],
            "handoff_dependencies": ["handoff.backend-to-frontend"],
        }
        for task in [producer_task, consumer_task]:
            write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        write_json(
            run_dir / "collaboration-plans" / "handoff.backend-to-frontend.json",
            {
                "schema": "collaboration-handoff.v1",
                "run_id": "change-replan-fail",
                "phase": "design",
                "objective_id": "app-b",
                "handoff_id": "handoff.backend-to-frontend",
                "from_capability": "backend",
                "to_capability": "frontend",
                "from_task_id": "APP-B-BACK-001",
                "to_role": "objectives.app-a.frontend-manager",
                "handoff_type": "interface_handoff",
                "reason": "Frontend consumes backend contract.",
                "deliverables": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "blocking": True,
                "shared_asset_ids": ["todo:api-contract"],
                "status": "planned",
                "to_task_ids": ["APP-A-FRONT-001"],
                "satisfied_by_task_ids": [],
                "missing_deliverables": [],
                "status_reason": None,
                "last_checked_at": None,
            },
        )
        write_json(
            run_dir / "reports" / "APP-B-BACK-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "change-replan-fail",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACK-001",
                "agent_role": "objectives.app-b.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Produced backend contract.",
                "artifacts": [{"path": "apps/todo/backend/design/api-contract.md", "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "change_requests": [],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "todo-api-contract",
                        "path": "apps/todo/backend/design/api-contract.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        change_requests = persist_change_requests(
            self.project_root,
            "change-replan-fail",
            consumer_task,
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Shared API contract must change.",
                        "blocking_reason": "The backend contract conflicts with approved frontend behavior.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": ["handoff.backend-to-frontend"],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            ),
        )

        with patch(
            "company_orchestrator.change_replan.plan_objective",
            side_effect=ValueError("producer replan failed"),
        ) as plan_mock, patch("company_orchestrator.change_replan.run_phase") as run_phase_mock:
            with self.assertRaisesRegex(ValueError, "producer replan failed"):
                apply_approved_changes_and_resume(
                    self.project_root,
                    "change-replan-fail",
                    change_ids=[change_requests[0]["change_id"]],
                )

        self.assertEqual(plan_mock.call_count, 1)
        run_phase_mock.assert_not_called()
        updated_request = read_json(run_dir / "change-requests" / f"{change_requests[0]['change_id']}.json")
        self.assertIsNone(updated_request["replacement_plan_revision"])
        self.assertEqual(read_activity(self.project_root, "change-replan-fail", "APP-A-FRONT-001")["status"], "needs_revision")

    def test_run_guidance_prefers_apply_approved_changes_for_approved_requests(self) -> None:
        scaffold_dual_planning_run(self.project_root, "change-guidance")
        run_dir = self.project_root / "runs" / "change-guidance"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
                item["human_approved"] = True
            elif item["phase"] == "design":
                item["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)
        task = {
            "schema": "task-assignment.v1",
            "run_id": "change-guidance",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONT-001",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Blocked on shared contract drift.",
            "inputs": [],
            "expected_outputs": [],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
            "execution_mode": "read_only",
            "parallel_policy": "allow",
            "owned_paths": [],
            "writes_existing_paths": [],
            "shared_asset_ids": [],
        }
        write_json(run_dir / "tasks" / "APP-A-FRONT-001.json", task)
        persist_change_requests(
            self.project_root,
            "change-guidance",
            task,
            normalize_change_request_payloads(
                [
                    {
                        "change_category": "interface_contract",
                        "summary": "Shared API contract must change.",
                        "blocking_reason": "The backend contract conflicts with approved frontend behavior.",
                        "why_local_resolution_is_invalid": "A local workaround would fork the shared contract.",
                        "blocking": True,
                        "goal_critical": True,
                        "affected_output_ids": ["todo-api-contract"],
                        "affected_handoff_ids": [],
                        "impacted_objective_ids": [],
                        "impacted_task_ids": [],
                        "required_reentry_phase": "design",
                        "impact": {
                            "goal_changed": False,
                            "scope_changed": False,
                            "boundary_changed": False,
                            "interface_changed": True,
                            "architecture_changed": False,
                            "team_changed": False,
                            "implementation_changed": False,
                        },
                    }
                ]
            ),
        )

        guidance = run_guidance(self.project_root, "change-guidance")

        self.assertEqual(guidance["run_status"], "recoverable")
        self.assertIn("apply-approved-changes", guidance["next_action_command"])

    def test_materialize_executor_response_accepts_artifacts_present_only_in_task_worktree(self) -> None:
        scaffold_planning_run(self.project_root, "structured-produced-outputs-worktree", ["frontend"])
        task_id = "APP-A-FRONTEND-003"
        worktree_artifact = (
            self.project_root
            / ".orchestrator-worktrees"
            / "structured-produced-outputs-worktree"
            / "tasks"
            / task_id
            / "docs"
            / "handoffs"
            / "frontend-middleware.md"
        )
        worktree_artifact.parent.mkdir(parents=True, exist_ok=True)
        worktree_artifact.write_text("handoff", encoding="utf-8")
        task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-produced-outputs-worktree",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": task_id,
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a discovery handoff package in a task worktree.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.handoff.doc",
                    "path": "docs/handoffs/frontend-middleware.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Produced the handoff package in the task worktree.",
            "status": "ready_for_bundle_review",
            "artifacts": [{"path": "docs/handoffs/frontend-middleware.md", "status": "created"}],
            "validation_results": [],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.handoff.doc",
                    "path": "docs/handoffs/frontend-middleware.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "collaboration_request": None,
        }

        report, _, _ = materialize_executor_response(
            self.project_root,
            "structured-produced-outputs-worktree",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(report["produced_outputs"], parsed_response["produced_outputs"])

    def test_materialize_executor_response_canonicalizes_assertion_outputs_by_output_id(self) -> None:
        scaffold_planning_run(self.project_root, "structured-produced-outputs-assertion", ["frontend"])
        task = {
            "schema": "task-assignment.v1",
            "run_id": "structured-produced-outputs-assertion",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "frontend",
            "task_id": "APP-A-FRONTEND-002",
            "assigned_role": "objectives.app-a.frontend-worker",
            "manager_role": "objectives.app-a.frontend-manager",
            "acceptance_role": "objectives.app-a.acceptance-manager",
            "objective": "Produce a discovery assertion.",
            "inputs": [],
            "expected_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "frontend.discovery.ready",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend discovery output is ready.",
                    "evidence": {
                        "validation_ids": ["scope-check"],
                        "artifact_paths": [],
                    },
                }
            ],
            "done_when": [],
            "depends_on": [],
            "validation": [{"id": "scope-check", "command": "validate-scope"}],
            "collaboration_rules": [],
        }
        parsed_response = {
            "summary": "Produced the discovery assertion.",
            "status": "ready_for_bundle_review",
            "artifacts": [],
            "validation_results": [{"id": "scope-check", "status": "passed", "evidence": "scope validated"}],
            "open_issues": [],
            "change_requests": [],
            "produced_outputs": [
                {
                    "kind": "assertion",
                    "output_id": "frontend.discovery.ready",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend discovery scope is ready for the next phase.",
                    "evidence": {
                        "validation_ids": ["scope-check"],
                        "artifact_paths": [],
                    },
                }
            ],
            "context_echo": {
                "role_id": "objectives.app-a.frontend-worker",
                "objective_id": "app-a",
                "phase": "discovery",
                "prompt_layers": ["orchestrator/roles/base/company.md"],
                "schema": "task-assignment.v1",
            },
            "collaboration_request": None,
        }

        report, _, _ = materialize_executor_response(
            self.project_root,
            "structured-produced-outputs-assertion",
            task,
            parsed_response,
            runtime_warnings=[],
            runtime_recovery=None,
            runtime_observability=None,
        )

        self.assertEqual(report["produced_outputs"], task["expected_outputs"])

    def test_ensure_run_integration_workspace_serializes_concurrent_setup(self) -> None:
        active = threading.Lock()
        concurrent_entries: list[str] = []

        def fake_ensure_worktree(repo_root: Path, branch_name: str, workspace_path: Path) -> None:
            if not active.acquire(blocking=False):
                concurrent_entries.append(branch_name)
                return
            try:
                time.sleep(0.05)
                workspace_path.mkdir(parents=True, exist_ok=True)
            finally:
                active.release()

        with (
            patch("company_orchestrator.worktree_manager.git_root", return_value=self.project_root),
            patch("company_orchestrator.worktree_manager.ensure_branch", return_value=None),
            patch("company_orchestrator.worktree_manager.ensure_worktree", side_effect=fake_ensure_worktree),
        ):
            results: list[Path] = []
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    workspace = ensure_run_integration_workspace(self.project_root, "concurrent-run")
                    results.append(workspace.workspace_path)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(concurrent_entries, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(path == results[0] for path in results))

    def test_validate_capability_plan_rejects_assertion_evidence_for_downstream_validation(self) -> None:
        scaffold_planning_run(self.project_root, "invalid-assertion-evidence", ["middleware"])
        outline = objective_outline_for_objective("invalid-assertion-evidence", "app-a", ["middleware"])
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "invalid-assertion-evidence",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware discovery plan.",
            "tasks": [
                {
                    "task_id": "APP-A-MW-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/app-a/discovery/integration-scope.md"],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Produce the middleware discovery package.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.brief",
                            "path": "apps/app-a/discovery/integration-scope.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "assertion",
                            "output_id": "middleware.discovery.ready",
                            "path": None,
                            "asset_id": None,
                            "description": "Discovery package is ready.",
                            "evidence": {
                                "validation_ids": ["acceptance.discovery.scope-boundary-review"],
                                "artifact_paths": ["apps/app-a/discovery/integration-scope.md"],
                            },
                        },
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "middleware-bundle",
                    "task_ids": ["APP-A-MW-001"],
                    "summary": "Middleware bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            validate_capability_plan_contents(
                self.project_root,
                plan,
                run_id="invalid-assertion-evidence",
                phase="discovery",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
            )

        self.assertIn("references validations that are not declared on the task", str(ctx.exception))

    def test_validate_capability_plan_rejects_discovery_producer_assertions_and_self_checks(self) -> None:
        scaffold_planning_run(self.project_root, "invalid-discovery-producer-contract", ["middleware"])
        outline = objective_outline_for_objective("invalid-discovery-producer-contract", "app-a", ["middleware"])
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "invalid-discovery-producer-contract",
            "phase": "discovery",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware discovery plan.",
            "tasks": [
                {
                    "task_id": "APP-A-MW-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/middleware/discovery/bundle.md"],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Produce the middleware discovery bundle.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "middleware.discovery.bundle",
                            "path": "apps/todo/middleware/discovery/bundle.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "assertion",
                            "output_id": "middleware.discovery.bundle.asserted",
                            "path": None,
                            "asset_id": None,
                            "description": "Bundle was written with required sections.",
                            "evidence": {
                                "validation_ids": ["bundle-exists"],
                                "artifact_paths": ["apps/todo/middleware/discovery/bundle.md"],
                            },
                        },
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [
                        {"id": "bundle-exists", "command": "test -f apps/todo/middleware/discovery/bundle.md"},
                    ],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "middleware-bundle",
                    "task_ids": ["APP-A-MW-001"],
                    "summary": "Middleware bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            validate_capability_plan_contents(
                self.project_root,
                plan,
                run_id="invalid-discovery-producer-contract",
                phase="discovery",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
            )

        self.assertIn("should not declare task-level assertion outputs", str(ctx.exception))

    def test_build_planning_payload_filters_frontend_contract_for_backend_mvp_build(self) -> None:
        scaffold_dual_planning_run(self.project_root, "backend-contract-filter")
        run_dir = self.project_root / "runs" / "backend-contract-filter"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)

        frontend_contract = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a" / "artifacts" / "frontend-api-consumption-contract.md"
        frontend_contract.parent.mkdir(parents=True, exist_ok=True)
        frontend_contract.write_text("frontend notes", encoding="utf-8")

        backend_contract = self.project_root / "apps" / "todo" / "backend" / "design" / "todo-api-contract.yaml"
        backend_contract.parent.mkdir(parents=True, exist_ok=True)
        backend_contract.write_text("openapi: 3.0.3\n", encoding="utf-8")

        write_json(
            run_dir / "reports" / "frontend-design-package.json",
            {
                "schema": "completion-report.v1",
                "run_id": "backend-contract-filter",
                "phase": "design",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-DESIGN",
                "agent_role": "objectives.app-a.frontend-worker",
                "summary": "Frontend design package with consumer notes.",
                "artifacts": [{"path": str(frontend_contract.relative_to(self.project_root)), "status": "created"}],
            },
        )
        write_json(
            run_dir / "reports" / "backend-design-package.json",
            {
                "schema": "completion-report.v1",
                "run_id": "backend-contract-filter",
                "phase": "design",
                "objective_id": "app-b",
                "task_id": "APP-B-BACKEND-DESIGN",
                "agent_role": "objectives.app-b.backend-worker",
                "summary": "Backend design package with OpenAPI contract.",
                "artifacts": [{"path": str(backend_contract.relative_to(self.project_root)), "status": "created"}],
            },
        )

        payload = build_planning_payload(self.project_root, "backend-contract-filter", "app-b")

        self.assertEqual(
            [report["capability"] for report in payload["related_prior_phase_reports"]],
            [],
        )
        self.assertEqual(payload["related_prior_phase_artifacts"], [])

    def test_build_planning_payload_surfaces_canonical_contracts_and_filters_peer_contract_artifacts(self) -> None:
        run_dir = initialize_run(self.project_root, "canonical-contract-authority", "# Goal\n\n## Objectives\n- Frontend\n- Backend\n- Integration")
        objective_map = {
            "schema": "objective-map.v1",
            "run_id": "canonical-contract-authority",
            "objectives": [
                {"objective_id": "frontend-obj", "title": "Frontend", "summary": "Frontend", "status": "approved", "capabilities": ["frontend"]},
                {"objective_id": "backend-obj", "title": "Backend", "summary": "Backend", "status": "approved", "capabilities": ["backend"]},
                {"objective_id": "integration-obj", "title": "Integration", "summary": "Integration", "status": "approved", "capabilities": ["middleware"]},
            ],
            "dependencies": [],
        }
        write_json(run_dir / "objective-map.json", objective_map)
        suggest_team_proposals(self.project_root, "canonical-contract-authority")
        generate_role_files(self.project_root, "canonical-contract-authority", approve=True)

        app_objective_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives"
        app_objective_root.parent.mkdir(parents=True, exist_ok=True)
        for objective_id in ("frontend-obj", "backend-obj", "integration-obj"):
            shutil.copytree(
                self.project_root / "orchestrator" / "roles" / "objectives" / objective_id,
                app_objective_root / objective_id,
            )
            shutil.rmtree(self.project_root / "orchestrator" / "roles" / "objectives" / objective_id)

        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)

        frontend_consumer = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "frontend-obj" / "design" / "frontend-api-consumption-contract.md"
        frontend_consumer.parent.mkdir(parents=True, exist_ok=True)
        frontend_consumer.write_text("consumer notes\n", encoding="utf-8")
        backend_contract = self.project_root / "apps" / "todo" / "backend" / "design" / "todo-api-contract.md"
        backend_contract.parent.mkdir(parents=True, exist_ok=True)
        backend_contract.write_text("backend contract\n", encoding="utf-8")
        integration_contract = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "integration-obj" / "design" / "integration-contract.md"
        integration_contract.parent.mkdir(parents=True, exist_ok=True)
        integration_contract.write_text("integration contract\n", encoding="utf-8")

        write_json(
            run_dir / "reports" / "frontend-design.json",
            {
                "schema": "completion-report.v1",
                "run_id": "canonical-contract-authority",
                "phase": "design",
                "objective_id": "frontend-obj",
                "task_id": "FRONTEND-DESIGN",
                "agent_role": "objectives.frontend-obj.frontend-worker",
                "summary": "Frontend notes.",
                "artifacts": [{"path": str(frontend_consumer.relative_to(self.project_root)), "status": "created"}],
            },
        )
        write_json(
            run_dir / "reports" / "backend-design.json",
            {
                "schema": "completion-report.v1",
                "run_id": "canonical-contract-authority",
                "phase": "design",
                "objective_id": "backend-obj",
                "task_id": "BACKEND-DESIGN",
                "agent_role": "objectives.backend-obj.backend-worker",
                "summary": "Backend contract.",
                "artifacts": [{"path": str(backend_contract.relative_to(self.project_root)), "status": "created"}],
            },
        )
        write_json(
            run_dir / "reports" / "integration-design.json",
            {
                "schema": "completion-report.v1",
                "run_id": "canonical-contract-authority",
                "phase": "design",
                "objective_id": "integration-obj",
                "task_id": "INTEGRATION-DESIGN",
                "agent_role": "objectives.integration-obj.middleware-worker",
                "summary": "Integration contract.",
                "artifacts": [{"path": str(integration_contract.relative_to(self.project_root)), "status": "created"}],
            },
        )

        payload = build_planning_payload(self.project_root, "canonical-contract-authority", "frontend-obj")

        self.assertEqual(payload["canonical_contracts"]["api_contract"]["path"], str(backend_contract.relative_to(self.project_root)))
        self.assertEqual(payload["canonical_contracts"]["integration_contract"]["path"], str(integration_contract.relative_to(self.project_root)))
        related_paths = [item["path"] for item in payload["related_prior_phase_artifacts"]]
        self.assertIn(str(backend_contract.relative_to(self.project_root)), related_paths)
        self.assertIn(str(integration_contract.relative_to(self.project_root)), related_paths)
        self.assertNotIn(str(frontend_consumer.relative_to(self.project_root)), related_paths)

    def test_build_capability_planning_payload_surfaces_shared_root_ownership_for_middleware(self) -> None:
        scaffold_planning_run(self.project_root, "shared-root-payload", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        run_dir = self.project_root / "runs" / "shared-root-payload"
        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "mvp-build"
        write_json(run_dir / "phase-plan.json", phase_plan)

        objective_outline = objective_outline_for_objective(
            "shared-root-payload",
            "app-a",
            ["middleware"],
        )
        objective_outline["phase"] = "mvp-build"
        (self.project_root / "package.json").write_text('{"name":"workspace-root"}\n', encoding="utf-8")
        payload = build_capability_planning_payload(
            self.project_root,
            "shared-root-payload",
            "app-a",
            "middleware",
            objective_outline,
        )

        ownership_map = payload["shared_workspace_ownership"]
        self.assertIn(
            {
                "path": "package.json",
                "owner_capability": "middleware",
                "reason": "shared app workspace manifest",
            },
            ownership_map,
        )
        self.assertIn(
            "package.json",
            payload["capability_scope_hints"]["shared_root_owned_paths"],
        )

    def test_build_capability_planning_payload_prefers_app_local_manifest_when_present(self) -> None:
        scaffold_planning_run(self.project_root, "shared-root-local-manifest", ["middleware"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        app_root = self.project_root / "apps" / "todo"
        (self.project_root / "package.json").write_text('{"name":"workspace-root"}\n', encoding="utf-8")
        (app_root / "package.json").write_text('{"name":"todo-app"}\n', encoding="utf-8")
        (app_root / "scripts").mkdir(parents=True, exist_ok=True)

        objective_outline = objective_outline_for_objective(
            "shared-root-local-manifest",
            "app-a",
            ["middleware"],
        )
        objective_outline["phase"] = "mvp-build"
        payload = build_capability_planning_payload(
            self.project_root,
            "shared-root-local-manifest",
            "app-a",
            "middleware",
            objective_outline,
        )

        self.assertIn(
            "apps/todo/package.json",
            payload["capability_scope_hints"]["shared_root_owned_paths"],
        )
        self.assertNotIn(
            "package.json",
            payload["capability_scope_hints"]["shared_root_owned_paths"],
        )

    def test_owned_path_targets_prefix_matches_shared_scripts_glob(self) -> None:
        self.assertTrue(
            owned_path_targets_prefix(
                "apps/todo/scripts/run-mvp-todo-workflow.sh",
                "apps/todo/scripts/**",
            )
        )
        self.assertFalse(
            owned_path_targets_prefix(
                "apps/todo/runtime/scripts/start.js",
                "apps/todo/scripts/**",
            )
        )

    def test_capability_shared_asset_hints_limit_contract_authority_by_capability(self) -> None:
        self.assertEqual(
            capability_shared_asset_hints("app-a", "frontend"),
            ["app-a:frontend:handoff"],
        )
        self.assertEqual(
            capability_shared_asset_hints("app-a", "backend"),
            ["app-a:backend:handoff", "app-a:api-contract"],
        )
        self.assertEqual(
            capability_shared_asset_hints("app-a", "middleware"),
            ["app-a:middleware:handoff", "app-a:integration-contract"],
        )

    def test_build_planning_payload_uses_report_declared_contract_artifacts_before_landing(self) -> None:
        run_dir = initialize_run(self.project_root, "canonical-contract-unlanded", "# Goal\n\n## Objectives\n- Frontend\n- Backend")
        write_json(
            run_dir / "objective-map.json",
            {
                "schema": "objective-map.v1",
                "run_id": "canonical-contract-unlanded",
                "objectives": [
                    {"objective_id": "frontend-obj", "title": "Frontend", "summary": "Frontend", "status": "approved", "capabilities": ["frontend"]},
                    {"objective_id": "backend-obj", "title": "Backend", "summary": "Backend", "status": "approved", "capabilities": ["backend"]},
                ],
                "dependencies": [],
            },
        )
        suggest_team_proposals(self.project_root, "canonical-contract-unlanded")
        generate_role_files(self.project_root, "canonical-contract-unlanded", approve=True)

        app_objective_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives"
        app_objective_root.parent.mkdir(parents=True, exist_ok=True)
        for objective_id in ("frontend-obj", "backend-obj"):
            shutil.copytree(
                self.project_root / "orchestrator" / "roles" / "objectives" / objective_id,
                app_objective_root / objective_id,
            )
            shutil.rmtree(self.project_root / "orchestrator" / "roles" / "objectives" / objective_id)

        phase_plan = read_json(run_dir / "phase-plan.json")
        phase_plan["current_phase"] = "design"
        for item in phase_plan["phases"]:
            if item["phase"] == "discovery":
                item["status"] = "complete"
            elif item["phase"] == "design":
                item["status"] = "active"
        write_json(run_dir / "phase-plan.json", phase_plan)

        contract_path = "apps/todo/backend/design/discovery-api-contract-seed.md"
        handoff_path = "apps/todo/orchestrator/roles/objectives/frontend-obj/artifacts/frontend-discovery-handoff.md"
        write_json(
            run_dir / "reports" / "backend-discovery.json",
            {
                "schema": "completion-report.v1",
                "run_id": "canonical-contract-unlanded",
                "phase": "discovery",
                "objective_id": "backend-obj",
                "task_id": "BACKEND-DISCOVERY",
                "agent_role": "objectives.backend-obj.backend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Backend discovery contract seed.",
                "artifacts": [{"path": contract_path, "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "produced_outputs": [
                    {
                        "kind": "asset",
                        "output_id": "backend-api-contract-seed",
                        "path": contract_path,
                        "asset_id": "backend-obj:api-contract",
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )
        write_json(
            run_dir / "reports" / "frontend-discovery.json",
            {
                "schema": "completion-report.v1",
                "run_id": "canonical-contract-unlanded",
                "phase": "discovery",
                "objective_id": "frontend-obj",
                "task_id": "FRONTEND-DISCOVERY",
                "agent_role": "objectives.frontend-obj.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Frontend discovery handoff.",
                "artifacts": [{"path": handoff_path, "status": "created"}],
                "validation_results": [],
                "open_issues": [],
                "produced_outputs": [
                    {
                        "kind": "asset",
                        "output_id": "frontend-discovery-handoff",
                        "path": handoff_path,
                        "asset_id": "frontend-obj:frontend:handoff",
                        "description": None,
                        "evidence": None,
                    }
                ],
            },
        )

        payload = build_planning_payload(self.project_root, "canonical-contract-unlanded", "frontend-obj")
        self.assertEqual(payload["canonical_contracts"]["api_contract"]["path"], contract_path)
        related_paths = [item["path"] for item in payload["related_prior_phase_artifacts"]]
        self.assertIn(contract_path, related_paths)

    def test_validate_capability_plan_rejects_backend_frontend_consumption_contract_input_in_mvp_build(self) -> None:
        scaffold_planning_run(self.project_root, "backend-contract-authority", ["backend"])
        outline = objective_outline_for_objective("backend-contract-authority", "app-a", ["backend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "backend_http_entrypoint",
                "path": "apps/todo/backend/src/app.ts",
                "asset_id": "backend-http-entrypoint",
                "description": None,
                "evidence": None,
            }
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "backend-contract-authority",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "backend",
            "summary": "Backend capability plan.",
            "tasks": [
                {
                    "task_id": "APP-A-BACKEND-001",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/backend/src/app.ts"],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Implement backend API.",
                    "inputs": [
                        "apps/todo/backend/design/todo-api-contract.yaml",
                        "apps/todo/orchestrator/roles/objectives/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/artifacts/frontend-api-consumption-contract.md",
                    ],
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "backend_http_entrypoint",
                            "path": "apps/todo/backend/src/app.ts",
                            "asset_id": "backend-http-entrypoint",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [
                {
                    "bundle_id": "backend-bundle",
                    "task_ids": ["APP-A-BACKEND-001"],
                    "summary": "Backend bundle",
                }
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            validate_capability_plan_contents(
                self.project_root,
                plan,
                run_id="backend-contract-authority",
                phase="mvp-build",
                objective_id="app-a",
                capability="backend",
                objective_outline=outline,
            )

        self.assertIn("must not consume frontend consumer contract files directly", str(ctx.exception))

    def test_normalize_objective_outline_rejects_frontend_shared_api_contract_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "frontend-contract-authority", ["frontend"])
        objective = {
            "objective_id": "app-a",
            "capabilities": ["frontend"],
        }
        outline = objective_outline_for_objective("frontend-contract-authority", "app-a", ["frontend"])
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "frontend_api_contract",
                "path": "apps/todo/orchestrator/roles/objectives/app-a/design/todo-api-contract.md",
                "asset_id": "app-a:api-contract",
                "description": None,
                "evidence": None,
            }
        ]

        with self.assertRaises(ExecutorError) as ctx:
            normalize_objective_outline(
                self.project_root,
                outline,
                run_id="frontend-contract-authority",
                phase="design",
                objective=objective,
            )

        self.assertIn("must not emit shared api contract outputs", str(ctx.exception))

    def test_validate_capability_plan_rejects_middleware_shared_api_contract_outputs(self) -> None:
        scaffold_planning_run(self.project_root, "middleware-contract-authority", ["middleware"])
        outline = objective_outline_for_objective("middleware-contract-authority", "app-a", ["middleware"])
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "integration-contract",
                "path": "apps/todo/orchestrator/roles/objectives/app-a/design/integration-contract.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "middleware-contract-authority",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware capability plan.",
            "tasks": [
                {
                    "task_id": "APP-A-MW-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/orchestrator/roles/objectives/app-a/design/api-interface-contract.md"],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Emit an API contract from middleware.",
                    "inputs": [],
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "middleware-api-contract",
                            "path": "apps/todo/orchestrator/roles/objectives/app-a/design/api-interface-contract.md",
                            "asset_id": "app-a:api-contract",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [{"bundle_id": "middleware-bundle", "task_ids": ["APP-A-MW-001"], "summary": "bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            validate_capability_plan_contents(
                self.project_root,
                plan,
                run_id="middleware-contract-authority",
                phase="design",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
            )

        self.assertIn("must not emit shared api contract outputs", str(ctx.exception))

    def test_validate_capability_plan_rejects_nonfrontend_consumer_contract_inputs(self) -> None:
        scaffold_planning_run(self.project_root, "consumer-contract-authority", ["middleware"])
        outline = objective_outline_for_objective("consumer-contract-authority", "app-a", ["middleware"])
        outline["phase"] = "design"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "artifact",
                "output_id": "integration-contract",
                "path": "apps/todo/orchestrator/roles/objectives/app-a/design/integration-contract.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        ]
        plan = {
            "schema": "capability-plan.v1",
            "run_id": "consumer-contract-authority",
            "phase": "design",
            "objective_id": "app-a",
            "capability": "middleware",
            "summary": "Middleware capability plan.",
            "tasks": [
                {
                    "task_id": "APP-A-MW-001",
                    "capability": "middleware",
                    "assigned_role": "objectives.app-a.middleware-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": ["apps/todo/orchestrator/roles/objectives/app-a/design/integration-contract.md"],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Produce the integration contract.",
                    "inputs": [
                        "apps/todo/orchestrator/roles/objectives/frontend-obj/design/frontend-api-consumption-contract.md"
                    ],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "integration-contract",
                            "path": "apps/todo/orchestrator/roles/objectives/app-a/design/integration-contract.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                }
            ],
            "bundle_plan": [{"bundle_id": "middleware-bundle", "task_ids": ["APP-A-MW-001"], "summary": "bundle"}],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        with self.assertRaises(ExecutorError) as ctx:
            validate_capability_plan_contents(
                self.project_root,
                plan,
                run_id="consumer-contract-authority",
                phase="design",
                objective_id="app-a",
                capability="middleware",
                objective_outline=outline,
            )

        self.assertIn("must not consume frontend consumer contract files directly", str(ctx.exception))

    def test_normalize_capability_plan_backfills_missing_mvp_lane_output_to_terminal_task(self) -> None:
        scaffold_planning_run(self.project_root, "backend-lane-backfill", ["backend"])
        generic_root = self.project_root / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root = self.project_root / "apps" / "todo" / "orchestrator" / "roles" / "objectives" / "app-a"
        app_role_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(generic_root, app_role_root)
        shutil.rmtree(generic_root)
        backend_src = self.project_root / "apps" / "todo" / "backend" / "src" / "todos"
        backend_src.mkdir(parents=True, exist_ok=True)
        backend_design = self.project_root / "apps" / "todo" / "backend" / "design"
        backend_design.mkdir(parents=True, exist_ok=True)
        (backend_design / "backend-design-package.md").write_text("backend design\n", encoding="utf-8")

        outline = objective_outline_for_objective("backend-lane-backfill", "app-a", ["backend"])
        outline["phase"] = "mvp-build"
        outline["capability_lanes"][0]["expected_outputs"] = [
            {
                "kind": "asset",
                "output_id": "backend-http-entrypoint",
                "path": "apps/todo/backend/src/server.ts",
                "asset_id": "backend-http-entrypoint",
                "description": None,
                "evidence": None,
            },
            {
                "kind": "asset",
                "output_id": "backend-crud-tests",
                "path": "apps/todo/backend/tests/todo-api.test.ts",
                "asset_id": "backend-crud-tests",
                "description": None,
                "evidence": None,
            },
            {
                "kind": "artifact",
                "output_id": "backend-review-bundle",
                "path": "apps/todo/backend/mvp-build/backend-review-bundle.md",
                "asset_id": None,
                "description": None,
                "evidence": None,
            },
        ]

        plan = {
            "schema": "capability-plan.v1",
            "run_id": "backend-lane-backfill",
            "phase": "mvp-build",
            "objective_id": "app-a",
            "capability": "backend",
            "summary": "Backend build plan.",
            "tasks": [
                {
                    "task_id": "BACKEND-001",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Implement persistence.",
                    "inputs": ["apps/todo/backend/design/backend-design-package.md"],
                    "expected_outputs": [
                        {
                            "kind": "asset",
                            "output_id": "backend-http-entrypoint",
                            "path": "apps/todo/backend/src/server.ts",
                            "asset_id": "backend-http-entrypoint",
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": [],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                },
                {
                    "task_id": "BACKEND-REVIEW",
                    "capability": "backend",
                    "assigned_role": "objectives.app-a.backend-worker",
                    "execution_mode": "isolated_write",
                    "parallel_policy": "serialize",
                    "owned_paths": [],
                    "writes_existing_paths": [],
                    "shared_asset_ids": [],
                    "objective": "Emit validation and review outputs.",
                    "inputs": ["Output of BACKEND-001"],
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "backend-review-bundle",
                            "path": "apps/todo/backend/mvp-build/backend-review-bundle.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        }
                    ],
                    "done_when": ["done"],
                    "depends_on": ["BACKEND-001"],
                    "validation": [],
                    "collaboration_rules": [],
                    "working_directory": None,
                    "additional_directories": [],
                    "sandbox_mode": "workspace-write",
                },
            ],
            "bundle_plan": [
                {"bundle_id": "backend-build", "task_ids": ["BACKEND-001", "BACKEND-REVIEW"], "summary": "bundle"}
            ],
            "dependency_notes": [],
            "collaboration_handoffs": [],
        }

        normalized, _ = normalize_capability_plan(
            self.project_root,
            plan,
            run_id="backend-lane-backfill",
            phase="mvp-build",
            objective_id="app-a",
            capability="backend",
            objective_outline=outline,
            default_sandbox_mode="read-only",
        )

        terminal_task = next(task for task in normalized["tasks"] if task["task_id"] == "BACKEND-REVIEW")
        terminal_output_ids = [item["output_id"] for item in terminal_task["expected_outputs"]]
        self.assertIn("backend-crud-tests", terminal_output_ids)
        self.assertIn("apps/todo/backend/tests/todo-api.test.ts", terminal_task["owned_paths"])

    def test_align_required_outbound_handoff_output_ids_keeps_source_task_scope(self) -> None:
        plan = {
            "tasks": [
                {
                    "task_id": "frontend-package-review-bundle",
                    "expected_outputs": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_mvp_build_bundle",
                            "path": "runs/test/reports/frontend-mvp-build-bundle.json",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "artifact",
                            "output_id": "frontend_mvp_build_summary",
                            "path": "runs/test/reports/frontend-mvp-build-summary.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                    ],
                }
            ],
            "collaboration_handoffs": [
                {
                    "handoff_id": "react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items-frontend_to_acceptance_review_bundle",
                    "from_capability": "frontend",
                    "to_capability": "acceptance",
                    "from_task_id": "frontend-package-review-bundle",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "reason": "Deliver the frontend review package.",
                    "deliverable_output_ids": ["frontend_mvp_build_bundle", "frontend_mvp_build_summary"],
                    "blocking": True,
                    "shared_asset_ids": [],
                }
            ],
        }
        objective_outline = {
            "collaboration_edges": [
                {
                    "edge_id": "frontend-acceptance-edge",
                    "from_capability": "frontend",
                    "to_capability": "acceptance",
                    "to_role": "objectives.app-a.acceptance-manager",
                    "handoff_type": "review_bundle",
                    "deliverables": [
                        {
                            "kind": "artifact",
                            "output_id": "frontend_mvp_build_bundle",
                            "path": "runs/test/reports/frontend-mvp-build-bundle.json",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "artifact",
                            "output_id": "frontend_mvp_build_summary",
                            "path": "runs/test/reports/frontend-mvp-build-summary.md",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                        {
                            "kind": "artifact",
                            "output_id": "frontend-todo-types",
                            "path": "apps/todo/frontend/src/types/todo.js",
                            "asset_id": None,
                            "description": None,
                            "evidence": None,
                        },
                    ],
                }
            ]
        }

        align_required_outbound_handoff_output_ids(plan, objective_outline=objective_outline, capability="frontend")

        self.assertEqual(
            plan["collaboration_handoffs"][0]["deliverable_output_ids"],
            ["frontend_mvp_build_bundle", "frontend_mvp_build_summary"],
        )

    def test_evaluate_handoff_satisfies_structured_deliverables_by_output_id(self) -> None:
        scaffold_planning_run(self.project_root, "structured-handoff", ["frontend", "middleware"])
        run_dir = self.project_root / "runs" / "structured-handoff"
        artifact_path = self.project_root / "docs" / "contracts" / "frontend-api.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("contract", encoding="utf-8")
        write_json(
            run_dir / "reports" / "APP-A-FRONTEND-001.json",
            {
                "schema": "completion-report.v1",
                "run_id": "structured-handoff",
                "phase": "design",
                "objective_id": "app-a",
                "task_id": "APP-A-FRONTEND-001",
                "agent_role": "objectives.app-a.frontend-worker",
                "status": "ready_for_bundle_review",
                "summary": "Frontend contract ready.",
                "artifacts": [{"path": "docs/contracts/frontend-api.md", "status": "created"}],
                "produced_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": "frontend.contract.doc",
                        "path": "docs/contracts/frontend-api.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    },
                    {
                        "kind": "assertion",
                        "output_id": "frontend.contract.validated",
                        "path": None,
                        "asset_id": None,
                        "description": "Frontend contract validation passed.",
                        "evidence": {
                            "validation_ids": ["contract-check"],
                            "artifact_paths": ["docs/contracts/frontend-api.md"],
                        },
                    },
                ],
                "validation_results": [{"id": "contract-check", "status": "passed", "evidence": "contract validated"}],
                "legacy_dependency_notes": [],
                "open_issues": [],
                "legacy_follow_ups": [],
            },
        )
        handoff = {
            "schema": "collaboration-handoff.v1",
            "run_id": "structured-handoff",
            "phase": "design",
            "objective_id": "app-a",
            "handoff_id": "app-a-frontend-to-middleware",
            "from_capability": "frontend",
            "to_capability": "middleware",
            "from_task_id": "APP-A-FRONTEND-001",
            "to_role": "objectives.app-a.middleware-manager",
            "handoff_type": "interface_handoff",
            "reason": "Middleware needs the finalized frontend contract.",
            "deliverables": [
                {
                    "kind": "artifact",
                    "output_id": "frontend.contract.doc",
                    "path": "docs/contracts/frontend-api.md",
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                },
                {
                    "kind": "assertion",
                    "output_id": "frontend.contract.validated",
                    "path": None,
                    "asset_id": None,
                    "description": "Frontend contract validation passed.",
                    "evidence": {
                        "validation_ids": ["contract-check"],
                        "artifact_paths": ["docs/contracts/frontend-api.md"],
                    },
                },
            ],
            "blocking": True,
            "shared_asset_ids": ["app-a:integration"],
            "to_task_ids": ["APP-A-MW-001"],
            "status": "planned",
            "satisfied_by_task_ids": [],
            "missing_deliverables": [],
            "status_reason": None,
            "last_checked_at": None,
        }

        refreshed = evaluate_handoff(self.project_root, "structured-handoff", handoff, tasks_by_id={})

        self.assertEqual(refreshed["status"], "satisfied")
        self.assertEqual(refreshed["missing_deliverables"], [])


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
    artifacts: list[dict[str, object]] | None = None,
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
        "artifacts": artifacts or [],
        "validation_results": [],
        "open_issues": [],
        "change_requests": [],
        "produced_outputs": [],
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
        "runtime_warnings": [],
        "parallel_execution_requested": False,
        "parallel_execution_granted": False,
        "parallel_fallback_reason": None,
        "branch_name": None,
        "workspace_path": None,
        "commit_sha": None,
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
                "expected_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": f"{objective_id}-{capability}-note",
                        "path": f"apps/{objective_id}/{capability}-note.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "done_when": ["task complete"],
                "execution_mode": "read_only",
                "parallel_policy": "allow",
                "writes_existing_paths": [],
                "owned_paths": [],
                "shared_asset_ids": [],
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
        "collaboration_handoffs": []
    }


def objective_outline_for_objective(
    run_id: str,
    objective_id: str,
    capabilities: list[str],
    *,
    collaboration_edges: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": "objective-outline.v1",
        "run_id": run_id,
        "phase": "discovery",
        "objective_id": objective_id,
        "summary": f"Capability-managed discovery plan for {objective_id}",
        "capability_lanes": [
            {
                "capability": capability,
                "assigned_manager_role": (
                    f"objectives.{objective_id}.{capability}-manager"
                    if capability != "general"
                    else f"objectives.{objective_id}.objective-manager"
                ),
                "objective": f"Plan {capability} discovery work for {objective_id}",
                "inputs": ["Planning Inputs.goal_markdown"],
                "expected_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": f"{objective_id}-{capability}-discovery-plan",
                        "path": f"apps/{objective_id}/{capability}-discovery-plan.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "done_when": [f"{capability} discovery tasks are planned"],
                "depends_on": [],
                "planning_notes": [f"Stay within the {capability} lane."],
                "collaboration_rules": [],
            }
            for capability in capabilities
        ],
        "dependency_notes": [],
        "collaboration_edges": collaboration_edges or [],
    }


def capability_plan_for_objective(
    run_id: str,
    objective_id: str,
    capability: str,
    *,
    collaboration_handoffs: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": "capability-plan.v1",
        "run_id": run_id,
        "phase": "discovery",
        "objective_id": objective_id,
        "capability": capability,
        "summary": f"{capability} capability plan for {objective_id}",
        "tasks": [
            {
                "task_id": f"{objective_id.upper()}-{capability.upper()}-001",
                "capability": capability,
                "assigned_role": f"objectives.{objective_id}.{capability}-worker",
                "execution_mode": "read_only",
                "parallel_policy": "allow",
                "writes_existing_paths": [],
                "owned_paths": [],
                "shared_asset_ids": [],
                "objective": f"Plan {capability} work for {objective_id}",
                "inputs": ["Planning Inputs.goal_markdown"],
                "expected_outputs": [
                    {
                        "kind": "artifact",
                        "output_id": f"{objective_id}-{capability}-discovery-plan",
                        "path": f"apps/{objective_id}/{capability}-discovery-plan.md",
                        "asset_id": None,
                        "description": None,
                        "evidence": None,
                    }
                ],
                "done_when": [f"{capability} work is described"],
                "depends_on": [],
                "validation": [{"id": "manager-check", "command": "true"}],
                "collaboration_rules": [],
                "working_directory": None,
                "additional_directories": [],
                "sandbox_mode": "read-only",
            }
        ],
        "bundle_plan": [
            {
                "bundle_id": f"{capability}-bundle",
                "task_ids": [f"{objective_id.upper()}-{capability.upper()}-001"],
                "summary": f"{capability} bundle",
            }
        ],
        "dependency_notes": [],
        "collaboration_handoffs": collaboration_handoffs or [],
    }


def read_json_lines(path: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        entries.append(json.loads(stripped))
    return entries


def init_git_repo(project_root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=project_root, capture_output=True, check=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_root, capture_output=True, check=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_root, capture_output=True, check=True, text=True)
    (project_root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project_root, capture_output=True, check=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project_root, capture_output=True, check=True, text=True)
