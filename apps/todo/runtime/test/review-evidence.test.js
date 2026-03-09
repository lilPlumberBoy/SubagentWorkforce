const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const REVIEW_EVIDENCE_PATH = path.join(
  REPO_ROOT,
  'apps',
  'todo',
  'docs',
  'design',
  'objectives',
  'basic-application-integration-and-delivery-workflow-connecting-frontend-and-backend',
  'mvp-integration-review-evidence.md'
);

test('review evidence package references the deterministic commands, smoke coverage, and known follow-up notes', () => {
  const packageJson = JSON.parse(
    fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8')
  );
  const evidence = fs.readFileSync(REVIEW_EVIDENCE_PATH, 'utf8');

  assert.equal(
    packageJson.scripts['validate:todo-e2e-smoke'],
    'node --no-warnings --test apps/todo/runtime/test/e2e-smoke.test.js'
  );
  assert.equal(
    packageJson.scripts['validate:todo-review-evidence'],
    'node --no-warnings --test apps/todo/runtime/test/review-evidence.test.js'
  );

  const requiredSnippets = [
    '# MVP Integration Review Evidence',
    '## Reviewer Entry Points',
    'npm run validate:todo-e2e-smoke',
    'npm run validate:todo-review-evidence',
    'npm run validate:todo-runtime-startup',
    'npm run validate:todo-runtime-connectivity',
    'npm run validate:todo-backend-contract',
    'npm run validate:todo-backend-persistence',
    'npm run validate:todo-frontend-flows',
    'npm run validate:todo-frontend-reload',
    '## Integrated Smoke Coverage',
    'add',
    'edit',
    'complete',
    'uncomplete',
    'delete',
    'reload',
    'restart',
    'no-op edit',
    '## Failure Attribution',
    'backend layer',
    'frontend layer',
    'integration layer',
    '## Known Limitations And Follow-Up',
    'frontend-contract-review.md',
    'backend-contract-review.md',
    'application-integration-contract.md',
    'delivery-workflow-and-review-gates.md',
  ];

  for (const snippet of requiredSnippets) {
    assert.match(
      evidence,
      new RegExp(escapeRegExp(snippet)),
      `Expected review evidence to mention "${snippet}".`
    );
  }
});

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
