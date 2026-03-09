# Delivery Workflow And Review Gates

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `design`  
Role: `middleware-worker`

## Purpose

This artifact defines the minimum frontend-backend delivery workflow for MVP build handoff. It turns the approved shared API contract into a small local integration plan with explicit startup order, environment and config boundaries, validation flow, smoke-test coverage, and phase-exit review gates for the acceptance manager and human operator.

One objective detail section was not supplied in resolved inputs:
- `Objective Details for Application Integration And Delivery Workflow`

This workflow therefore relies only on the approved contract, worker review notes, success criteria, constraints, and the requirement that each phase end with a human-approvable report.

## Handoff Inputs

The MVP build handoff package depends on these design artifacts:
- `application-integration-contract.md` as the source of truth for request and response behavior
- `frontend-contract-review.md` as evidence that the contract covers client-visible CRUD, state, and error needs
- `backend-contract-review.md` as evidence that the contract preserves persistence, validation, and error guarantees

No separate task-graph artifact was supplied in resolved inputs. This document therefore limits itself to the delivery sequence and review gates required for build handoff.

## Environment And Config Boundary

The implementation teams must preserve these configuration boundaries:
- The frontend owns one API base-URL setting and must not hard-code the backend origin in component code.
- The backend owns one listen-address or port setting and one persistence setting suitable for the chosen durable local storage mechanism.
- If local development uses cross-origin requests instead of a dev proxy, the backend must also own the allow-list or origin setting needed for the React app to reach the API.
- The shared contract does not require browser-local persistence, secret management, authentication, or background workers.

Recommended MVP naming to reduce avoidable drift during implementation:
- Frontend API base URL inside the browser runtime: `TODO_API_BASE_URL`
- Integrated runtime backend host and port: `TODO_BACKEND_HOST` and `TODO_BACKEND_PORT`
- Integrated runtime frontend host and port: `TODO_FRONTEND_HOST` and `TODO_FRONTEND_PORT`
- Integrated runtime backend persistence path: `TODO_BACKEND_DB_PATH`

These names are handoff recommendations, not proof that a specific framework or storage vendor has been chosen.

## Current MVP Runtime Workflow

The approved local MVP workflow is now implemented as one command:

```bash
npm run todo-runtime:start
```

The runtime command:
- starts the backend first
- resolves the frontend `apiBaseUrl` from the live backend URL without source edits
- applies backend cross-origin allowance for the frontend origin within the runtime wiring
- serves the React todo page from the frontend host
- prints one JSON line with `backend.url`, `backend.allowedOrigin`, `backend.databasePath`, `frontend.url`, and `frontend.apiBaseUrl`

Reviewers can optionally override the integrated runtime with:
- `TODO_BACKEND_HOST`
- `TODO_BACKEND_PORT`
- `TODO_FRONTEND_HOST`
- `TODO_FRONTEND_PORT`
- `TODO_BACKEND_DB_PATH`

## Startup Order

The local startup order for integration work is:
1. Choose the durable backend storage path and any optional host or port overrides for the integrated runtime.
2. Start the integrated runtime with `npm run todo-runtime:start`; it starts the backend before starting the frontend host.
3. Read the JSON startup line and use `frontend.url` as the reviewer entry point.
4. Verify backend readiness with `GET /api/todos`; success is `200 OK` with a JSON body that matches the shared contract, including the empty-list case.
5. Load the frontend from `frontend.url` and exercise the integrated CRUD flow against the wired backend `apiBaseUrl`.
6. Run the smoke-test flow before broader implementation work is considered integrated.

## Integration Touchpoints

The MVP has four direct frontend-backend touchpoints:
- `GET /api/todos` for initial load, reload restoration, and post-restart persistence confirmation
- `POST /api/todos` for create
- `PATCH /api/todos/:id` for edit, complete, and uncomplete
- `DELETE /api/todos/:id` for delete

The frontend-backend boundary also depends on these shared behaviors:
- JSON request and response bodies
- structured `error` envelopes for all non-success responses
- authoritative mutation responses for local reconciliation
- deterministic list ordering and deterministic missing-resource handling

No additional middleware surface is approved for this MVP. Websockets, background jobs, event streams, search endpoints, and auth middleware remain out of scope.

## Validation Flow

The minimum validation flow before MVP build begins is:
1. Contract validation: confirm frontend and backend implementation plans still target the exact route set, todo schema, error envelope, and no-op `PATCH` semantics defined in `application-integration-contract.md`.
2. Local readiness validation: confirm the backend can answer `GET /api/todos` and the frontend can reach the configured base URL without hard-coded transport assumptions.
3. Mutation-path validation: confirm create, edit, complete, uncomplete, and delete each use the approved endpoints and response envelopes.
4. Persistence validation: confirm the persisted todo collection survives both browser reload and backend restart.

Recommended minimum automated validation path for implementation:
- API-level tests for `GET /api/todos`, `POST /api/todos`, `PATCH /api/todos/:id`, and `DELETE /api/todos/:id`
- at least one automated assertion that `PATCH` no-op requests return `200 OK` without changing `updatedAt`
- at least one automated assertion that data created before a backend restart is still returned after restart
- runtime startup and connectivity checks through `npm run validate:todo-runtime-startup` and `npm run validate:todo-runtime-connectivity`

Recommended minimum manual validation path for implementation:
- load the frontend with an empty store
- create one todo
- edit the same todo title
- mark it complete
- mark it incomplete
- reload the page and confirm the last persisted state remains visible
- restart the backend and confirm the todo still appears
- delete the todo
- reload again and confirm the todo remains deleted

## Smoke Test

The smallest approved smoke-test path for MVP handoff is:
1. Start with an empty persisted store and confirm `GET /api/todos` returns `200 OK` with `items: []`.
2. Create one todo from the frontend and confirm the backend returns `201 Created` with a backend-assigned `id`, `createdAt`, and `updatedAt`.
3. Edit that todo title and confirm the UI reconciles from the returned `PATCH` response.
4. Toggle `completed` to `true`, then back to `false`, and confirm each response is authoritative.
5. Submit a no-op edit using the already persisted values and confirm the request returns `200 OK` without changing `updatedAt`.
6. Reload the browser and confirm the todo state survives reload through `GET /api/todos`.
7. Restart the backend and confirm the todo still exists through `GET /api/todos`.
8. Delete the todo and confirm the delete response returns `deletedId`.
9. Reload once more and confirm `GET /api/todos` no longer returns the deleted item.

This smoke test is intentionally small. Anything beyond CRUD and persistence restoration is outside the MVP acceptance path for this objective.

## Review Gates

The design package is ready for acceptance-manager review only if the reviewer confirms:
- `Contract alignment gate`: the shared contract reflects the worker review outcomes, including explicit no-op `PATCH` handling.
- `Environment gate`: the handoff package defines the frontend API base-URL boundary, the backend listen and persistence boundary, and the local cross-origin or proxy dependency.
- `Startup-order gate`: the handoff package defines a backend-first startup order and an explicit readiness check before frontend integration.
- `Touchpoint gate`: the handoff package limits integration to the approved CRUD endpoints and shared JSON or error-envelope behaviors.
- `Smoke-test gate`: the handoff package includes a small create, edit, complete, uncomplete, delete, reload, and restart persistence validation path.
- `Scope gate`: the handoff package does not introduce auth, collaboration, filtering, analytics, or other deferred features.

The phase is ready for human operator approval only if the operator confirms:
- `Simplicity gate`: the handoff package stays aggressively simple and does not force unnecessary services, teams, or infrastructure.
- `Build-readiness gate`: the package is concrete enough that frontend and backend teams can start MVP implementation without re-opening route or persistence-boundary questions.
- `Approval gate`: the acceptance manager has reviewed the package and reported no unresolved blocker that would invalidate build handoff.

## Phase Exit And Handoff

Phase exit requires these artifacts to move together as one design package:
- `application-integration-contract.md`
- `frontend-contract-review.md`
- `backend-contract-review.md`
- `delivery-workflow-and-review-gates.md`

The handoff is incomplete if any of these conditions remain true:
- the shared contract leaves no-op `PATCH` behavior ambiguous
- the frontend API base-URL boundary is still undefined
- backend restart durability is not treated as mandatory
- the smoke-test path does not cover create, edit, complete, uncomplete, delete, reload, and restart persistence
- the acceptance manager or human operator has not completed review

This document does not self-approve phase completion. It defines what reviewers must verify before MVP build begins.
