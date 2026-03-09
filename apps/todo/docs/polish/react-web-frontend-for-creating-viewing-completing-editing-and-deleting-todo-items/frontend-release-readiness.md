# Frontend Release Readiness

Objective: `react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items`  
Phase: `polish`

This handoff keeps the frontend polish pass narrow: manual QA, keyboard and focus expectations, and the exact commands that should stay green before release review.

## Manual QA Checklist

1. Start the integrated runtime with `npm run todo-runtime:start`.
2. Open the reported `frontend.url`.
3. Confirm the empty state shows `No todos yet.` before any create action.
4. Create a todo and confirm it appears immediately without a full page refresh.
5. Edit the todo, save it, and confirm the updated title appears.
6. Re-enter edit mode, change the text, cancel, and confirm the persisted title stays unchanged.
7. Complete the todo, then uncomplete it, and confirm both states render correctly.
8. Delete the todo and confirm the empty state returns.
9. Refresh the page and confirm persistence is preserved.
10. Restart the runtime against the same database path and confirm the persisted state still renders.

## Keyboard And Focus Expectations

- After a successful create, focus should return to the new-todo input.
- Entering edit mode should focus and select the inline edit input.
- Canceling an edit should return focus to that row's edit control.
- Saving an edit should return focus to the row's edit control for the updated title.
- Deleting a row should move focus to the next available row action; if the list becomes empty, focus should return to the create input.
- Duplicate row submissions should be blocked while save, toggle, or delete work is already in flight.

## Frontend Validation Commands

Primary polish validation commands:
- `npm run validate:todo-frontend-flows`
- `npm run validate:todo-frontend-editing`
- `npm run validate:todo-frontend-reload`

Supporting references:
- `apps/todo/docs/design/objectives/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/mvp-frontend-review-evidence.md`
- `runs/todo-react-draft/reports/task_fe_04_verification_evidence.json`
- `runs/todo-react-draft/reports/T4_frontend_review_gates_and_build_handoff.json`

## Scope Guardrails

- Do not add new frontend behaviors in polish.
- Keep validation and docs aligned to the approved MVP CRUD surface only.
- Defer ordering, exact title-rule, and stale-error naming semantics to the accepted shared contract rather than inventing new local rules.
