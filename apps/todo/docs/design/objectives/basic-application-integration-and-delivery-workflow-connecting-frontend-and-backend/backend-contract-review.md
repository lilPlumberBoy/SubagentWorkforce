# Backend Contract Review

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `design`  
Role: `backend-worker`

## Review Scope

Reviewed inputs:
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/application-integration-contract.md`
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/backend-integration-requirements.md`
- `Goal markdown: Success Criteria and Out Of Scope`

Review boundary:
- Backend service guarantees only
- Shared API contract completeness and ambiguity only
- No implementation-stack decisions unless required by the contract

## Outcome

Decision: `approved with notes`

The shared contract is acceptable for backend implementation planning. It preserves the required todo resource semantics, CRUD behavior, validation ownership, machine-detectable error handling, and persistence guarantees without leaking storage internals.

No blocking gaps were found against the supplied backend integration requirements or the MVP success criteria.

## Evidence

### Resource Guarantees

- The contract preserves all backend-owned resource fields required by the backend requirements: `id`, `title`, `completed`, `createdAt`, and `updatedAt`.
- The resource invariants align with backend requirements: stable identifier ownership, immutable `createdAt`, boolean `completed`, and authoritative persisted read responses.
- `GET /todos`, `POST /todos`, `PATCH /todos/:id`, and `DELETE /todos/:id` cover the full CRUD surface needed for add, edit, complete, uncomplete, delete, and reload restoration.
- Mutation responses return authoritative persisted state, which is sufficient for backend-led read-after-write behavior.

### Validation Semantics

- The contract keeps validation authority on the backend and does not delegate correctness to the frontend.
- `POST /todos` requires `title`, enforces string type, and rejects empty or whitespace-only values.
- `PATCH /todos/:id` requires at least one supported mutable field, restricts supported fields to `title` and `completed`, rejects unsupported fields, and rejects attempts to mutate backend-owned fields.
- The error envelope is machine-detectable and maps deterministic backend categories to HTTP status codes: `validation_error`, `not_found`, `conflict`, and `server_error`.
- Missing-resource behavior is explicit for update and delete, including deterministic `404 Not Found` behavior for repeated delete on a missing resource.

### Persistence Behavior

- The contract explicitly requires durable backend-owned storage that survives both browser reloads and backend restarts.
- Successful mutation responses are defined as persisted authoritative state rather than speculative acceptance.
- `GET /todos` is defined as the full persisted collection needed to reconstruct state after reload.
- The contract stays at the interface boundary and does not expose storage internals such as tables, files, or storage vendors.

## Notes

### Non-Blocking Ambiguity

- `PATCH /todos/:id` does not explicitly define no-op update behavior when the client sends supported fields whose values already match persisted state.
- This matters because the shared contract says `updatedAt` changes after every successful persisted mutation, while the backend requirements say `updatedAt` must change after successful mutations that modify stored state.
- The current draft is still acceptable for design review because the ambiguity is narrow and does not block contract shape, but it should be resolved before implementation starts so frontend and backend do not make different assumptions.

Recommended clarification in a later revision:
- Either reject no-op PATCH requests as `400 validation_error`
- Or accept them as `200 OK` without changing `updatedAt`
- Or explicitly define them as state-changing writes that do change `updatedAt`

### Deferred Constraint Note

- The draft correctly leaves `title` length limits, normalization rules, and identifier format unstated.
- This is acceptable at the current design boundary, but any stricter rules must be added to the shared contract before implementation diverges across teams.

## Dependency Impact

- Backend implementation planning can proceed against the current route set, response envelopes, and persistence guarantees.
- Frontend implementation can safely treat mutation responses as authoritative state without requiring immediate follow-up reads.
- If later design work introduces stricter title constraints, identifier rules, or explicit no-op update semantics, the shared contract must be revised before teams implement conflicting behavior.

## Review Gate Check

- `Resource gate`: passed
- `Validation gate`: passed
- `Error gate`: passed
- `Persistence gate`: passed
- `Boundary gate`: passed

## Final Review Position

Approved with notes. No blocking backend gap or ambiguity prevents bundle review, but the no-op PATCH ambiguity should be tracked for follow-on contract tightening before implementation begins.
