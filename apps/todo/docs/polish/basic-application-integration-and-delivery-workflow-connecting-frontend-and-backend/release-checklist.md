# Integration Release Checklist

Objective: `basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend`  
Phase: `polish`

This is the final integrated release checklist for the todo MVP.

## One-Command Release Validation

Run:

```bash
npm run validate:todo-release-readiness
```

That command runs:
1. `npm run lint`
2. `CI=1 npm test`
3. `npm run build`
4. `npm run validate:todo-e2e-smoke`

## Manual Runtime Check

1. Start the app with `npm run todo-runtime:start`.
2. Open the reported `frontend.url`.
3. Verify create, edit, complete, uncomplete, delete, reload, and restart persistence.
4. Stop the runtime with `Ctrl+C`.

## Supporting Docs

- `apps/todo/docs/polish/react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items/frontend-release-readiness.md`
- `apps/todo/docs/polish/simple-backend-api-and-persistence-layer-for-storing-todo-items/backend-operations-handoff.md`
- `apps/todo/docs/design/objectives/basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend/mvp-integration-review-evidence.md`

## Release Gate

The release handoff is ready when:
- `npm run validate:todo-release-readiness` passes
- the integrated runtime starts cleanly
- the manual CRUD checklist is completed
- no new scope is introduced during polish
