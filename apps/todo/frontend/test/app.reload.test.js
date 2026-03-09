const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const { startTodoHttpServer } = require('../../backend/src/server');
const { createTodoApiClient } = require('../src');
const { renderTodoApp } = require('./dom-helpers');

test('todo app reloads persisted todos on mount and reflects backend restart durability', async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-frontend-reload-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');
  let server = await startTodoHttpServer({
    databasePath,
    host: '127.0.0.1',
    idGenerator: (() => {
      const ids = ['todo_reload'];
      return () => ids.shift();
    })(),
    now: (() => {
      const timestamps = [
        '2026-03-08T12:00:00Z',
        '2026-03-08T12:01:00Z',
        '2026-03-08T12:02:00Z',
      ];
      return () => timestamps.shift();
    })(),
    port: 0,
  });

  const firstClient = createTodoApiClient({ baseUrl: server.url });
  const firstApp = await renderTodoApp({ client: firstClient });

  try {
    await firstApp.waitFor(() => {
      assert.ok(firstApp.getByText('No todos yet.'));
    });

    await firstApp.setInputValue('New todo', 'Persist me');
    await firstApp.submitForm('Create todo');

    await firstApp.waitFor(() => {
      assert.ok(firstApp.getByText('Persist me'));
    });

    await firstApp.clickButton('Edit Persist me');
    await firstApp.waitFor(() => {
      assert.equal(firstApp.getInputByLabelText('Edit todo').value, 'Persist me');
    });

    await firstApp.setInputValue('Edit todo', 'Persisted after reload');
    await firstApp.clickButton('Save');

    await firstApp.waitFor(() => {
      assert.ok(firstApp.getByText('Persisted after reload'));
    });

    await firstApp.setCheckboxValue('Persisted after reload', true);

    await firstApp.waitFor(() => {
      const checkbox = firstApp.getInputByLabelText('Persisted after reload');
      assert.equal(checkbox.checked, true);
      assert.equal(checkbox.disabled, false);
    });
  } finally {
    await firstApp.cleanup();
    await server.close();
  }

  server = await startTodoHttpServer({
    databasePath,
    host: '127.0.0.1',
    port: 0,
  });

  const secondClient = createTodoApiClient({ baseUrl: server.url });
  const secondApp = await renderTodoApp({ client: secondClient });

  try {
    await secondApp.waitFor(() => {
      assert.ok(secondApp.getByText('Persisted after reload'));
      assert.equal(
        secondApp.getInputByLabelText('Persisted after reload').checked,
        true
      );
    });

    await secondApp.clickButton('Delete Persisted after reload');

    await secondApp.waitFor(() => {
      assert.ok(secondApp.getByText('No todos yet.'));
    });

    const persistedTodos = await secondClient.listTodos();
    assert.deepEqual(persistedTodos, { items: [] });
  } finally {
    await secondApp.cleanup();
    await server.close();
    fs.rmSync(tempDirectory, { force: true, recursive: true });
  }
});
