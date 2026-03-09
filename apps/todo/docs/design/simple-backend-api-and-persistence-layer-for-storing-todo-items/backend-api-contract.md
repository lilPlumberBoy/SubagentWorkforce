# Backend API Contract

Objective: `simple-backend-api-and-persistence-layer-for-storing-todo-items`  
Phase: `design`  
Role: `backend-worker`

This artifact publishes the frontend-facing MVP HTTP contract for todo CRUD. It applies the approved backend architecture decision of a flat REST-style JSON collection under `/api/todos` and the approved todo data model with exactly five public todo fields.

Contract scope:
- Single-user MVP only.
- JSON over HTTP only.
- One todo collection only.
- No auth, pagination, filtering, search, labels, due dates, or speculative workflow endpoints.

## Endpoints

All paths are relative to one backend `base URL`, for example `http://localhost:3000`.

### GET `/api/todos`

Purpose:
- Return the full persisted todo collection needed to render or restore the UI after load or reload.

Success:
- `200 OK`
- Response body uses `ListTodosResponse`.

Behavior:
- Returns all non-deleted todos.
- Returns items in deterministic order: `createdAt` ascending, then `id` ascending as the stable tie-breaker.
- Returns `200 OK` with an empty `items` array when no todos exist.

### POST `/api/todos`

Purpose:
- Create one todo.

Request:
- Body uses `CreateTodoRequest`.
- `Content-Type` must be `application/json`.

Success:
- `201 Created`
- Response body uses `TodoMutationResponse`.

Behavior:
- The backend trims `title`, validates it, generates `id`, sets `completed` to `false`, and sets `createdAt` and `updatedAt` to the same current UTC timestamp before persisting.

### PATCH `/api/todos/:id`

Purpose:
- Partially update one existing todo.
- This single endpoint covers title edits, complete, and uncomplete behavior.

Request:
- Body uses `UpdateTodoRequest`.
- `Content-Type` must be `application/json`.
- `:id` is an opaque backend identifier supplied as a URL path segment.

Success:
- `200 OK`
- Response body uses `TodoMutationResponse`.

Behavior:
- A request may update `title`, `completed`, or both.
- Setting `completed` to `true` is the complete flow.
- Setting `completed` to `false` is the uncomplete flow.
- A valid request that produces no persisted change is still successful and returns the unchanged todo with its existing `updatedAt`.
- There are no separate `/complete` or `/uncomplete` endpoints in the MVP contract.

Identifier rule:
- The contract does not impose a client-visible identifier format beyond "opaque non-empty path segment".
- Clients must treat `id` values as opaque strings and must not construct meaning from them.
- If the routed `:id` does not match a persisted todo, the backend returns `404 Not Found` rather than a separate malformed-identifier error.

### DELETE `/api/todos/:id`

Purpose:
- Delete one existing todo.

Request:
- No request body.
- `:id` follows the same opaque identifier rule as `PATCH`.

Success:
- `200 OK`
- Response body uses `DeleteTodoResponse`.

Behavior:
- The delete is a hard delete.
- After success, the deleted todo no longer appears in later `GET /api/todos` responses.
- If the routed `:id` does not match a persisted todo, the backend returns `404 Not Found`.

## Request Schemas

### `CreateTodoRequest`

```json
{
  "title": "Buy milk"
}
```

Rules:
- `title` is required.
- `title` must be a JSON string.
- The backend trims leading and trailing whitespace before validation and persistence.
- The normalized `title` length must be between 1 and 200 characters inclusive.
- Additional request properties are not allowed.
- Client-supplied `id`, `completed`, `createdAt`, and `updatedAt` are rejected.

### `UpdateTodoRequest`

```json
{
  "title": "Buy oat milk",
  "completed": true
}
```

Rules:
- The body must include at least one supported mutable field.
- Supported mutable fields are only `title` and `completed`.
- `title`, when present, must be a JSON string and must satisfy the same trim-plus-length rule used on create.
- `completed`, when present, must be a JSON boolean.
- Additional request properties are not allowed.
- Client attempts to mutate `id`, `createdAt`, or `updatedAt` are rejected.
- A body that is valid but normalizes to the already-persisted values is allowed and becomes a successful no-op update.

### `GET` and `DELETE` request bodies

- `GET /api/todos` has no request body.
- `DELETE /api/todos/:id` has no request body.

## Response Schemas

### `Todo`

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
- `id`: backend-generated opaque string.
- `title`: authoritative persisted title after backend trimming and validation.
- `completed`: authoritative persisted completion state.
- `createdAt`: backend-generated RFC 3339 UTC timestamp string set once on create.
- `updatedAt`: backend-generated RFC 3339 UTC timestamp string updated only when stored mutable state changes.

### `ListTodosResponse`

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

Rules:
- `items` is always present.
- `items` is an array of `Todo`.
- An empty collection is represented as `"items": []`.

### `TodoMutationResponse`

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

Rules:
- Used by `POST /api/todos` and `PATCH /api/todos/:id`.
- `todo` is always the authoritative persisted record after the write attempt is resolved.
- For successful no-op `PATCH` requests, `todo` is the unchanged persisted record and `updatedAt` remains unchanged.

### `DeleteTodoResponse`

```json
{
  "deletedId": "todo_123"
}
```

Rules:
- Used by `DELETE /api/todos/:id`.
- `deletedId` echoes the identifier of the removed todo.
- No deleted todo representation is returned because the resource no longer exists after the hard delete.

### `ErrorResponse`

```json
{
  "error": {
    "code": "validation_error",
    "message": "Title must be between 1 and 200 characters.",
    "fieldErrors": {
      "title": [
        "Title must be between 1 and 200 characters."
      ]
    }
  }
}
```

Rules:
- All non-success responses use this envelope.
- `error.code` is a machine-detectable category string.
- `error.message` is a stable summary suitable for logs or direct UI display.
- `error.fieldErrors` is optional and is present only when a failure maps cleanly to one or more request fields.

## Validation And Errors

Validation authority:
- The backend is the source of truth for request validation and mutation rules.
- Frontend validation may mirror these rules for UX, but it must not replace backend validation.

Status and error mapping:

| HTTP status | `error.code` | When used |
| --- | --- | --- |
| `400 Bad Request` | `validation_error` | Invalid JSON body, unsupported content type for `POST` or `PATCH`, missing required fields, unsupported fields, wrong JSON types, empty update body, or title normalization failure. |
| `404 Not Found` | `not_found` | `PATCH` or `DELETE` targets an `id` that does not match a persisted todo. |
| `500 Internal Server Error` | `server_error` | The backend cannot complete an otherwise valid request because of an internal or persistence failure. |

Consistency rules:
- The MVP contract does not define a `409 Conflict` case.
- Unknown or stale todo identifiers are handled as `404 Not Found`, not as a separate malformed-identifier branch.
- A successful no-op `PATCH` response is `200 OK`, not a validation error and not `304 Not Modified`.
- The contract does not expose SQLite table names, column names, file paths, or storage-specific failure details.

## Integration Notes

- Frontend and integration work must target `/api/todos` as the stable collection path for MVP work.
- Frontend code should treat `id` as opaque and should send it back unchanged in `PATCH` and `DELETE` routes.
- Frontend code should use `POST` for create, `PATCH` for edit and complete or uncomplete, and `DELETE` for removal. No separate completion route should be assumed.
- The frontend may reconcile local state directly from successful `POST` and `PATCH` responses without an immediate follow-up fetch.
- The frontend may treat a successful `DELETE` response with `deletedId` as authoritative removal of that item from local state.
- The frontend must not send backend-owned fields and must not make assumptions about SQLite, database file placement, or identifier generation.
- Integration validation should include reload and backend-restart checks to confirm that successful mutations remain visible through later `GET /api/todos` responses.
