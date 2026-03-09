const test = require('node:test');
const assert = require('node:assert/strict');

const { TODO_COLLECTION_PATH } = require('../src/server');
const { createServerContext, requestJson } = require('./http-helpers');

test('crud-contract + no-op-update: HTTP contract supports list, create, edit, complete, uncomplete, no-op patch, delete, and stable ordering', async () => {
  const timestamps = [
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:31:00Z',
    '2026-03-07T14:32:00Z',
    '2026-03-07T14:33:00Z',
  ];
  const ids = ['todo_b', 'todo_a'];
  const context = await createServerContext({
    idGenerator: () => ids.shift(),
    now: () => timestamps.shift(),
  });

  try {
    const emptyList = await requestJson(context.server, TODO_COLLECTION_PATH);
    assert.equal(emptyList.status, 200);
    assert.deepEqual(emptyList.body, { items: [] });

    const firstCreate = await requestJson(context.server, TODO_COLLECTION_PATH, {
      method: 'POST',
      json: { title: '  Buy milk  ' },
    });
    assert.equal(firstCreate.status, 201);
    assert.deepEqual(firstCreate.body, {
      todo: {
        id: 'todo_b',
        title: 'Buy milk',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:30:00Z',
      },
    });

    const secondCreate = await requestJson(context.server, TODO_COLLECTION_PATH, {
      method: 'POST',
      json: { title: 'Read book' },
    });
    assert.equal(secondCreate.status, 201);
    assert.equal(secondCreate.body.todo.id, 'todo_a');

    const orderedList = await requestJson(context.server, TODO_COLLECTION_PATH);
    assert.deepEqual(
      orderedList.body.items.map((todo) => todo.id),
      ['todo_a', 'todo_b']
    );

    const renamedTodo = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_b`,
      {
        method: 'PATCH',
        json: { title: 'Buy oat milk' },
      }
    );
    assert.equal(renamedTodo.status, 200);
    assert.deepEqual(renamedTodo.body.todo, {
      id: 'todo_b',
      title: 'Buy oat milk',
      completed: false,
      createdAt: '2026-03-07T14:30:00Z',
      updatedAt: '2026-03-07T14:31:00Z',
    });

    const completedTodo = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_b`,
      {
        method: 'PATCH',
        json: { completed: true },
      }
    );
    assert.equal(completedTodo.status, 200);
    assert.equal(completedTodo.body.todo.completed, true);
    assert.equal(completedTodo.body.todo.createdAt, '2026-03-07T14:30:00Z');
    assert.equal(completedTodo.body.todo.updatedAt, '2026-03-07T14:32:00Z');

    const uncompletedTodo = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_b`,
      {
        method: 'PATCH',
        json: { completed: false },
      }
    );
    assert.equal(uncompletedTodo.status, 200);
    assert.equal(uncompletedTodo.body.todo.completed, false);
    assert.equal(uncompletedTodo.body.todo.updatedAt, '2026-03-07T14:33:00Z');

    const noOpTodo = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_b`,
      {
        method: 'PATCH',
        json: { title: 'Buy oat milk', completed: false },
      }
    );
    assert.equal(noOpTodo.status, 200);
    assert.deepEqual(noOpTodo.body, {
      todo: {
        id: 'todo_b',
        title: 'Buy oat milk',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:33:00Z',
      },
    });

    const deletion = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_b`,
      {
        method: 'DELETE',
      }
    );
    assert.equal(deletion.status, 200);
    assert.deepEqual(deletion.body, { deletedId: 'todo_b' });

    const finalList = await requestJson(context.server, TODO_COLLECTION_PATH);
    assert.equal(finalList.status, 200);
    assert.deepEqual(finalList.body, {
      items: [secondCreate.body.todo],
    });
  } finally {
    await context.cleanup();
  }
});

test('validation-errors + scope-boundary: HTTP contract returns structured validation, not-found, and unsupported-route errors', async () => {
  const context = await createServerContext({
    idGenerator: () => 'todo_fixed',
    now: () => '2026-03-07T14:30:00Z',
  });

  try {
    const invalidJson = await requestJson(context.server, TODO_COLLECTION_PATH, {
      method: 'POST',
      body: '{"title":',
      headers: {
        'content-type': 'application/json',
      },
    });
    assert.equal(invalidJson.status, 400);
    assert.deepEqual(invalidJson.body, {
      error: {
        code: 'validation_error',
        message: 'Request body must be valid JSON.',
      },
    });

    const invalidContentType = await requestJson(
      context.server,
      TODO_COLLECTION_PATH,
      {
        method: 'POST',
        body: JSON.stringify({ title: 'Write docs' }),
        headers: {
          'content-type': 'text/plain',
        },
      }
    );
    assert.equal(invalidContentType.status, 400);
    assert.deepEqual(invalidContentType.body, {
      error: {
        code: 'validation_error',
        message: 'Content-Type must be application/json.',
      },
    });

    const unsupportedCreateField = await requestJson(
      context.server,
      TODO_COLLECTION_PATH,
      {
        method: 'POST',
        json: { title: 'Write docs', completed: false },
      }
    );
    assert.equal(unsupportedCreateField.status, 400);
    assert.deepEqual(unsupportedCreateField.body, {
      error: {
        code: 'validation_error',
        message: 'Unsupported fields: completed.',
        fieldErrors: {
          completed: ['Field is not supported.'],
        },
      },
    });

    const backendOwnedCreateFields = await requestJson(
      context.server,
      TODO_COLLECTION_PATH,
      {
        method: 'POST',
        json: {
          title: 'Write docs',
          id: 'todo_client',
          createdAt: '2026-03-01T00:00:00Z',
          updatedAt: '2026-03-01T00:00:00Z',
        },
      }
    );
    assert.equal(backendOwnedCreateFields.status, 400);
    assert.deepEqual(backendOwnedCreateFields.body, {
      error: {
        code: 'validation_error',
        message: 'Unsupported fields: id, createdAt, updatedAt.',
        fieldErrors: {
          id: ['Field is not supported.'],
          createdAt: ['Field is not supported.'],
          updatedAt: ['Field is not supported.'],
        },
      },
    });

    const invalidCreateTitle = await requestJson(
      context.server,
      TODO_COLLECTION_PATH,
      {
        method: 'POST',
        json: { title: '   ' },
      }
    );
    assert.equal(invalidCreateTitle.status, 400);
    assert.deepEqual(invalidCreateTitle.body, {
      error: {
        code: 'validation_error',
        message: 'Title must be between 1 and 200 characters.',
        fieldErrors: {
          title: ['Title must be between 1 and 200 characters.'],
        },
      },
    });

    const createdTodo = await requestJson(context.server, TODO_COLLECTION_PATH, {
      method: 'POST',
      json: { title: 'Write docs' },
    });
    assert.equal(createdTodo.status, 201);

    const invalidPatchJson = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed`,
      {
        method: 'PATCH',
        body: '{"completed":',
        headers: {
          'content-type': 'application/json',
        },
      }
    );
    assert.equal(invalidPatchJson.status, 400);
    assert.deepEqual(invalidPatchJson.body, {
      error: {
        code: 'validation_error',
        message: 'Request body must be valid JSON.',
      },
    });

    const invalidPatchContentType = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed`,
      {
        method: 'PATCH',
        body: JSON.stringify({ completed: true }),
        headers: {
          'content-type': 'text/plain',
        },
      }
    );
    assert.equal(invalidPatchContentType.status, 400);
    assert.deepEqual(invalidPatchContentType.body, {
      error: {
        code: 'validation_error',
        message: 'Content-Type must be application/json.',
      },
    });

    const emptyPatch = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed`,
      {
        method: 'PATCH',
        json: {},
      }
    );
    assert.equal(emptyPatch.status, 400);
    assert.deepEqual(emptyPatch.body, {
      error: {
        code: 'validation_error',
        message: 'Update payload must include at least one supported field.',
      },
    });

    const invalidPatch = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed`,
      {
        method: 'PATCH',
        json: { title: '   ' },
      }
    );
    assert.equal(invalidPatch.status, 400);
    assert.deepEqual(invalidPatch.body, {
      error: {
        code: 'validation_error',
        message: 'Title must be between 1 and 200 characters.',
        fieldErrors: {
          title: ['Title must be between 1 and 200 characters.'],
        },
      },
    });

    const backendOwnedUpdateFields = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed`,
      {
        method: 'PATCH',
        json: {
          id: 'todo_other',
          createdAt: '2026-03-01T00:00:00Z',
          updatedAt: '2026-03-01T00:00:00Z',
        },
      }
    );
    assert.equal(backendOwnedUpdateFields.status, 400);
    assert.deepEqual(backendOwnedUpdateFields.body, {
      error: {
        code: 'validation_error',
        message: 'Unsupported fields: id, createdAt, updatedAt.',
        fieldErrors: {
          id: ['Field is not supported.'],
          createdAt: ['Field is not supported.'],
          updatedAt: ['Field is not supported.'],
        },
      },
    });

    const missingPatch = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_missing`,
      {
        method: 'PATCH',
        json: { completed: true },
      }
    );
    assert.equal(missingPatch.status, 404);
    assert.deepEqual(missingPatch.body, {
      error: {
        code: 'not_found',
        message: 'Todo with id "todo_missing" was not found.',
      },
    });

    const missingDelete = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_missing`,
      {
        method: 'DELETE',
      }
    );
    assert.equal(missingDelete.status, 404);
    assert.deepEqual(missingDelete.body, {
      error: {
        code: 'not_found',
        message: 'Todo with id "todo_missing" was not found.',
      },
    });

    const legacyRoute = await requestJson(context.server, '/todos');
    assert.equal(legacyRoute.status, 404);
    assert.deepEqual(legacyRoute.body, {
      error: {
        code: 'not_found',
        message: 'Route not found.',
      },
    });

    const unsupportedRoute = await requestJson(
      context.server,
      `${TODO_COLLECTION_PATH}/todo_fixed/complete`,
      {
        method: 'POST',
        json: {},
      }
    );
    assert.equal(unsupportedRoute.status, 404);
    assert.deepEqual(unsupportedRoute.body, {
      error: {
        code: 'not_found',
        message: 'Route not found.',
      },
    });
  } finally {
    await context.cleanup();
  }
});
