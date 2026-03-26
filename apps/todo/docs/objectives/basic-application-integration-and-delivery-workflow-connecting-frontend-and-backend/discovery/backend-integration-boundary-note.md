# Backend Integration Boundary Note

## Scope And Input Gaps

This discovery note is bounded by the task assignment and the shared application summary in `apps/todo/goal-draft.md`. The following required planning inputs were unresolved in the injected runtime context when this note was authored:

- `capability_lane.objective`
- `capability_lane.expected_outputs`
- `goal_context.objective_details.Backend API And Persistence`
- `goal_context.sections.Discovery Expectations`
- `goal_context.sections.In Scope`
- `goal_context.sections.Out Of Scope`
- `goal_context.sections.Known Unknowns`
- `goal_context.sections.MVP Build Expectations`

Because those inputs were missing, this note only defines backend-owned obligations that are already implied by the objective and task contract. It does not claim that missing planning decisions were supplied.

## Backend-Owned Todo Lifecycle Responsibilities

For the MVP integration boundary, the backend owns the authoritative server-side handling of todo item lifecycle changes after a client request crosses the application boundary.

- Create todo items and return the accepted persisted record.
- List todo items from persisted state so the frontend can render the current collection.
- Update todo item content and return the resulting persisted record.
- Complete or uncomplete todo items and return the resulting persisted record.
- Delete todo items and ensure subsequent reads reflect the deletion outcome.
- Reject malformed writes, invalid identifiers, or requests targeting missing todo items.

The frontend may collect input and render responses, but it is not the source of truth for stored todo state.

## Frontend-Visible Contract Obligations

The backend must expose a client-visible contract that allows the frontend to:

- load the current todo list
- submit a new todo
- submit changes to an existing todo
- toggle completion state in either direction
- remove a todo item

That contract must also:

- return enough stable todo data for the frontend to reconcile create, update, completion, reload, and delete flows
- distinguish success, validation failure, missing-resource failure, and unexpected server failure so the frontend can map outcomes to user feedback
- avoid partial-success ambiguity for writes that fail validation or persistence

Discovery does not choose transport style, endpoint naming, status-code mapping, or detailed request and response schema. Those contract details are deferred to design.

## Server-Side Validation Expectations

Server-side validation is required even if the frontend performs its own checks.

- Validation must reject todo writes that are incomplete, empty, malformed, or otherwise not acceptable for persistence.
- Validation must prevent unsupported state transitions or invalid item references from being persisted.
- Validation failures must be surfaced through the contract without leaving partial writes behind.
- Any normalization rules that change stored or returned values remain a design-phase decision, but they must be documented because they affect the frontend-visible contract.

## Persistence Obligations

The backend owns persistence behavior required for the MVP:

- Accepted create, update, complete or uncomplete, and delete operations must persist across page reloads and backend restarts.
- List reads must reflect the persisted source of truth rather than transient in-memory UI state.
- Mutation acknowledgements must reflect the state the backend considers persisted, not just the request payload.
- Delete behavior must be consistent from the frontend perspective: once a delete succeeds, the item should no longer appear in normal list results.

Discovery does not choose storage technology, database type, file format, migration approach, or repository abstraction. Those persistence design choices are deferred to design.

## Explicit Design-Phase Deferrals

The following topics are intentionally outside this discovery note and must be decided later:

- backend framework choice
- storage technology choice
- authentication and authorization behavior
- detailed API schema and endpoint design
- exact todo field set beyond what is minimally necessary to support CRUD behavior

## Cross-Team Boundary Notes

- Backend owns data integrity, persistence, and server-side validation.
- Frontend owns input collection, rendering, and presentation of contract outcomes.
- Middleware or integration work should own environment wiring, runtime connectivity, and end-to-end verification once the backend contract is designed.

This note should be treated as discovery input to the shared API contract asset, not as the final contract itself.
