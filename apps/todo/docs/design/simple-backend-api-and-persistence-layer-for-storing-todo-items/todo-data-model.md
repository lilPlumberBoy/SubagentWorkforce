# Todo Data Model

Objective: `simple-backend-api-and-persistence-layer-for-storing-todo-items`  
Phase: `design`  
Role: `backend-worker`

This artifact fixes the backend-owned todo record, SQLite storage shape, and mutation rules for the MVP. It is intentionally minimal: one todo entity, one table, and only the fields required for create, list, edit, complete, uncomplete, and delete behavior.

One required resolved input was not actually supplied:
- `Objective details that the backend should keep the model simple and own persistence details`

The decisions below therefore rely on the approved backend architecture artifact, the success criteria for CRUD plus persistence, and the explicit constraint that the frontend must consume a clear backend contract without guessing storage behavior.

## Todo Entity

Authoritative persisted todo entity:

```json
{
  "id": "opaque-backend-id",
  "title": "Buy milk",
  "completed": false,
  "createdAt": "2026-03-07T14:30:00Z",
  "updatedAt": "2026-03-07T14:30:00Z"
}
```

Field definition:

| Field | Type | Ownership | Rules |
| --- | --- | --- | --- |
| `id` | string | backend-generated | Stable for the life of the todo, unique among live todos, never client-editable. |
| `title` | string | client-supplied, backend-normalized | Persist the trimmed title text. Internal spacing is preserved. |
| `completed` | boolean | backend-owned mutable state | Defaults to `false` on create. Can later be set to `true` or `false`. |
| `createdAt` | RFC 3339 UTC timestamp string | backend-generated | Set once on create and never changes after insert. |
| `updatedAt` | RFC 3339 UTC timestamp string | backend-generated | Set on create and updated only when persisted mutable state actually changes. |

Entity invariants:
- The MVP todo model exposes exactly five fields: `id`, `title`, `completed`, `createdAt`, and `updatedAt`.
- No due date, description, notes, labels, priority, user ownership, reminder, archive, or soft-delete fields exist in the MVP model.
- `createdAt` and `updatedAt` are equal at initial creation time.
- `title` and `completed` are the only mutable business fields in the MVP.
- Delete is modeled as removal of the todo record, not as a state transition on the entity.

Identifier note:
- The backend public contract should treat `id` as an opaque string.
- The storage layer must generate identifiers internally before insert.
- The exact generation algorithm is an implementation choice for build work so long as uniqueness and immutability are preserved.

## Storage Schema

SQLite is the sole MVP persistence store. The backend owns the table and column names; they are not part of the public API contract.

Recommended MVP table:

```sql
CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (length(title) BETWEEN 1 AND 200)
);
```

Storage mapping rules:
- `id` stores the backend-generated opaque identifier as text.
- `title` stores the already-normalized persisted title string.
- `completed` stores `0` for incomplete and `1` for complete.
- `created_at` and `updated_at` store UTC timestamps in RFC 3339 string form so they can map directly into the backend entity without timezone guessing.

Schema notes:
- One `todos` table is sufficient for the MVP. No join tables or history tables are required.
- No `deleted_at` or `is_deleted` column is allowed in the MVP schema because delete is a hard delete.
- No secondary indexes are required for the MVP because the dataset is expected to stay small and the default list query is simple.
- Default read ordering should be `ORDER BY created_at ASC, id ASC` so reloads return stable output even when two rows share the same timestamp string.

## Validation Rules

Backend validation is authoritative for all mutations. Frontend validation may mirror these rules for UX, but the backend remains the source of truth.

Create validation:
- The request must supply `title`.
- `title` must be a string.
- The backend trims leading and trailing whitespace before validation and persistence.
- The normalized `title` must contain at least 1 character and at most 200 characters.
- No client-supplied `id`, `createdAt`, `updatedAt`, or `completed` field is accepted on create.

Update validation:
- The request must include at least one supported mutable field.
- Supported mutable fields are only `title` and `completed`.
- When `title` is present, it must be a string and must satisfy the same trim-plus-length rule used on create.
- When `completed` is present, it must be a boolean.
- Attempts to send unsupported fields, or to mutate backend-owned fields such as `id`, `createdAt`, or `updatedAt`, are invalid and must be rejected by the API contract rather than ignored silently.

Identifier validation boundary:
- Domain logic requires a non-empty backend identifier that can match exactly one persisted row.
- Public malformed-identifier handling belongs to the API contract artifact because it affects route parsing and HTTP status behavior.
- The data model does not require clients to understand or construct identifier values.

## Lifecycle And Mutations

List semantics:
- List returns every persisted, non-deleted todo.
- The default list order is creation order ascending, with `id` ascending as a stable tie-breaker.
- List reads return persisted state only; there is no derived client-only projection in the backend model.

Create semantics:
- Normalize and validate `title`.
- Generate `id` inside the backend.
- Set `completed` to `false`.
- Set `createdAt` and `updatedAt` to the same current UTC timestamp.
- Insert the row atomically and return the persisted record.

Edit semantics:
- Edit is a partial update over the mutable field set.
- A request may update `title`, `completed`, or both in one mutation.
- When `title` is updated, the backend persists the normalized trimmed value.
- When at least one effective field value changes, the backend updates `updatedAt` to the current UTC timestamp in the same atomic write as the field change.
- When a request is otherwise valid but all supplied values match the persisted values after normalization, the backend may return the unchanged record as a successful no-op and must leave `updatedAt` unchanged.

Complete and uncomplete semantics:
- Complete is the same persisted mutation as setting `completed` to `true`.
- Uncomplete is the same persisted mutation as setting `completed` to `false`.
- Both flows use the same validation and timestamp rules as any other edit.

Delete semantics:
- Delete removes the row from the `todos` table in one atomic write.
- After a successful delete, the todo must no longer appear in list results.
- The model defines no restore flow and no tombstone record for the MVP.
- Missing-target delete behavior is part of the API contract, but the model assumes deletion succeeds only when an existing persisted row is removed.

## Consistency Notes

- The backend entity is the source of truth for todo state. The frontend should only depend on the five entity fields and mutation outcomes, not on SQLite-specific column names or boolean storage encoding.
- Storage internals remain private: `created_at` and `updated_at` are backend implementation details even though they map directly to `createdAt` and `updatedAt` in the entity.
- The API contract should preserve that only `title` and `completed` are client-writable, while `id`, `createdAt`, and `updatedAt` are backend-owned.
- The API contract should preserve the trim-before-persist rule and the normalized title length bound of 1 to 200 characters.
- The API contract should explicitly decide how successful no-op updates are represented over HTTP while preserving the model rule that `updatedAt` changes only when stored state changes.
- Build and integration work still need to define the SQLite file path, schema initialization timing, and test-reset strategy around this model.
