# MVP Integration Review Evidence

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `mvp-build`  
Role: `middleware-worker`

This package gives acceptance review one deterministic integrated smoke path plus the smaller follow-up commands needed to localize failures without reopening scope.

## Reviewer Entry Points

Primary acceptance commands:
- `npm run validate:todo-e2e-smoke`
- `npm run validate:todo-review-evidence`

Supporting isolation commands when the integrated smoke fails:
- `npm run validate:todo-runtime-startup`
- `npm run validate:todo-runtime-connectivity`
- `npm run validate:todo-backend-contract`
- `npm run validate:todo-backend-persistence`
- `npm run validate:todo-frontend-flows`
- `npm run validate:todo-frontend-reload`

Authoritative reference documents for the workflow under review:
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/application-integration-contract.md`
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/delivery-workflow-and-review-gates.md`

## Integrated Smoke Coverage

`npm run validate:todo-e2e-smoke` drives the full approved MVP path against the integrated runtime created by `apps/todo/runtime/src/runtime.js`.

Observed smoke coverage:
1. Starts from an empty SQLite-backed store and confirms `GET /api/todos` returns `{"items":[]}`.
2. Uses the served frontend page to add one todo and confirms the backend stores the created item with `completed: false`.
3. Uses the frontend edit flow and confirms the backend returns the updated persisted title.
4. Uses the frontend checkbox flow to complete the todo and confirms the backend persists `completed: true`.
5. Uses the same checkbox flow to uncomplete the todo and confirms the backend persists `completed: false`.
6. Uses a no-op edit through the frontend and confirms the backend keeps `updatedAt` unchanged.
7. Reloads the frontend page and confirms the persisted todo rehydrates from the backend.
8. Restarts the integrated runtime against the same database path and confirms the todo still appears through both `GET /api/todos` and the frontend page.
9. Uses the frontend delete flow, confirms `GET /api/todos` becomes empty, then reloads once more and confirms the empty state persists.

The smoke intentionally stays on approved MVP behaviors only: add, edit, complete, uncomplete, delete, reload persistence, and restart durability.

## Failure Attribution

Use this order so failures remain attributable to one layer instead of becoming a mixed investigation:

- `backend layer`: run `npm run validate:todo-backend-contract` and `npm run validate:todo-backend-persistence`. These isolate CRUD envelopes, validation semantics, no-op behavior, and restart durability at `/api/todos` without the browser host.
- `frontend layer`: run `npm run validate:todo-frontend-flows` and `npm run validate:todo-frontend-reload`. These isolate the React feature module, its shared-client wiring, and reload behavior without the integrated frontend host.
- `integration layer`: run `npm run validate:todo-runtime-startup` and `npm run validate:todo-runtime-connectivity`. These isolate startup wiring, runtime config injection, CORS allowance, and frontend-to-backend connectivity for the integrated host.

Practical triage rule:
- If the integrated smoke fails and backend or frontend layer commands also fail, route the issue to that owning layer first.
- If the integrated smoke fails while the backend and frontend layer commands pass but a runtime command fails, treat it as an integration-layer regression.
- If all supporting commands pass, re-run the integrated smoke against a clean temp database path before escalating because the smoke itself already exercises the full approved path deterministically.

## Known Limitations And Follow-Up

- No new MVP behavior gaps were discovered during this validation task.
- Historical design review notes in `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/frontend-contract-review.md` and `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/backend-contract-review.md` still describe legacy `/todos` route names. If those notes are bundled for acceptance, refresh them or mark them as superseded by `application-integration-contract.md` and `delivery-workflow-and-review-gates.md`.
- MVP scope remains intentionally narrow: no auth, collaboration, reminders, labels, due dates, filtering, or real-time sync behavior belongs in this review bundle.
