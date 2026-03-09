# Backend Operations Handoff

Objective: `simple-backend-api-and-persistence-layer-for-storing-todo-items`  
Phase: `polish`

This handoff packages the accepted backend MVP for release review. It is intentionally operational, not architectural.

## Startup Notes

- Start the backend alone with `npm run todo-backend:start`.
- By default it binds to `127.0.0.1:3000`.
- Override host or port with `HOST` and `PORT`.
- Allow a browser origin with `TODO_ALLOWED_ORIGIN` when running the backend outside the integrated runtime.

## Database Behavior

- The default SQLite file is `apps/todo/backend/data/todos.sqlite`.
- Override the database path with `TODO_BACKEND_DB_PATH`.
- The backend creates missing parent directories automatically.
- Schema bootstrap is idempotent; repeated startups against the same database file are supported.

## Contract And Validation Boundaries

- Collection route: `GET /api/todos`
- Create route: `POST /api/todos`
- Update route: `PATCH /api/todos/:id`
- Delete route: `DELETE /api/todos/:id`
- `title` is trimmed and must be between 1 and 200 characters.
- `completed` must be a boolean when provided.
- Unsupported routes return a structured `not_found` error payload.

## Failure Localization

Run these commands in order:
- `npm run validate:todo-backend-contract`
- `npm run validate:todo-backend-persistence`
- `npm run validate:todo-backend-bootstrap`

Interpretation:
- If contract fails, treat it as an API envelope or validation regression.
- If persistence fails, treat it as a SQLite bootstrap, restart, or CRUD durability regression.
- If bootstrap fails alone, treat it as a schema or database-path regression before looking at higher layers.

## Scope Guardrails

- Do not add routes, auth, filtering, or collaboration behavior in polish.
- Keep backend polish focused on startup, storage, validation, and restart durability for the accepted MVP.
