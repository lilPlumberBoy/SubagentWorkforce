# Backend Open Decisions Register

## Scope

This register captures backend discovery questions that remain intentionally unresolved for the todo MVP. It does not choose a stack or finalize the API contract. Its purpose is to make the open decisions explicit so design and implementation can sequence work without accidental scope growth.

## Decision Register

| Decision area | Open question | Why it matters to the MVP | Decision criteria | Downstream work impacted | Latest responsible phase | Blocks implementation if unresolved? |
| --- | --- | --- | --- | --- | --- | --- |
| Framework and runtime | Which backend framework and runtime should expose the todo API to the React frontend? | The framework choice sets request handling patterns, validation approach, project structure, local development workflow, and deployment assumptions. A mismatch here can create avoidable rework for the API contract and persistence wiring. | Keep the stack simple, favor straightforward architecture over optimization, support reliable CRUD endpoints, and align with a small-team maintenance burden. | API/interface contract definition, service bootstrap, request validation, error handling, local run instructions, and deployment packaging. | Design | Yes. Endpoint implementation should not start until the framework boundary is agreed. |
| API contract shape | What API shape should the frontend consume for creating, listing, updating, and deleting todo items? | The frontend and backend need a clear integration contract. Leaving the resource model, payload fields, and error semantics vague increases coordination risk and can create duplicated assumptions across teams. | Minimize surface area, keep CRUD flows obvious for a React client, define stable request/response payloads, and make failure modes explicit enough for UI handling. | Shared API contract artifact, frontend data access layer, backend route definitions, validation rules, and acceptance criteria. | Design | Yes. Frontend/backend implementation should not diverge on payloads or endpoint behavior. |
| Persistence approach | What persistence mechanism is simplest and reliable enough for the MVP? | Storage choice determines setup complexity, durability expectations, concurrency behavior, and how much operational overhead the small app inherits. The planning inputs explicitly leave this open. | Simplicity for a small test application, acceptable reliability for stored todo items, low operational overhead, and clean support for the agreed CRUD contract. | Data access layer, local developer setup, migration strategy if any, backup/recovery expectations, and test environment setup. | Design | Yes. Implementation should not proceed without knowing the persistence boundary. |
| Minimal data model and lifecycle rules | What is the minimum persisted todo schema and what lifecycle behavior is in scope? | Even a simple todo app needs clarity on required fields and update semantics to avoid quiet scope expansion. Discovery inputs call out the need to prevent optional features from slipping into the MVP. | Keep the model minimal, support the chosen API shape, reject optional features unless explicitly approved, and make default state transitions unambiguous. | Schema definition, request validation, persistence mapping, API examples, and frontend form/state assumptions. | Design | Yes. CRUD implementation depends on agreed fields and lifecycle semantics. |
| Authentication boundary | Is user authentication explicitly out of scope for the first version? | The planning inputs identify this as a known unknown. If this stays ambiguous, the backend may over-design data ownership, authorization checks, or session/token handling for an MVP that does not need them. | Confirm whether the MVP is single-user or anonymous, ensure scope stays aligned with the simple-app constraint, and document the future extension point if auth is deferred. | API authorization model, data model ownership fields, route middleware, and acceptance scenarios. | Discovery or early design | Yes, if unresolved when API and schema design begin. No, if explicitly ruled out before design finalizes. |
| Testing depth and backend validation strategy | What testing depth is expected for the backend at MVP scope? | The planning inputs explicitly leave testing depth open. Without a minimum expectation, teams can either under-test core CRUD reliability or over-invest in heavy test infrastructure. | Cover the reliability risks that matter for a small CRUD service, keep the approach proportional to MVP scope, and align tests with the chosen framework and persistence mechanism. | Test plan, fixture strategy, CI expectations, persistence test setup, and acceptance evidence. | Design | No for initial discovery. Yes before implementation completes, because test obligations influence service and persistence seams. |

## Decisions That Need Early Resolution

These decisions should be settled before implementation begins because they define shared technical boundaries:

- Framework and runtime
- API contract shape
- Persistence approach
- Minimal data model and lifecycle rules
- Authentication boundary

## Decisions That Can Wait Until Design Finalization

These decisions do not need to be resolved during discovery, but they should be closed during design so implementation and acceptance do not drift:

- Testing depth and backend validation strategy

## Notes For Downstream Design

- Keep the backend scope aligned to a simple CRUD todo service for a React frontend.
- Avoid locking in specialized infrastructure unless the chosen persistence option clearly requires it.
- Treat optional features as out of scope until the objective manager or design artifacts explicitly add them.
