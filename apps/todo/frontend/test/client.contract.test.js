const test = require('node:test');
const assert = require('node:assert/strict');

const { createClientContext } = require('./helpers');

test('todo API client normalizes CRUD responses for the MVP flows', async () => {
  const timestamps = [
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:31:00Z',
    '2026-03-07T14:32:00Z',
    '2026-03-07T14:33:00Z',
  ];
  const ids = ['todo_b', 'todo_a'];
  const context = await createClientContext({
    serverOptions: {
      idGenerator: () => ids.shift(),
      now: () => timestamps.shift(),
    },
  });

  try {
    assert.equal(context.client.baseUrl, context.server.url);

    const emptyList = await context.client.listTodos();
    assert.deepEqual(emptyList, { items: [] });

    const firstCreate = await context.client.createTodo({ title: '  Buy milk  ' });
    assert.deepEqual(firstCreate, {
      item: {
        id: 'todo_b',
        title: 'Buy milk',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:30:00Z',
      },
    });
    assert.equal(Object.prototype.hasOwnProperty.call(firstCreate, 'todo'), false);

    const secondCreate = await context.client.createTodo({ title: 'Read book' });
    assert.equal(secondCreate.item.id, 'todo_a');

    const orderedList = await context.client.listTodos();
    assert.deepEqual(
      orderedList.items.map((todo) => todo.id),
      ['todo_a', 'todo_b']
    );

    const renamed = await context.client.editTodoTitle({
      id: 'todo_b',
      title: 'Buy oat milk',
    });
    assert.deepEqual(renamed, {
      item: {
        id: 'todo_b',
        title: 'Buy oat milk',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:31:00Z',
      },
    });

    const completed = await context.client.completeTodo('todo_b');
    assert.equal(completed.item.completed, true);
    assert.equal(completed.item.updatedAt, '2026-03-07T14:32:00Z');

    const uncompleted = await context.client.uncompleteTodo('todo_b');
    assert.equal(uncompleted.item.completed, false);
    assert.equal(uncompleted.item.updatedAt, '2026-03-07T14:33:00Z');

    const noOp = await context.client.updateTodo({
      id: 'todo_b',
      title: 'Buy oat milk',
      completed: false,
    });
    assert.deepEqual(noOp, {
      item: {
        id: 'todo_b',
        title: 'Buy oat milk',
        completed: false,
        createdAt: '2026-03-07T14:30:00Z',
        updatedAt: '2026-03-07T14:33:00Z',
      },
    });

    const deleted = await context.client.deleteTodo({ id: 'todo_b' });
    assert.deepEqual(deleted, { deletedId: 'todo_b' });

    const finalList = await context.client.listTodos();
    assert.deepEqual(finalList, {
      items: [secondCreate.item],
    });
  } finally {
    await context.cleanup();
  }
});
