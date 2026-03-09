# Backend Architecture Decision Record

Objective: `simple-backend-api-and-persistence-layer-for-storing-todo-items`  
Phase: `design`

This artifact recommends one MVP backend direction for a single-user todo application that must support backend-backed CRUD and persistence across reloads. It is grounded in the resolved design inputs for this task: keep the stack simple, keep persistence reliable for a small MVP, expose a stable interface to the React frontend, and explicitly reject authentication, collaboration, reminders, labels, and complex filtering.

## Recommended MVP Backend

Recommend a single-process Node.js HTTP API using Express with a small SQLite-backed repository layer.

Decision summary:
- Keep one backend service process for local development and MVP delivery.
- Keep the service boundary thin: HTTP routes -> todo service -> SQLite repository.
- Use SQLite as the only source of truth for todo persistence.
- Use modern JavaScript for the service implementation and avoid an ORM for MVP; persistence should stay explicit through a narrow repository module.

Why this is the recommended MVP path:
- It stays aligned with the project's simplicity constraint and the React frontend's JavaScript/TypeScript ecosystem, which reduces tooling and handoff overhead.
- It satisfies the success criterion that todos persist across reloads without introducing a separate database server or deployment dependency.
- It keeps backend ownership clear: the backend owns validation, mutation rules, and durability, while the frontend consumes only the HTTP contract.
- It is reliable enough for a small single-user MVP because SQLite gives transactional writes and durable local storage in one file.
- It gives the frontend a predictable JSON-over-HTTP contract that is easy to consume from React without coupling the UI to storage details.

Why more complex options are rejected for MVP:
- Postgres or another standalone database adds operational setup, configuration, and migration overhead that the single-user scope does not justify.
- A microservice split would create extra contracts and delivery complexity without solving an MVP problem.
- An ORM adds abstraction cost and schema indirection before the model is large enough to benefit from it.

API shape implication for downstream contract work:
- Expose a small REST-style JSON surface under `/api/todos`.
- Keep the resource flat: list, create, update, and delete against one todo collection.
- Return the authoritative persisted todo record after each mutation so the frontend can reconcile state without guessing.
- Reserve exact request or response bodies and status codes for the dedicated API contract artifact, but do not reopen the REST-style collection decision.

## Persistence Choice

Use a file-backed SQLite database on the backend host as the authoritative todo store.

Recommended persistence behavior:
- Create a single `todos` table for the MVP domain.
- Run a simple startup schema initialization or migration step before the server begins serving requests.
- Execute each create, update, completion toggle, and delete operation as an atomic database write.
- Return persisted state from the database after mutations so the API response is authoritative.

Why SQLite is the right persistence choice here:
- It is materially more reliable than a JSON file because it provides transactional semantics, locking, and a well-defined schema.
- It is materially simpler than a networked database because it avoids another running service and still meets the reload-persistence requirement.
- It matches the single-user, single-service MVP assumption well.

Why lighter or heavier persistence options are rejected:
- In-memory storage fails the persistence-across-reloads success criterion.
- Browser-only storage would move persistence ownership out of the backend objective and break the requirement for a backend-backed solution.
- A flat JSON file is easy to start but weak on concurrent access, schema discipline, and failure handling compared with SQLite.
- A client/server database is unnecessary until the project needs multi-user access, remote hosting scale, or cross-process write coordination.

Tradeoff note:
- This choice intentionally optimizes for one deployed backend instance with local disk. If the project later adds multi-user access, horizontal scaling, or external hosting requirements, the persistence design should be revisited through a formal change.

## Component Boundaries

Recommended backend components and responsibilities:

`HTTP/API handling`
- Expose the CRUD-oriented todo endpoints defined by the downstream API contract task.
- Parse JSON requests, enforce content type expectations, and translate domain outcomes into stable HTTP status and error responses.
- Keep transport concerns here only; no SQL or persistence-specific branching in route handlers.

`Todo service / business logic`
- Apply minimal server-side rules for create, edit, complete, uncomplete, list, and delete behavior.
- Normalize mutation flow so route handlers do not encode domain rules.
- Hide persistence details from callers and return backend-owned todo records suitable for contract shaping.

`Persistence / repository`
- Own SQLite connection lifecycle, schema setup, SQL statements, and row-to-domain mapping.
- Keep database structure private to the backend objective; downstream consumers should not depend on table or column names.
- Guarantee that reads come from persisted state and writes commit before success is returned.

`Configuration and runtime`
- Own server port, database file path, and local environment defaults.
- Keep configuration minimal and local-first; no external infrastructure should be required for MVP development.

Request flow:
1. Client calls backend CRUD endpoint.
2. Route validates request shape at the transport boundary and forwards to the todo service.
3. Service applies MVP business rules and invokes the repository.
4. Repository reads or writes SQLite and returns persisted state.
5. Service returns the authoritative result and the route maps it to the HTTP response contract.

Boundary rules for later tasks:
- Field-level schema, validation thresholds, and mutation semantics belong in `todo-data-model.md`.
- Endpoint paths, request bodies, response bodies, and status codes belong in `backend-api-contract.md`.
- Frontend behavior such as optimistic updates, loading states, or retry UX is outside this artifact except where the API contract must constrain it.

## Task Graph

Recommended backend design sequence:
1. `backend-architecture.md` fixes the service shape, persistence direction, and scope guardrails.
2. `todo-data-model.md` defines the MVP todo record, field constraints, and mutation invariants that fit this architecture.
3. `backend-api-contract.md` defines the concrete JSON contract for the `/api/todos` resource using the approved model semantics.
4. `backend-review-gates.md` defines the validation path that proves persistence, CRUD correctness, and contract stability before MVP build.

Downstream build dependency notes:
- Backend implementation should not begin until the data model and API contract artifacts accept the single-process Express plus SQLite decision.
- Frontend integration can proceed against mocked or documented `/api/todos` responses once the API contract is approved.
- No additional backend role split is recommended for MVP; the current backend manager and backend worker structure is sufficient for the remaining design and build steps.

## Validation and Review Gates

The backend design should be considered review-ready only if all of the following gates are satisfied:
- `Architecture gate`: reviewers confirm the stack decision is singular and explicit: Express service, SQLite persistence, no ORM, no extra infrastructure.
- `Scope gate`: reviewers confirm the design stays within single-user todo CRUD and does not add authentication, collaboration, reminders, labels, due dates, or complex filtering.
- `Contract gate`: the API contract uses one flat todo collection with JSON request and response bodies that do not expose SQLite-specific details.
- `Durability gate`: the planned validation path proves that create, edit, complete or uncomplete, and delete operations persist across server restarts and page reloads.
- `Backend ownership gate`: validation, mutation rules, and persistence responsibilities remain backend-owned, while frontend concerns stay limited to consuming the contract.

Minimum evidence expected from later tasks:
- Data model review shows the todo schema remains minimal and supports only the MVP flows in the success criteria.
- API contract review shows deterministic status handling for CRUD operations and authoritative post-mutation responses.
- Build validation proves the SQLite file is created or initialized automatically, CRUD writes commit successfully, and persisted data survives process restart.

## Non-Goals

The MVP backend design explicitly does not include:
- Authentication, authorization, user accounts, or per-user data partitioning.
- Multi-user collaboration or real-time synchronization.
- Reminders, due dates, calendars, labels, search, or complex filtering.
- Background jobs, message queues, caches, or event-driven architecture.
- Multi-service decomposition, distributed persistence, or horizontal write scaling.
- Production-hardening features beyond basic logging, validation, and deterministic CRUD behavior.

## Open Questions

- The exact todo field set and validation thresholds still need to be fixed in the separate data model artifact.
- The exact endpoint surface and error contract still need to be fixed in the separate backend API contract artifact.
- The final database file location and test reset strategy still need to be defined in build and integration planning.
