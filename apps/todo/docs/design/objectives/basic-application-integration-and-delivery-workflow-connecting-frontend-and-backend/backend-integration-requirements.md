# Backend Integration Requirements

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `design`  
Role: `backend-worker`

## Purpose

This artifact defines the backend-side integration requirements that the shared todo contract must preserve for the MVP. It stays at the service-contract boundary: it describes required resource behavior, CRUD semantics, persistence guarantees, validation and error boundaries, and local integration assumptions without prescribing frontend UI behavior.

Two objective detail sections were not supplied in resolved inputs:
- `Objective Details for Application Integration And Delivery Workflow`
- `Objective Details for Backend API And Persistence`

The requirements below therefore rely only on the available scope, success criteria, constraints, and explicit design expectation that the team produce an API or interface contract with validation and review gates.

## Scope and Assumptions

- The backend serves a single todo resource collection for a single-user MVP.
- No auth is required or designed for this contract. Requests are treated as operating within one implicit local user context.
- Multi-user collaboration, real-time synchronization, reminders, due dates, labels, search, and complex filtering are out of scope.
- The frontend is a React client, but the backend contract must remain consumable by any HTTP-capable local client.
- Persistence must be backend-owned and must survive browser reloads and backend process restarts.

## Resource Contract

### Todo Resource

The backend owns one todo resource with the minimum behavior necessary to support:
- create a todo item
- list persisted todo items
- edit an existing todo item
- mark an item complete
- mark an item incomplete
- delete an item

### Required Resource Fields

The shared contract must preserve these backend-owned fields:
- `id`: stable unique identifier assigned by the backend
- `title`: user-editable text for the todo item
- `completed`: boolean completion state
- `createdAt`: backend-generated creation timestamp
- `updatedAt`: backend-generated last-modified timestamp

Field names may change in the final API contract if all teams agree, but the semantics above must remain intact.

### Resource Invariants

The backend must preserve these invariants:
- Each todo item has exactly one backend-assigned identifier.
- `completed` is always a boolean in responses.
- `createdAt` is immutable after creation.
- `updatedAt` changes after every successful mutation that modifies stored state.
- Read responses reflect persisted state, not speculative or client-assumed state.

## CRUD Semantics

### List

- A list operation returns the authoritative persisted collection of todo items.
- The list result must be stable enough for the frontend to fully reconstruct visible application state after a reload.
- Default list behavior should return all non-deleted todo items because filtering is out of scope for the MVP.

### Create

- A create operation accepts the client-supplied fields required to make a new todo item.
- The backend assigns backend-owned fields and persists the new record before returning success.
- A successful create response returns the authoritative persisted todo item.

### Update

- An update operation may modify only supported mutable fields.
- Unsupported fields from the client must be rejected or ignored explicitly by contract; they must never mutate backend-owned fields silently in a way that changes invariants.
- A successful update response returns the authoritative persisted todo item after the mutation is committed.

### Complete or Incomplete

- Completion changes are ordinary persisted mutations of `completed`.
- The contract must support both complete and uncomplete behavior because success criteria require both.
- Completion toggles must update `updatedAt` and return the persisted post-mutation record.

### Delete

- A delete operation removes the todo item from normal list results.
- Repeating delete on a missing resource must resolve deterministically in the final API contract rather than producing ambiguous behavior.
- After a successful delete, subsequent reads must not return the deleted item as an active resource.

## Persistence Expectations

- Persistence is mandatory and backend-owned. In-memory-only behavior is not acceptable for the MVP.
- Once a mutation succeeds, the resulting state must survive page reloads.
- Once a mutation succeeds, the resulting state must survive backend restarts under the supported local development setup.
- The contract must not expose storage internals such as table names, file paths, or vendor-specific details to the frontend.
- The backend may choose the concrete persistence mechanism later, but it must provide durable local storage with deterministic read-after-write behavior for a small MVP.

## Validation Boundary

The backend owns request validation for all mutating operations. At minimum, the final contract must enforce:
- required data for creating a todo item
- valid data types for supported fields
- rejection of malformed identifiers
- rejection of unsupported mutation fields
- rejection of syntactically valid requests that violate todo invariants

The frontend may perform convenience validation, but client validation does not replace backend validation.

## Error Boundary

The final API contract must expose predictable error categories so the frontend can handle failures without inferring backend internals. The contract must distinguish at least:
- `validation error`: request shape or field values are invalid
- `not found error`: the target todo resource does not exist
- `conflict or invariant error`: the request cannot be applied without breaking a defined rule, if such rules are introduced in the final model
- `server or persistence error`: the backend could not complete a valid request because of an internal failure

Error responses must be machine-detectable and must not require parsing free-form text to determine the category.

## Local Integration Assumptions

- The MVP uses one backend service endpoint reachable by the local React frontend during development.
- The frontend is allowed to treat backend mutation responses as the source of truth for local state reconciliation.
- The backend contract should support simple request-response integration and should not require websockets, job polling, or event subscriptions.
- Ordering, pagination, and search are not required unless later design artifacts explicitly add them through review.

## Review Gates

This artifact is ready for bundle review only if reviewers confirm:
- `Scope gate`: the contract stays within single-user todo CRUD and explicitly excludes no auth out-of-scope features such as collaboration and advanced filtering.
- `Resource gate`: the todo resource invariants are sufficient to support create, edit, complete, uncomplete, delete, and reload restoration.
- `Persistence gate`: the requirements make persistence across reloads and backend restarts a non-optional backend guarantee.
- `Validation gate`: the backend clearly owns validation and does not delegate correctness to the frontend.
- `Error gate`: the contract requires deterministic, categorizable error handling for invalid, missing, and failed operations.
- `Boundary gate`: the artifact does not lock the frontend to backend implementation details beyond the guarantees required for integration.

## Dependencies on Follow-On Design Work

- The backend API contract should translate these requirements into concrete routes, request bodies, response bodies, and status codes.
- The todo data model should define exact field constraints, mutation rules, and any normalization rules for `title`.
- Middleware and frontend artifacts should assume only the guarantees defined here unless a later approved contract narrows them further.
