# Backend Design Review Gates

Objective: `simple-backend-api-and-persistence-layer-for-storing-todo-items`  
Phase: `design`  
Role: `backend-worker`

This artifact defines the minimum backend review gates for MVP build work. It converts the approved backend architecture, todo data model, and API contract into deterministic evidence expectations so implementation can be decomposed and reviewed without reopening settled design decisions.

## Build Preconditions

These items are fixed for MVP build planning and must not be reopened without an explicit design change:

- The backend remains a single-process Node.js service using Express for HTTP handling and SQLite as the only persistence store.
- The public collection path is `/api/todos`, with `GET`, `POST`, `PATCH /:id`, and `DELETE /:id` as the only MVP todo routes.
- The public todo representation remains exactly five fields: `id`, `title`, `completed`, `createdAt`, and `updatedAt`.
- Only `title` and `completed` are client-writable. `title` is trimmed before validation and persistence and must normalize to 1 through 200 characters inclusive.
- `PATCH /api/todos/:id` is the only edit and complete or uncomplete endpoint. Valid no-op updates still return `200 OK` with unchanged `updatedAt`.
- Missing todo targets on `PATCH` and `DELETE` resolve as `404 Not Found`. The MVP does not define a `409 Conflict` branch.
- Delete remains a hard delete. The backend must not introduce archive, soft-delete, restore, or history behavior in MVP build work.

The following items may still be refined during MVP build planning so long as they preserve the fixed contract above:

- SQLite file-path selection for local development and tests.
- Startup schema initialization mechanism and where it lives in the codebase.
- Backend identifier generation algorithm.
- Exact backend test harness and command structure.
- Local frontend-to-backend wiring details such as proxy or origin configuration.

Shared-asset precondition:
- Before cross-objective integration review, `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/application-integration-contract.md` must be aligned to `/api/todos`, `404` for missing IDs, the 1 to 200 normalized title rule, and the removal of the reserved MVP `409` branch.

## Validation Plan

Minimum validation gates expected from MVP backend build work:

| Validation gate | Minimum execution | Required evidence |
| --- | --- | --- |
| `startup-schema` | Automated backend test or reproducible script against a clean database path | Shows the service initializes the SQLite schema before serving requests, startup succeeds with no pre-existing DB file, and a second startup against the same file remains safe and idempotent. |
| `crud-contract` | Automated request-level tests against a running service or app harness | Shows `GET /api/todos` returns `200` with `{"items":[]}` from an empty store, `POST` returns `201 {"todo": ...}`, `PATCH` returns `200 {"todo": ...}` for title edits and complete or uncomplete flows, and `DELETE` returns `200 {"deletedId":"..."}`. |
| `validation-errors` | Automated negative-path tests | Shows invalid JSON or unsupported fields return `400 validation_error`, create or update title normalization failures return `400 validation_error`, empty update bodies return `400 validation_error`, and missing todo IDs on `PATCH` or `DELETE` return `404 not_found`. |
| `no-op-update` | Automated request-level test | Shows a valid `PATCH` whose normalized values already match persisted state returns `200 OK`, returns the unchanged persisted todo, and preserves the prior `updatedAt` value. |
| `durability` | Automated integration test or deterministic scripted smoke with restart steps | Shows a created todo survives process restart when the same SQLite file path is reused, and a deleted todo stays absent after a later restart. |
| `scope-boundary` | Code review plus one targeted test or artifact note | Shows route handlers expose only the approved `/api/todos` contract, storage details do not leak into response bodies, and no out-of-scope backend features were added. |

Required scenario coverage for the `crud-contract` and `validation-errors` gates:

1. Create with surrounding whitespace in `title` and confirm the persisted response contains the trimmed value, `completed: false`, and equal `createdAt` plus `updatedAt`.
2. Edit `title` and confirm the response returns the authoritative stored todo with a later `updatedAt`.
3. Complete and uncomplete the same todo through `PATCH /api/todos/:id` and confirm `completed` toggles without changing `createdAt`.
4. Delete an existing todo and confirm a later `GET /api/todos` omits it.
5. Reject client attempts to send `id`, `createdAt`, or `updatedAt` in create or update payloads.
6. Reject unsupported routes or alternate completion endpoints from being treated as part of the MVP surface.

Evidence format expected at review time:

- Test names or scripted steps must identify which gate they satisfy.
- Captured assertions must check response envelopes, not only status codes.
- Durability evidence must call out the database path reuse across restarts.
- Build notes must state how tests reset or isolate SQLite state so results are deterministic.

## Review Checklist

Reviewers should not mark backend MVP build work ready unless every item below is satisfied:

- The implementation stays within the approved architecture: one Express process, one SQLite store, no ORM, no extra infrastructure.
- The service exposes only the approved `/api/todos` route family and response envelopes from the API contract.
- The public todo model remains the approved five-field shape with backend-owned timestamps and identifiers.
- Backend validation enforces the approved mutable-field rules and 1 to 200 normalized title length bound.
- Automated evidence covers create, list, edit, complete, uncomplete, delete, invalid payloads, missing IDs, and successful no-op updates.
- Review evidence proves persistence across process restart rather than only within one server lifetime.
- Build notes explain the SQLite file-path convention and test reset strategy chosen for implementation.
- No out-of-scope behavior was introduced, including auth, labels, due dates, reminders, filtering, soft delete, or extra mutation routes.
- Cross-objective integration review does not proceed until the shared integration contract is updated to the approved backend contract.

## Traceability To Success Criteria

| Success criterion | Backend interpretation for MVP build | Required build evidence |
| --- | --- | --- |
| A user can add, edit, complete, uncomplete, and delete todo items from the frontend. | The backend must provide one stable JSON contract that supports create, list, edit, complete, uncomplete, and delete through `/api/todos`. | Request-level tests or deterministic service-harness tests covering `POST`, `GET`, `PATCH` for title changes, `PATCH` for `completed: true`, `PATCH` for `completed: false`, and `DELETE`, all with approved response envelopes. |
| Todo items persist across page reloads through a backend API and storage layer. | The backend must commit writes to SQLite, return authoritative persisted state, and preserve data when the service restarts. | Startup-schema evidence, durability evidence across restart with the same database path, and a post-restart `GET /api/todos` assertion showing previously created data remains available until deleted. |
| The orchestration system can complete Discovery and Design with clear outputs and then produce an MVP plan that is small and actionable. | MVP build planning must consume the approved architecture, data model, API contract, and this review-gates artifact without reopening fixed scope. | A backend build plan that decomposes work into implementation slices for server bootstrap, repository and schema init, route plus validation handling, and deterministic validation, while keeping only file-path, init mechanism, ID generation, and local wiring as refinable details. |

## Handoff To MVP Build

The backend build plan should treat these decisions as locked inputs:

- Architecture: single Express service, SQLite persistence, no ORM, no extra backend roles or infrastructure.
- Contract: `/api/todos` route family, fixed success envelopes, `400` for validation failures, `404` for missing targets, `500` for internal failures, and no MVP `409` branch.
- Model: five public todo fields, trimmed `title`, only `title` and `completed` mutable, hard delete, deterministic list ordering, and `updatedAt` changing only when stored state changes.

The backend build plan may still choose implementation details for:

- SQLite file placement and naming.
- Schema bootstrap wiring.
- Module layout inside the service.
- Identifier generation implementation.
- Specific test libraries and process orchestration for restart checks.

Recommended MVP build decomposition:

1. Server bootstrap and configuration slice: service startup, JSON handling, environment defaults, and SQLite path configuration.
2. Persistence slice: schema initialization, repository methods, deterministic list ordering, and hard-delete behavior.
3. HTTP contract slice: `/api/todos` handlers, request validation, error mapping, and authoritative mutation responses.
4. Validation slice: automated request-level tests plus restart-persistence coverage and documented test reset rules.

Open handoff risks that remain in scope but unresolved:

- The shared integration contract artifact still reflects `/todos` routes and a reserved `409` path and must be corrected before integration review.
- SQLite file-path and test-reset conventions are still intentionally open and must be made explicit during build planning so validation can be reproduced deterministically.
