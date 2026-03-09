const http = require('node:http');
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  TODO_CLIENT_ERROR_CODES,
  TodoApiClientError,
  createTodoApiClient,
} = require('../src');
const { createClientContext } = require('./helpers');

test('todo API client enforces configuration and input validation before transport work', async () => {
  assert.throws(
    () => createTodoApiClient({ baseUrl: '', fetch: async () => ({}) }),
    (error) =>
      error instanceof TodoApiClientError &&
      error.code === TODO_CLIENT_ERROR_CODES.CONFIGURATION_ERROR
  );

  let fetchCalled = false;
  const client = createTodoApiClient({
    baseUrl: 'http://127.0.0.1:43210',
    fetch: async () => {
      fetchCalled = true;
      throw new Error('fetch should not be reached');
    },
  });

  await assert.rejects(
    client.createTodo({ title: '   ' }),
    (error) =>
      error instanceof TodoApiClientError &&
      error.code === TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR &&
      error.fieldErrors.title[0] === 'Title is required.'
  );

  await assert.rejects(
    client.updateTodo({ id: 'todo_1' }),
    (error) =>
      error instanceof TodoApiClientError &&
      error.code === TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR &&
      error.message === 'Update payload must include at least one supported field.'
  );

  assert.equal(fetchCalled, false);
});

test('todo API client maps backend validation and not-found responses to normalized client errors', async () => {
  const context = await createClientContext();

  try {
    await assert.rejects(
      context.client.editTodoTitle({ id: 'todo_missing', title: 'Write docs' }),
      (error) =>
        error instanceof TodoApiClientError &&
        error.code === TODO_CLIENT_ERROR_CODES.TODO_NOT_FOUND &&
        error.status === 404 &&
        error.message === 'Todo with id "todo_missing" was not found.'
    );

    await assert.rejects(
      context.client.createTodo({ title: 'x'.repeat(201) }),
      (error) =>
        error instanceof TodoApiClientError &&
        error.code === TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR &&
        error.status === 400 &&
        error.fieldErrors.title[0] === 'Title must be between 1 and 200 characters.'
    );
  } finally {
    await context.cleanup();
  }
});

test('todo API client maps server failures, aborts, and malformed success payloads', async () => {
  const serverErrorContext = await createClientContext({
    serverOptions: {
      repository: {
        listTodos() {
          throw new Error('boom');
        },
      },
    },
  });

  try {
    await assert.rejects(
      serverErrorContext.client.listTodos(),
      (error) =>
        error instanceof TodoApiClientError &&
        error.code === TODO_CLIENT_ERROR_CODES.UNAVAILABLE &&
        error.status === 500 &&
        error.isRetryable === true
    );
  } finally {
    await serverErrorContext.cleanup();
  }

  const invalidServer = http.createServer((_request, response) => {
    response.writeHead(200, {
      'content-type': 'application/json',
    });
    response.end(JSON.stringify({ items: 'not-an-array' }));
  });

  await new Promise((resolve, reject) => {
    invalidServer.listen(0, '127.0.0.1', (error) => {
      if (error) {
        reject(error);
        return;
      }

      resolve();
    });
  });

  try {
    const address = invalidServer.address();
    const invalidClient = createTodoApiClient({
      baseUrl: `http://127.0.0.1:${address.port}`,
    });

    await assert.rejects(
      invalidClient.listTodos(),
      (error) =>
        error instanceof TodoApiClientError &&
        error.code === TODO_CLIENT_ERROR_CODES.INVALID_RESPONSE &&
        error.message === 'Todo API response is missing "items".'
    );
  } finally {
    await new Promise((resolve, reject) => {
      invalidServer.close((error) => {
        if (error) {
          reject(error);
          return;
        }

        resolve();
      });
    });
  }

  const abortedClient = createTodoApiClient({
    baseUrl: 'http://127.0.0.1:43210',
    fetch: async () => {
      throw new DOMException('Request aborted', 'AbortError');
    },
  });

  await assert.rejects(
    abortedClient.listTodos(),
    (error) =>
      error instanceof TodoApiClientError &&
      error.code === TODO_CLIENT_ERROR_CODES.ABORTED &&
      error.message === 'Todo request was aborted.'
  );
});
