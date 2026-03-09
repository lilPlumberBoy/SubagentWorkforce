const test = require('node:test');
const assert = require('node:assert/strict');

const { TODO_CLIENT_ERROR_CODES } = require('../src');
const { renderTodoApp } = require('./dom-helpers');
const { createClientContext } = require('./helpers');

test('todo app blocks duplicate edit submits, locks the inline editor while saving, and restores focus after success', async () => {
  const editRequest = createDeferred();
  const editCalls = [];
  const items = [createTodo('todo_edit', 'Rename me')];

  const client = {
    async editTodoTitle(input) {
      editCalls.push(input);
      return editRequest.promise;
    },
    async listTodos() {
      return { items };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('Rename me'));
    });

    await app.clickButton('Edit Rename me');
    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').value, 'Rename me');
    });

    await app.setInputValue('Edit todo', 'Renamed todo');
    await app.submitForm('Edit Rename me');
    await app.submitForm('Edit Rename me');

    assert.equal(editCalls.length, 1);
    assert.deepEqual(editCalls[0], {
      id: 'todo_edit',
      title: 'Renamed todo',
    });

    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').disabled, true);
      assert.equal(app.getButton('Saving…').disabled, true);
      assert.equal(app.getButton('Cancel').disabled, true);
    });

    editRequest.resolve({
      item: createTodo('todo_edit', 'Renamed todo', {
        updatedAt: '2026-03-08T13:01:00Z',
      }),
    });

    await app.waitFor(() => {
      assert.ok(app.getByText('Renamed todo'));
      assert.equal(app.queryByText('Save'), null);
      assert.equal(
        app.document.activeElement.getAttribute('aria-label'),
        'Edit Renamed todo'
      );
    });
  } finally {
    await app.cleanup();
  }
});

test('todo app cancels inline edits without mutating persisted data and restores focus to the row action', async () => {
  const editCalls = [];
  const items = [createTodo('todo_keep', 'Keep original')];

  const client = {
    async editTodoTitle(input) {
      editCalls.push(input);
      return {
        item: createTodo(input.id, input.title, {
          updatedAt: '2026-03-08T13:05:00Z',
        }),
      };
    },
    async listTodos() {
      return { items };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('Keep original'));
    });

    await app.clickButton('Edit Keep original');
    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').value, 'Keep original');
    });

    await app.setInputValue('Edit todo', 'Discard this draft');
    await app.clickButton('Cancel');

    await app.waitFor(() => {
      assert.ok(app.getByText('Keep original'));
      assert.equal(app.queryByText('Save'), null);
      assert.equal(editCalls.length, 0);
      assert.equal(
        app.document.activeElement.getAttribute('aria-label'),
        'Edit Keep original'
      );
    });
  } finally {
    await app.cleanup();
  }
});

test('todo app keeps invalid edit drafts inline and allows recovery with a later successful save', async () => {
  const context = await createClientContext({
    serverOptions: {
      idGenerator: () => 'todo_validation',
      now: (() => {
        const timestamps = [
          '2026-03-08T13:10:00Z',
          '2026-03-08T13:11:00Z',
        ];
        return () => timestamps.shift();
      })(),
    },
  });
  const created = await context.client.createTodo({ title: 'Needs cleanup' });
  const app = await renderTodoApp({ client: context.client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText(created.item.title));
    });

    await app.clickButton('Edit Needs cleanup');
    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').value, 'Needs cleanup');
    });

    await app.setInputValue('Edit todo', '   ');
    await app.clickButton('Save');

    await app.waitFor(() => {
      assert.ok(app.getByText('Title is required.'));
      assert.equal(app.getInputByLabelText('Edit todo').value, '   ');
    });

    const persistedAfterInvalidSave = await context.client.listTodos();
    assert.deepEqual(persistedAfterInvalidSave.items.map((todo) => todo.title), [
      'Needs cleanup',
    ]);

    await app.setInputValue('Edit todo', 'Recovered title');
    await app.clickButton('Save');

    await app.waitFor(() => {
      assert.ok(app.getByText('Recovered title'));
      assert.equal(app.queryByText('Title is required.'), null);
    });

    const persistedAfterRecovery = await context.client.listTodos();
    assert.deepEqual(persistedAfterRecovery.items.map((todo) => todo.title), [
      'Recovered title',
    ]);
  } finally {
    await app.cleanup();
    await context.cleanup();
  }
});

test('todo app refetches authoritative state after a stale edit response instead of guessing locally', async () => {
  const initialItems = [createTodo('todo_stale', 'Original title')];
  const refreshedItems = [createTodo('todo_stale', 'Persisted elsewhere')];
  let listCalls = 0;

  const client = {
    async editTodoTitle() {
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
      assert.ok(app.getByText('Original title'));
    });

    await app.clickButton('Edit Original title');
    await app.setInputValue('Edit todo', 'Local draft');
    await app.clickButton('Save');

    await app.waitFor(() => {
      assert.equal(listCalls, 2);
      assert.ok(app.getByText('Persisted elsewhere'));
      assert.equal(app.queryByText('Original title'), null);
      assert.equal(app.queryByText('Todo was not found.'), null);
      assert.ok(
        app.getByText(
          'That todo was refreshed because it no longer matched persisted state.'
        )
      );
      assert.equal(app.queryByText('Save'), null);
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

function createTodo(id, title, overrides = {}) {
  return {
    completed: false,
    createdAt: '2026-03-08T13:00:00Z',
    id,
    title,
    updatedAt: '2026-03-08T13:00:00Z',
    ...overrides,
  };
}
