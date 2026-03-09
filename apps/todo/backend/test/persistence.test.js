const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { startTodoHttpServer, TODO_COLLECTION_PATH } = require('../src/server');
const { requestJson } = require('./http-helpers');

function createPersistenceContext() {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-persistence-'));

  return {
    databasePath: path.join(tempDirectory, 'todos.sqlite'),
    cleanup() {
      fs.rmSync(tempDirectory, { recursive: true, force: true });
    },
  };
}

async function startPersistenceServer(databasePath, options = {}) {
  return startTodoHttpServer({
    databasePath,
    host: '127.0.0.1',
    port: 0,
    ...options,
  });
}

test('durability: persistence validation covers CRUD durability across server restarts', async () => {
  const context = createPersistenceContext();
  const timestamps = [
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:31:00Z',
    '2026-03-07T14:32:00Z',
    '2026-03-07T14:33:00Z',
  ];
  const nextTimestamp = () => {
    const value = timestamps.shift();

    if (!value) {
      throw new Error('Test timestamp queue is exhausted.');
    }

    return value;
  };

  let server = await startPersistenceServer(context.databasePath, {
    idGenerator: () => 'todo_persisted',
    now: nextTimestamp,
  });

  try {
    const createdTodo = await requestJson(server, TODO_COLLECTION_PATH, {
      method: 'POST',
      json: { title: '  Persist me  ' },
    });
    assert.equal(createdTodo.status, 201);
    assert.deepEqual(createdTodo.body, {
      todo: {
        id: 'todo_persisted',
        title: 'Persist me',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:30:00Z',
      },
    });
  } finally {
    await server.close();
  }

  server = await startPersistenceServer(context.databasePath, {
    now: nextTimestamp,
  });

  try {
    const persistedList = await requestJson(server, TODO_COLLECTION_PATH);
    assert.equal(persistedList.status, 200);
    assert.deepEqual(persistedList.body, {
      items: [
        {
          id: 'todo_persisted',
          title: 'Persist me',
          completed: false,
          createdAt: '2026-03-07T14:30:00Z',
          updatedAt: '2026-03-07T14:30:00Z',
        },
      ],
    });

    const updatedTodo = await requestJson(
      server,
      `${TODO_COLLECTION_PATH}/todo_persisted`,
      {
        method: 'PATCH',
        json: { title: 'Persist me better', completed: true },
      }
    );
    assert.equal(updatedTodo.status, 200);
    assert.deepEqual(updatedTodo.body, {
      todo: {
        id: 'todo_persisted',
        title: 'Persist me better',
        completed: true,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:31:00Z',
      },
    });
  } finally {
    await server.close();
  }

  server = await startPersistenceServer(context.databasePath, {
    now: nextTimestamp,
  });

  try {
    const reopenedTodo = await requestJson(server, TODO_COLLECTION_PATH);
    assert.deepEqual(reopenedTodo.body, {
      items: [
        {
          id: 'todo_persisted',
          title: 'Persist me better',
          completed: true,
          createdAt: '2026-03-07T14:30:00Z',
          updatedAt: '2026-03-07T14:31:00Z',
        },
      ],
    });

    const uncompletedTodo = await requestJson(
      server,
      `${TODO_COLLECTION_PATH}/todo_persisted`,
      {
        method: 'PATCH',
        json: { completed: false },
      }
    );
    assert.equal(uncompletedTodo.status, 200);
    assert.deepEqual(uncompletedTodo.body, {
      todo: {
        id: 'todo_persisted',
        title: 'Persist me better',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:32:00Z',
      },
    });

    const deletion = await requestJson(
      server,
      `${TODO_COLLECTION_PATH}/todo_persisted`,
      {
        method: 'DELETE',
      }
    );
    assert.equal(deletion.status, 200);
    assert.deepEqual(deletion.body, { deletedId: 'todo_persisted' });
  } finally {
    await server.close();
  }

  server = await startPersistenceServer(context.databasePath, {
    now: nextTimestamp,
  });

  try {
    const finalList = await requestJson(server, TODO_COLLECTION_PATH);
    assert.equal(finalList.status, 200);
    assert.deepEqual(finalList.body, { items: [] });
  } finally {
    await server.close();
    context.cleanup();
  }
});
