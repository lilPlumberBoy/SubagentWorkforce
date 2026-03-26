# Backend Discovery Brief

## MVP Scope

The backend responsibility for the first version is limited to storing and serving a single-user todo list for the React frontend. The MVP backend surface should cover create, read, update, and delete operations for todo items, plus the minimum persistence behavior needed so items survive process restarts.

Within that scope, backend work should assume:
- The React frontend is the primary consumer and needs a simple request/response API for listing todos and submitting CRUD changes.
- The persisted todo model stays minimal for MVP: an identifier, the todo content needed for display, and completion state are in scope; richer fields remain optional and should not be assumed.
- Basic input validation is in scope only to keep stored data usable for the frontend workflow.
- Persistence must be simple, local, and easy to review for the MVP; the exact storage technology remains an open stack choice during discovery.
- Testing is in scope at a basic reliability level for CRUD behavior, validation, and persistence continuity, but not at production-scale performance or resilience depth.

## API Surface Assumptions

These are minimum consumer-facing assumptions for discovery and should be treated as an assumption surface rather than a locked contract:
- The frontend needs a way to fetch the current todo collection.
- The frontend needs a way to create a new todo item.
- The frontend needs a way to update an existing todo item, including toggling completion and editing the displayed content.
- The frontend needs a way to delete an existing todo item.
- Responses should be structured and predictable enough for the React client to render current state and handle basic validation failures.
- The API can assume a single-user MVP context with no authentication, tenancy, or per-user isolation in the first version.
- Error handling only needs to cover straightforward cases such as invalid input or requests for a todo item that does not exist.

Open backend discovery questions that remain intentionally unresolved:
- Which backend runtime and framework should own the CRUD API.
- Which persistence mechanism best fits the MVP review and setup constraints.
- Whether the API returns whole-resource payloads, collection refresh payloads, or another minimal response shape after writes.

## Explicit Exclusions

The backend first version explicitly excludes:
- Multi-user data partitioning, collaboration, or synchronization concerns.
- Authentication, authorization, sessions, or identity management.
- Real-time delivery mechanisms such as websockets, subscriptions, or push notifications.
- Non-MVP todo features such as due dates, reminders, labels, rich filtering, or workflow automation.
- Advanced operational concerns such as horizontal scaling, distributed storage, background jobs, or high-availability design.
- Detailed performance tuning, analytics, auditing, or compliance-oriented retention behavior.

## Dependencies And Handoff Notes

- The frontend/integration teams can assume backend ownership covers only the minimal todo CRUD and persistence boundary described above.
- Contract-level details beyond these assumptions should remain open until the shared API contract asset is resolved.
- A task-contract inconsistency remains open: the owned output path is `apps/todo/backend/discovery/backend-discovery-brief.md`, while the task validation commands reference `backend/discovery/backend-discovery-brief.md`.
