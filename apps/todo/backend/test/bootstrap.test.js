const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { initializeTodoDatastore } = require('../src/bootstrap');

function createBootstrapContext() {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-bootstrap-'));

  return {
    databasePath: path.join(tempDirectory, 'nested', 'todos.sqlite'),
    cleanup() {
      fs.rmSync(tempDirectory, { recursive: true, force: true });
    },
  };
}

test('startup-schema: bootstrap initializes schema for a clean database path', () => {
  const context = createBootstrapContext();
  const datastore = initializeTodoDatastore({
    databasePath: context.databasePath,
    idGenerator: () => 'todo_bootstrap',
    now: () => '2026-03-07T14:30:00Z',
  });

  try {
    assert.equal(fs.existsSync(context.databasePath), true);
    assert.equal(datastore.databasePath, path.resolve(context.databasePath));
    assert.deepEqual(datastore.repository.listTodos(), []);

    const createdTodo = datastore.repository.createTodo({ title: 'Bootstrap todo' });
    assert.equal(createdTodo.id, 'todo_bootstrap');
  } finally {
    datastore.close();
    context.cleanup();
  }
});

test('startup-schema: bootstrap is idempotent across repeated startups on the same sqlite file', () => {
  const context = createBootstrapContext();
  const firstDatastore = initializeTodoDatastore({
    databasePath: context.databasePath,
    idGenerator: () => 'todo_restart',
    now: () => '2026-03-07T14:30:00Z',
  });

  try {
    firstDatastore.repository.createTodo({ title: 'Persist me' });
  } finally {
    firstDatastore.close();
  }

  const secondDatastore = initializeTodoDatastore({
    databasePath: context.databasePath,
    now: () => '2026-03-07T14:31:00Z',
  });

  try {
    assert.deepEqual(secondDatastore.repository.listTodos(), [
      {
        id: 'todo_restart',
        title: 'Persist me',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:30:00Z',
      },
    ]);
  } finally {
    secondDatastore.close();
    context.cleanup();
  }
});
