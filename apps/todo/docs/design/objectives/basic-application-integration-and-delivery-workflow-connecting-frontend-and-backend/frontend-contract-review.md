# Frontend Contract Review

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `design`  
Role: `frontend-worker`

## Review Scope

Reviewed inputs:
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/application-integration-contract.md`
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/frontend-integration-requirements.md`
- `Goal markdown: Success Criteria and Out Of Scope`

Review boundary:
- React client behavior only
- Shared contract completeness and ambiguity only
- No backend implementation requests beyond interface guarantees

## Outcome

Decision: `approved with notes`

The shared contract is acceptable for frontend implementation planning. It covers the MVP CRUD surface, preserves the todo shape the React client needs, defines deterministic success and error behavior, and stays within the approved MVP scope.

No blocking frontend gap was found against the supplied frontend integration requirements or the stated success criteria.

## Evidence

### CRUD Flow Coverage

- `GET /todos` returns the full persisted collection needed for initial load and reload restoration.
- `POST /todos` accepts a title-only create request and returns the authoritative persisted todo record needed to add an item in the UI without a follow-up read.
- `PATCH /todos/:id` supports title edits and `completed` toggles, which covers edit, complete, and uncomplete flows with one mutation surface.
- `DELETE /todos/:id` returns `deletedId`, which gives the client a deterministic removal contract for delete success.
- The shared todo schema includes every client-consumed field required by the React list and mutation flows: `id`, `title`, `completed`, `createdAt`, and `updatedAt`.

### Client-Visible State Coverage

- The contract supports `loading` as a client-owned state by providing one read operation for initial fetch and retry.
- The contract supports `success with items` and `success with empty list` because `GET /todos` returns either populated `items` or `items: []` with `200 OK`.
- The contract supports list-level `error` handling because non-success responses use a structured `error` envelope and `GET /todos` has explicit failure behavior.
- The contract supports per-action pending UI states because create, edit, toggle, and delete each map to a single request whose success response is authoritative for reconciliation.
- The contract keeps item-level operations scoped to one `id`, which lets the frontend represent row-level saving or deleting states without requiring whole-list blocking.

### Validation and Error Handling

- Create and edit validation needs are covered because `title` is required, must be a string, and must not be empty or whitespace-only.
- `PATCH /todos/:id` rejects unsupported fields and attempts to mutate backend-owned fields, which prevents the client from relying on undefined request shapes.
- The error envelope provides machine-detectable `error.code`, a user-displayable `error.message`, and optional `fieldErrors`, which satisfies the frontend requirement to avoid parsing free-form text.
- `404 not_found` behavior is explicit for update and delete, which lets the UI reconcile stale row actions deterministically.
- `500 server_error` behavior is explicit for read and mutation failures, which is sufficient for list-level or row-level retry messaging.

### Determinism and MVP Scope Fit

- `GET /todos` defines deterministic ordering by `createdAt` ascending with a stable secondary rule, so the UI does not need to guess how the list should settle after reload.
- Mutation success responses are defined as authoritative persisted state, which preserves read-after-write reconciliation for create, edit, complete, and uncomplete flows.
- The contract explicitly excludes auth, collaboration, real-time sync, reminders, labels, search, pagination, and advanced filtering, which matches the approved MVP boundary.
- The contract stays at the interface boundary and does not prescribe storage internals or frontend presentation details.

## Notes

### Non-Blocking Ambiguity

- `PATCH /todos/:id` does not explicitly define no-op behavior when the client submits supported fields whose values already match persisted state.
- This matters to the frontend because the contract currently says `updatedAt` changes after every successful persisted mutation, but it does not say whether an unchanged edit or repeated toggle is rejected, accepted without changing `updatedAt`, or accepted as a state-changing write.
- The draft remains acceptable for bundle review because the CRUD surface and UI error handling are still complete, but this ambiguity should be resolved before implementation begins so the frontend does not make incorrect assumptions about row reconciliation after no-op submissions.

Recommended clarification in a later revision:
- Reject no-op PATCH requests as `400 validation_error`
- Or accept them as `200 OK` without changing `updatedAt`
- Or explicitly define them as successful writes that do change `updatedAt`

### Optional Validation Detail Note

- The contract makes `error.fieldErrors` optional, which is still sufficient for nearby validation messaging because `error.message` is required.
- If the team wants guaranteed field-level inline rendering for title validation, a later revision can require `fieldErrors.title` for `validation_error` responses that originate from `title`.

## Dependency Impact

- Frontend planning can proceed against `GET /todos`, `POST /todos`, `PATCH /todos/:id`, and `DELETE /todos/:id` without waiting for additional route design.
- UI state design can remain response-driven because mutation responses are authoritative and missing-resource failures are explicit.
- If later design work adds stricter title constraints, mandatory field-level validation details, or explicit no-op PATCH semantics, the shared contract should be revised before implementation starts.

## Review Gate Check

- `Flow gate`: passed
- `Validation gate`: passed
- `State gate`: passed
- `Error gate`: passed
- `Exclusion gate`: passed
- `Boundary gate`: passed

## Final Review Position

Approved with notes. No blocking frontend gap or ambiguity prevents bundle review, but the no-op PATCH behavior and optional field-level validation detail should be tracked for follow-on contract tightening before implementation begins.
