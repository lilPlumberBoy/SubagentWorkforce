const test = require('node:test');
const assert = require('node:assert/strict');

const { TODO_CLIENT_ERROR_CODES } = require('../src');
const { renderTodoApp } = require('./dom-helpers');

test('todo app keeps toggle interactions response-driven and recovers from retryable row errors', async () => {
  const toggleRequest = createDeferred();
  const toggleCalls = [];
  const persistedTodo = {
    completed: false,
    createdAt: '2026-03-08T11:00:00Z',
    id: 'todo_toggle',
    title: 'Ship toggle coverage',
    updatedAt: '2026-03-08T11:00:00Z',
  };

  const client = {
    async listTodos() {
      return { items: [persistedTodo] };
    },
    async setTodoCompleted(input) {
      toggleCalls.push(input);
      return toggleRequest.promise;
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('Ship toggle coverage'));
    });

    await app.setCheckboxValue('Ship toggle coverage', true);

    assert.equal(toggleCalls.length, 1);
    assert.deepEqual(toggleCalls[0], {
      completed: true,
      id: 'todo_toggle',
    });

    await app.waitFor(() => {
      const checkbox = app.getInputByLabelText('Ship toggle coverage');
      assert.equal(checkbox.checked, false);
      assert.equal(checkbox.disabled, true);
      assert.equal(app.getButton('Edit Ship toggle coverage').disabled, true);
      assert.equal(app.getButton('Delete Ship toggle coverage').disabled, true);
      assert.ok(app.getByText('Saving…'));
    });

    const toggleError = new Error('Todo API is unavailable.');
    toggleError.code = TODO_CLIENT_ERROR_CODES.UNAVAILABLE;
    toggleRequest.reject(toggleError);

    await app.waitFor(() => {
      const checkbox = app.getInputByLabelText('Ship toggle coverage');
      assert.equal(checkbox.checked, false);
      assert.equal(checkbox.disabled, false);
      assert.ok(app.getByText('Todo API is unavailable.'));
      assert.equal(app.queryByText('Saving…'), null);
    });
  } finally {
    await app.cleanup();
  }
});

test('todo app blocks duplicate delete requests, keeps the row rendered until success, and restores focus to the adjacent row action', async () => {
  const deleteRequest = createDeferred();
  const deleteCalls = [];
  const items = [
    createTodo('todo_alpha', 'Alpha'),
    createTodo('todo_beta', 'Beta'),
    createTodo('todo_gamma', 'Gamma'),
  ];

  const client = {
    async deleteTodo(input) {
      deleteCalls.push(input);
      return deleteRequest.promise;
    },
    async listTodos() {
      return { items };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('Alpha'));
      assert.ok(app.getByText('Beta'));
      assert.ok(app.getByText('Gamma'));
    });

    app.getButton('Delete Beta').focus();
    assert.equal(
      app.document.activeElement.getAttribute('aria-label'),
      'Delete Beta'
    );

    await app.clickButton('Delete Beta');
    await app.clickButton('Delete Beta');

    assert.equal(deleteCalls.length, 1);
    assert.deepEqual(deleteCalls[0], { id: 'todo_beta' });

    await app.waitFor(() => {
      assert.ok(app.getByText('Beta'));
      assert.ok(app.getByText('Deleting…'));
      assert.equal(app.getInputByLabelText('Beta').disabled, true);
      assert.equal(app.getButton('Edit Beta').disabled, true);
      assert.equal(app.getButton('Delete Beta').disabled, true);
    });

    deleteRequest.resolve({ deletedId: 'todo_beta' });

    await app.waitFor(() => {
      assert.equal(app.queryByText('Beta'), null);
      assert.equal(
        app.document.activeElement.getAttribute('aria-label'),
        'Edit Gamma'
      );
    });
  } finally {
    await app.cleanup();
  }
});

test('todo app refetches authoritative state after a stale delete response instead of guessing locally', async () => {
  const initialItems = [
    createTodo('todo_stale', 'Stale todo'),
    createTodo('todo_kept', 'Kept todo'),
  ];
  const refreshedItems = [createTodo('todo_kept', 'Kept todo')];
  let listCalls = 0;

  const client = {
    async deleteTodo() {
      const error = new Error('Todo was not found.');
      error.code = TODO_CLIENT_ERROR_CODES.TODO_NOT_FOUND;
      throw error;
    },
    async listTodos() {
      listCalls += 1;
      return {
        items: listCalls === 1 ? initialItems : refreshedItems,
      };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('Stale todo'));
      assert.ok(app.getByText('Kept todo'));
    });

    await app.clickButton('Delete Stale todo');

    await app.waitFor(() => {
      assert.equal(listCalls, 2);
      assert.equal(app.queryByText('Stale todo'), null);
      assert.ok(app.getByText('Kept todo'));
      assert.ok(
        app.getByText(
          'That todo was refreshed because it no longer matched persisted state.'
        )
      );
      assert.equal(app.queryByText('Todo was not found.'), null);
    });
  } finally {
    await app.cleanup();
  }
});

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });

  return {
    promise,
    reject,
    resolve,
  };
}

function createTodo(id, title) {
  return {
    completed: false,
    createdAt: '2026-03-08T11:00:00Z',
    id,
    title,
    updatedAt: '2026-03-08T11:00:00Z',
  };
}
