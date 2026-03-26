# Backend Persistence Constraints Draft

## Scope

This discovery draft defines the minimum backend persistence constraints for an MVP todo service. It stays technology-agnostic and focuses on the storage and validation guarantees needed for create, read, update, toggle-complete, and delete flows to survive page reloads and normal service restarts.

## MVP Persistence Boundary

- The backend is the system of record for todo items; frontend state alone is not sufficient for durability.
- Persistence must cover the full CRUD lifecycle: create, list/read, update text, toggle completion state, and delete.
- A successful mutation response must mean the resulting state is durably committed and available to later reads after a page reload.
- Storage must start cleanly when no todos exist yet and must not require manual seeding for basic operation.
- Service bootstrap must be idempotent and must not erase previously committed todos during normal restarts.

## Minimum Todo Record Constraints

- Each todo item needs a stable backend-generated identifier that remains consistent across reads, updates, completion toggles, deletes, and reloads.
- Each todo item needs persisted user-visible text content.
- Each todo item needs a persisted completion flag.
- The identifier is immutable once created.
- If internal metadata is used for auditing, ordering, or write tracking, it should not be required as part of the MVP user contract unless another team explicitly adopts it.

## Server-Side Validation Constraints

- Validation must run on the server for every create and update request, regardless of frontend behavior.
- Todo text must be required, must not be empty after normalization, and should be stored in a canonical form that avoids blank-only records.
- Completion state must be validated as a boolean when provided.
- Updates must reject attempts to mutate immutable record identity.
- The backend should reject malformed payloads with explicit client error responses rather than coercing ambiguous input.
- Validation failures must not partially mutate stored data.

## Consistency and Durability Constraints

- Each mutation must be atomic at the single-item level; partial writes are not acceptable for MVP CRUD operations.
- After a successful create, update, completion toggle, or delete response, the next read from the same backend instance must reflect that committed result.
- Failed persistence operations must return a server error and leave the last committed state intact.
- Delete must be durable in the same way as create and update; a deleted item must not reappear after restart unless recovery tooling is intentionally introduced later.
- Storage access must serialize or otherwise safely handle near-simultaneous writes so that rapid sequential requests do not corrupt records.

## Operational Assumptions

- MVP scope is effectively single-user and does not require multi-tenant isolation, authentication, or cross-device conflict resolution.
- Discovery does not require multi-node replication, distributed consistency, or high-availability failover.
- Persistence only needs to survive normal local or single-deployment restarts; disaster recovery and backup policy can be deferred to later phases.
- Local development and MVP delivery both need a writable durable storage location controlled by backend startup configuration.

## Error Handling Expectations

- The backend contract should distinguish validation errors, not-found errors, and storage/runtime failures.
- Reads should return an empty collection when no todos exist, not a bootstrap failure.
- Update, toggle, and delete operations against a missing identifier should return a deterministic not-found result.
- Storage initialization errors should fail fast during startup rather than silently falling back to in-memory behavior.

## Open Questions

- Which storage class best fits MVP delivery constraints: embedded database, structured file store, or managed service?
- Does the API contract need exposed timestamps or version fields for ordering/debugging, or can those remain internal?
- What maximum todo text length and request body size should be enforced for MVP?
- Does deployment require any backup/export expectation before the first post-MVP phase?

## Dependency Impacts

- Middleware should preserve a contract with stable identifiers, validated text fields, boolean completion state, and clear error categories.
- Frontend should treat backend responses as the durable source of truth and should not assume writes are committed until the API confirms success.
- Delivery planning must account for writable storage provisioning, startup initialization, and file/path permissions without hard-coding a specific storage technology during discovery.

## Evidence From Owned Paths

- `apps/todo/backend/data/todos.sqlite` indicates the current workspace already expects some durable backend-owned storage artifact.
- `apps/todo/backend/scripts/bootstrap.js` exposes datastore initialization and returns a storage path, which supports the need for explicit bootstrap and durability checks.
- `apps/todo/backend/scripts/start.js` reports a storage path at server startup, which supports carrying storage configuration and startup-failure behavior into later design work.
