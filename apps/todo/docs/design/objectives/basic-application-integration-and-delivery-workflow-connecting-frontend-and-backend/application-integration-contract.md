# Application Integration Contract

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `design`  
Role: `middleware-worker`

## Purpose

This artifact defines the minimum shared interface contract between the React todo frontend and the backend CRUD service for the MVP. It converts the approved frontend and backend integration requirements into one transport-level contract that both sides can build against without exposing storage or implementation details.

Three objective detail sections were not supplied in resolved inputs:
- `Objective Details for Application Integration And Delivery Workflow`
- `Objective Details for React Web Frontend`
- `Objective Details for Backend API And Persistence`

This contract therefore relies only on the supplied scope, success criteria, constraints, and the frontend and backend requirement artifacts already approved for review.

## Scope and Boundary

- The contract supports a single-user todo application only.
- Auth, collaboration, real-time sync, reminders, due dates, labels, search, pagination, and advanced filtering are out of scope.
- The backend remains responsible for durable persistence, request validation, and authoritative todo state.
- The frontend remains responsible for local loading, empty, success, and error presentation.
- The contract is HTTP request-response only. No websocket, polling job, or event subscription behavior is required.

## Base URL and Transport

- The frontend sends all requests to one configured API `base URL`.
- In local development, the `base URL` is an environment-configured HTTP origin such as `http://localhost:<port>`.
- When the frontend is served from a different local origin than the API, the backend must explicitly allow that frontend origin through runtime wiring such as `TODO_ALLOWED_ORIGIN`.
- All request and response bodies use `application/json` unless otherwise stated.
- Timestamp fields in every response use RFC 3339 UTC string format.
- Every successful mutation response returns authoritative persisted state from the backend.
- Every non-success response returns a structured `error` envelope so the frontend does not need to parse free-form text.

## Shared Todo Schema

The backend exposes todos with this shared response shape:

```json
{
  "id": "todo_123",
  "title": "Buy milk",
  "completed": false,
  "createdAt": "2026-03-07T14:30:00Z",
  "updatedAt": "2026-03-07T14:30:00Z"
}
```

Field contract:
- `id`: backend-assigned stable identifier. The frontend must treat it as an opaque string and send it back unchanged in mutation routes.
- `title`: authoritative persisted title text.
- `completed`: boolean completion state.
- `createdAt`: backend-generated creation timestamp in machine-readable string format.
- `updatedAt`: backend-generated last-modified timestamp in machine-readable string format.

Resource invariants:
- `id` is stable for the life of the todo.
- `completed` is always a boolean in every response.
- `createdAt` does not change after creation.
- `updatedAt` changes after every successful persisted mutation that modifies stored state.
- Read responses reflect persisted state, not speculative client state.

## Endpoint Surface

### GET /api/todos

Purpose:
- Returns the full persisted todo collection required to reconstruct the UI after load or reload.

Request:
- No request body.

Success response:
- `status`: `200 OK`

```json
{
  "items": [
    {
      "id": "todo_123",
      "title": "Buy milk",
      "completed": false,
      "createdAt": "2026-03-07T14:30:00Z",
      "updatedAt": "2026-03-07T14:30:00Z"
    }
  ]
}
```

List behavior:
- Returns all active, non-deleted todos.
- Returns items in deterministic `createdAt` ascending order.
- If two items share the same `createdAt`, the backend must apply a stable secondary ordering rule, such as lexical `id`, so the response does not reshuffle across reloads.
- An empty collection returns `200 OK` with `"items": []`.

Error response:
- `status`: `500 Internal Server Error` when the backend cannot read persisted state.

### POST /api/todos

Purpose:
- Creates one todo item.

Request:

```json
{
  "title": "Buy milk"
}
```

Request rules:
- `title` is required.
- `title` must be a string.
- `title` is trimmed before persistence and the trimmed value must be between `1` and `200` characters.
- No other request fields are required or supported.

Success response:
- `status`: `201 Created`

```json
{
  "todo": {
    "id": "todo_123",
    "title": "Buy milk",
    "completed": false,
    "createdAt": "2026-03-07T14:30:00Z",
    "updatedAt": "2026-03-07T14:30:00Z"
  }
}
```

Error response:
- `status`: `400 Bad Request` for validation failure
- `status`: `500 Internal Server Error` for persistence or internal failure

### PATCH /api/todos/:id

Purpose:
- Updates one existing todo item.
- Supports both title edits and complete or uncomplete operations.

Request:

```json
{
  "title": "Buy oat milk",
  "completed": true
}
```

Request rules:
- At least one supported mutable field must be present in the request body.
- Supported mutable fields are `title` and `completed`.
- `title`, when present, is trimmed before persistence and the trimmed value must be between `1` and `200` characters.
- `completed`, when present, must be a boolean.
- Unsupported fields must be rejected with a validation `error` rather than ignored silently.
- Client attempts to mutate `id`, `createdAt`, or `updatedAt` must be rejected with a validation `error`.
- If the request contains only supported fields whose values already match the persisted todo, the backend must accept the request as a no-op update and return the current persisted todo without changing `updatedAt`.

Success response:
- `status`: `200 OK`

```json
{
  "todo": {
    "id": "todo_123",
    "title": "Buy oat milk",
    "completed": true,
    "createdAt": "2026-03-07T14:30:00Z",
    "updatedAt": "2026-03-07T14:35:00Z"
  }
}
```

Error response:
- `status`: `400 Bad Request` for invalid request body
- `status`: `404 Not Found` when `:id` is well-formed but no todo exists for that identifier
- `status`: `500 Internal Server Error` for persistence or internal failure

No-op update behavior:
- A no-op `PATCH` request is not treated as a validation failure.
- A no-op `PATCH` response returns `200 OK` with the current persisted todo.
- For a no-op `PATCH`, `updatedAt` must remain unchanged because no stored state changed.

### DELETE /api/todos/:id

Purpose:
- Deletes one existing todo item.

Request:
- No request body.

Success response:
- `status`: `200 OK`

```json
{
  "deletedId": "todo_123"
}
```

Delete behavior:
- After a successful delete response, subsequent `GET /api/todos` responses must not include the deleted item.
- Repeating delete on a missing resource must resolve deterministically as `404 Not Found`.

Error response:
- `status`: `400 Bad Request` only if a future identifier-validation rule is added to the shared contract
- `status`: `404 Not Found` when `:id` is well-formed but no todo exists for that identifier
- `status`: `500 Internal Server Error` for persistence or internal failure

## Error Envelope

All non-success responses return this shared `error` response shape:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Title is required.",
    "fieldErrors": {
      "title": [
        "Title is required."
      ]
    }
  }
}
```

Error contract:
- `error.code`: machine-detectable category string
- `error.message`: user-displayable summary
- `error.fieldErrors`: optional map for field-level validation details

Required error codes:
- `validation_error`: request shape, field values, or unsupported fields are invalid
- `not_found`: the target todo does not exist
- `server_error`: the backend could not complete a valid request because of an internal or persistence failure

Status mapping:
- `400` -> `validation_error`
- `404` -> `not_found`
- `500` -> `server_error`

## Validation and Reconciliation Rules

- The backend is authoritative for all mutation validation.
- The frontend may perform convenience validation, but it must treat backend validation responses as the source of truth.
- Mutation success responses are sufficient for the frontend to reconcile local state without an immediate follow-up read.
- The frontend may treat `PATCH /api/todos/:id` responses as the authoritative state for edit, complete, and uncomplete flows.
- The frontend may treat an unchanged `updatedAt` in a successful `PATCH /api/todos/:id` response as a no-op acceptance rather than a failed write.
- The frontend may treat `DELETE /api/todos/:id` success as authoritative removal of the returned `deletedId`.
- The list response is sufficient to reconstruct state after browser reload without browser-local persistence.

## Local Environment Boundary

- One React frontend integrates with one backend service endpoint reachable during local development.
- The frontend should be able to switch the API `base URL` through environment configuration rather than hard-coding a transport origin in component code.
- The local MVP runtime may use a proxy or direct cross-origin requests, but that wiring must be externalized through runtime configuration rather than source edits.
- The frontend runtime configuration uses `TODO_API_BASE_URL` as the canonical API-origin input.
- The backend runtime may use `TODO_ALLOWED_ORIGIN` when the frontend host runs on a different origin.
- The backend must provide durable storage that survives page reloads and backend restarts, but storage implementation details remain hidden from the frontend.

## Deferred or Unresolved Items

- The backend trims titles and enforces a `1` to `200` character bound after trimming.
- The backend identifier format is intentionally not standardized here. The frontend must treat `id` as opaque, and the backend may reject path identifiers that do not match its accepted identifier format.
- No direct frontend-backend mismatch was found in the supplied requirement artifacts. The remaining open items are missing detail, not a team conflict.

## Review Gates

This artifact is ready for bundle review only if reviewers confirm:
- `Surface gate`: the contract defines the minimum endpoint set required for create, list, edit, complete, uncomplete, delete, and reload restoration.
- `Schema gate`: the shared todo schema preserves `id`, `title`, `completed`, `createdAt`, and `updatedAt` semantics.
- `Status gate`: each endpoint has explicit success and error `status` behavior that the frontend can handle deterministically.
- `Error gate`: the `error` envelope is machine-detectable and supports both field-level validation and missing-resource handling.
- `No-op gate`: `PATCH /api/todos/:id` no-op behavior is explicit so frontend and backend do not diverge on `updatedAt` or validation expectations.
- `Boundary gate`: the contract stays at the interface boundary and does not prescribe storage internals or non-MVP features.
- `Persistence gate`: persisted read-after-write behavior and restart durability remain mandatory backend guarantees.

## Dependency Notes

- Frontend implementation can build directly against `GET /api/todos`, `POST /api/todos`, `PATCH /api/todos/:id`, and `DELETE /api/todos/:id`.
- Backend implementation must choose a concrete persistence mechanism and validation layer that satisfy this contract without exposing storage internals.
- If a later design artifact introduces stricter identifier rules or additional transport behaviors, this contract must be revised before implementation diverges.
