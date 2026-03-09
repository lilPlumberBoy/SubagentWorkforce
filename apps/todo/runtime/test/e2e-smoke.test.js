const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { startTodoRuntime } = require('../src/runtime');
const { loadTodoRuntimePage } = require('./browser-helpers');

test('e2e smoke covers add, edit, complete, uncomplete, delete, reload persistence, and backend restart durability', async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-e2e-smoke-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');
  let runtime = await startTodoRuntime({
    backendPort: 0,
    databasePath,
    frontendPort: 0,
  });
  let app = await loadTodoRuntimePage(runtime.frontend.url);

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('No todos yet.'));
    });

    assert.deepEqual(await readTodoList(runtime), { items: [] });

    await app.setInputValue('New todo', 'Ship review evidence');
    await app.submitForm('Create todo');

    await app.waitFor(() => {
      assert.ok(app.getByText('Ship review evidence'));
    });

    const createdTodo = await readSingleTodo(runtime);
    assert.equal(createdTodo.title, 'Ship review evidence');
    assert.equal(createdTodo.completed, false);
    assert.equal(createdTodo.createdAt, createdTodo.updatedAt);

    await app.clickButton('Edit Ship review evidence');
    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').value, 'Ship review evidence');
    });

    await app.setInputValue('Edit todo', 'Ship bundled review evidence');
    await app.submitForm('Edit Ship review evidence');

    await app.waitFor(() => {
      assert.ok(app.getByText('Ship bundled review evidence'));
    });

    const editedTodo = await readSingleTodo(runtime);
    assert.equal(editedTodo.id, createdTodo.id);
    assert.equal(editedTodo.title, 'Ship bundled review evidence');
    assert.equal(editedTodo.createdAt, createdTodo.createdAt);

    await app.setCheckboxValue('Ship bundled review evidence', true);

    await app.waitFor(() => {
      assert.equal(
        app.getInputByLabelText('Ship bundled review evidence').checked,
        true
      );
    });

    const completedTodo = await readSingleTodo(runtime);
    assert.equal(completedTodo.completed, true);

    await app.setCheckboxValue('Ship bundled review evidence', false);

    await app.waitFor(() => {
      assert.equal(
        app.getInputByLabelText('Ship bundled review evidence').checked,
        false
      );
    });

    const uncompletedTodo = await readSingleTodo(runtime);
    assert.equal(uncompletedTodo.completed, false);

    await app.clickButton('Edit Ship bundled review evidence');
    await app.waitFor(() => {
      assert.equal(
        app.getInputByLabelText('Edit todo').value,
        'Ship bundled review evidence'
      );
    });

    await app.submitForm('Edit Ship bundled review evidence');

    await app.waitFor(() => {
      assert.ok(app.getByText('Ship bundled review evidence'));
      assert.equal(app.queryByText('Save'), null);
    });

    const noOpTodo = await readSingleTodo(runtime);
    assert.equal(noOpTodo.updatedAt, uncompletedTodo.updatedAt);

    app.cleanup();
    app = await loadTodoRuntimePage(runtime.frontend.url);

    await app.waitFor(() => {
      assert.ok(app.getByText('Ship bundled review evidence'));
      assert.equal(
        app.getInputByLabelText('Ship bundled review evidence').checked,
        false
      );
    });

    app.cleanup();
    app = null;
    await runtime.close();

    runtime = await startTodoRuntime({
      backendPort: 0,
      databasePath,
      frontendPort: 0,
    });
    app = await loadTodoRuntimePage(runtime.frontend.url);

    await app.waitFor(() => {
      assert.ok(app.getByText('Ship bundled review evidence'));
      assert.equal(
        app.getInputByLabelText('Ship bundled review evidence').checked,
        false
      );
    });

    const restartedTodo = await readSingleTodo(runtime);
    assert.equal(restartedTodo.id, createdTodo.id);
    assert.equal(restartedTodo.title, 'Ship bundled review evidence');
    assert.equal(restartedTodo.completed, false);

    await app.clickButton('Delete Ship bundled review evidence');

    await app.waitFor(() => {
      assert.ok(app.getByText('No todos yet.'));
    });

    assert.deepEqual(await readTodoList(runtime), { items: [] });

    app.cleanup();
    app = await loadTodoRuntimePage(runtime.frontend.url);

    await app.waitFor(() => {
      assert.ok(app.getByText('No todos yet.'));
    });
  } finally {
    if (app) {
      app.cleanup();
    }

    if (runtime) {
      await runtime.close();
    }

    fs.rmSync(tempDirectory, { force: true, recursive: true });
  }
});

async function readTodoList(runtime) {
  const response = await fetch(`${runtime.backend.url}/api/todos`);
  assert.equal(response.status, 200);
  return response.json();
}

async function readSingleTodo(runtime) {
  const payload = await readTodoList(runtime);
  assert.equal(payload.items.length, 1);
  return payload.items[0];
}
