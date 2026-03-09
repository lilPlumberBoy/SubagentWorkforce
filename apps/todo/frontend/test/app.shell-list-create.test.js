const test = require('node:test');
const assert = require('node:assert/strict');

const { TODO_CLIENT_ERROR_CODES } = require('../src');
const { renderTodoApp } = require('./dom-helpers');

test('todo app shows loading, list error, and retry recovery for persisted todos', async () => {
  const firstListRequest = createDeferred();
  const persistedTodo = {
    completed: false,
    createdAt: '2026-03-08T09:00:00Z',
    id: 'todo_retry',
    title: 'Persisted todo',
    updatedAt: '2026-03-08T09:00:00Z',
  };
  let listCalls = 0;

  const client = {
    async createTodo() {
      throw new Error('createTodo should not be called in this test');
    },
    async listTodos() {
      listCalls += 1;

      if (listCalls === 1) {
        return firstListRequest.promise;
      }

      return { items: [persistedTodo] };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    assert.ok(app.getByText('Loading todos…'));

    const loadError = new Error('Todo API is unavailable.');
    loadError.code = TODO_CLIENT_ERROR_CODES.UNAVAILABLE;
    firstListRequest.reject(loadError);

    await app.waitFor(() => {
      assert.ok(app.getByText('Todo API is unavailable.'));
      assert.equal(listCalls, 1);
    });

    await app.clickButton('Retry');

    await app.waitFor(() => {
      assert.ok(app.getByText('Persisted todo'));
      assert.equal(listCalls, 2);
    });
  } finally {
    await app.cleanup();
  }
});

test('todo app blocks duplicate create submits and preserves the draft on retryable create failure', async () => {
  const createRequest = createDeferred();
  const createCalls = [];

  const client = {
    async createTodo(input) {
      createCalls.push(input);
      return createRequest.promise;
    },
    async listTodos() {
      return { items: [] };
    },
  };

  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.getByText('No todos yet.'));
    });

    await app.setInputValue('New todo', 'Write docs');
    await app.submitForm('Create todo');
    await app.submitForm('Create todo');

    assert.equal(createCalls.length, 1);
    assert.deepEqual(createCalls[0], { title: 'Write docs' });
    assert.equal(app.getInputByLabelText('New todo').disabled, true);

    const createError = new Error('Todo API is unavailable.');
    createError.code = TODO_CLIENT_ERROR_CODES.UNAVAILABLE;
    createRequest.reject(createError);

    await app.waitFor(() => {
      assert.ok(app.getByText('Todo API is unavailable.'));
      assert.equal(app.getInputByLabelText('New todo').value, 'Write docs');
      assert.equal(app.getInputByLabelText('New todo').disabled, false);
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
