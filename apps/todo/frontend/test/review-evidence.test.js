const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const EVIDENCE_PATH = path.resolve(
  __dirname,
  '..',
  '..',
  'docs',
  'design',
  'objectives',
  'react-web-frontend-for-creating-viewing-completing-editing-and-deleting-todo-items',
  'mvp-frontend-review-evidence.md'
);

test('frontend review evidence references the required validation commands and approved design package', () => {
  const evidence = fs.readFileSync(EVIDENCE_PATH, 'utf8');

  assert.match(evidence, /npm run lint/);
  assert.match(evidence, /CI=1 npm test/);
  assert.match(evidence, /npm run build/);
  assert.match(evidence, /apps\/todo\/frontend\/test\/app\.shell-list-create\.test\.js/);
  assert.match(evidence, /apps\/todo\/frontend\/test\/app\.toggle-delete\.test\.js/);
  assert.match(evidence, /apps\/todo\/frontend\/test\/app\.editing\.test\.js/);
  assert.match(evidence, /runs\/todo-react-draft\/reports\/T1_frontend_mvp_interaction_spec\.json/);
  assert.match(evidence, /runs\/todo-react-draft\/reports\/T2_frontend_api_dependency_contract\.json/);
  assert.match(evidence, /runs\/todo-react-draft\/reports\/T3_frontend_component_state_architecture\.json/);
  assert.match(evidence, /runs\/todo-react-draft\/reports\/T4_frontend_review_gates_and_build_handoff\.json/);
});
