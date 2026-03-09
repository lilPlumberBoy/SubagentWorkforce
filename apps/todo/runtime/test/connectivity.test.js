const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { startTodoRuntime } = require('../src/runtime');
const { loadTodoRuntimePage } = require('./browser-helpers');

test('runtime connectivity serves the frontend, wires the backend origin, and supports CRUD through the integrated runtime', async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-runtime-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');
  const runtime = await startTodoRuntime({
    backendPort: 0,
    databasePath,
    frontendPort: 0,
  });

  try {
    const preflightResponse = await fetch(`${runtime.backend.url}/api/todos`, {
      headers: {
        'access-control-request-headers': 'content-type',
        'access-control-request-method': 'POST',
        origin: runtime.frontend.url,
      },
      method: 'OPTIONS',
    });

    assert.equal(preflightResponse.status, 204);
    assert.equal(
      preflightResponse.headers.get('access-control-allow-origin'),
      runtime.frontend.url
    );

    const app = await loadTodoRuntimePage(runtime.frontend.url);

    try {
      await app.waitFor(() => {
        assert.ok(app.getByText('No todos yet.'));
      });

      await app.setInputValue('New todo', 'Ship MVP runtime');
      await app.submitForm('Create todo');

      await app.waitFor(() => {
        assert.ok(app.getByText('Ship MVP runtime'));
      });

      await app.clickButton('Edit Ship MVP runtime');

      await app.waitFor(() => {
        assert.ok(app.getByText('Save'));
      });

      await app.setInputValue('Edit todo', 'Ship integrated MVP runtime');
      await app.submitForm('Edit Ship MVP runtime');

      await app.waitFor(() => {
        assert.ok(app.getByText('Ship integrated MVP runtime'));
      });

      await app.setCheckboxValue('Ship integrated MVP runtime', true);

      await app.waitFor(() => {
        assert.equal(
          app.getInputByLabelText('Ship integrated MVP runtime').checked,
          true
        );
      });

      const persistedListResponse = await fetch(`${runtime.backend.url}/api/todos`);
      assert.equal(persistedListResponse.status, 200);

      const persistedList = await persistedListResponse.json();
      assert.equal(persistedList.items.length, 1);
      assert.equal(persistedList.items[0].title, 'Ship integrated MVP runtime');
      assert.equal(persistedList.items[0].completed, true);

      await app.clickButton('Delete Ship integrated MVP runtime');

      await app.waitFor(() => {
        assert.ok(app.getByText('No todos yet.'));
      });
    } finally {
      app.cleanup();
    }
  } finally {
    await runtime.close();
    fs.rmSync(tempDirectory, { force: true, recursive: true });
  }
});
