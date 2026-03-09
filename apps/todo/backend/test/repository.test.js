const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { createTodoRepository } = require('../src/todos/repository');
const { TodoNotFoundError, TodoValidationError } = require('../src/todos/errors');

function createTestContext() {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-repo-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');

  return {
    cleanup() {
      fs.rmSync(tempDirectory, { recursive: true, force: true });
    },
    databasePath,
  };
}

test('repository CRUD preserves normalization, ordering, no-op updates, and hard deletes', () => {
  const context = createTestContext();
  let nowCallCount = 0;
  const timestamps = [
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:30:00Z',
    '2026-03-07T14:31:00Z',
    '2026-03-07T14:32:00Z',
    '2026-03-07T14:33:00Z',
  ];

  const repository = createTodoRepository({
    databasePath: context.databasePath,
    idGenerator: (() => {
      const ids = ['todo_b', 'todo_a'];
      return () => ids.shift();
    })(),
    now: () => timestamps[nowCallCount++],
  });

  try {
    const firstTodo = repository.createTodo({ title: '  Buy milk  ' });
    const secondTodo = repository.createTodo({ title: 'Read book' });

    assert.deepEqual(firstTodo, {
      id: 'todo_b',
      title: 'Buy milk',
      completed: false,
      createdAt: '2026-03-07T14:30:00Z',
      updatedAt: '2026-03-07T14:30:00Z',
    });
    assert.equal(secondTodo.id, 'todo_a');

    assert.deepEqual(repository.listTodos().map((todo) => todo.id), ['todo_a', 'todo_b']);

    const renamedTodo = repository.updateTodo(firstTodo.id, { title: '  Buy oat milk ' });
    assert.deepEqual(renamedTodo, {
      id: 'todo_b',
      title: 'Buy oat milk',
      completed: false,
      createdAt: '2026-03-07T14:30:00Z',
      updatedAt: '2026-03-07T14:31:00Z',
    });

    const noOpTodo = repository.updateTodo(firstTodo.id, { title: 'Buy oat milk' });
    assert.deepEqual(noOpTodo, renamedTodo);

    const completedTodo = repository.updateTodo(firstTodo.id, { completed: true });
    assert.equal(completedTodo.completed, true);
    assert.equal(completedTodo.createdAt, '2026-03-07T14:30:00Z');
    assert.equal(completedTodo.updatedAt, '2026-03-07T14:32:00Z');

    const reopenedRepository = createTodoRepository({
      databasePath: context.databasePath,
      now: () => timestamps[nowCallCount++],
    });

    try {
      assert.equal(reopenedRepository.listTodos().length, 2);

      const uncompletedTodo = reopenedRepository.updateTodo(firstTodo.id, {
        completed: false,
      });
      assert.equal(uncompletedTodo.completed, false);
      assert.equal(uncompletedTodo.updatedAt, '2026-03-07T14:33:00Z');

      const deletion = reopenedRepository.deleteTodo(firstTodo.id);
      assert.deepEqual(deletion, { deletedId: firstTodo.id });
      assert.deepEqual(reopenedRepository.listTodos().map((todo) => todo.id), ['todo_a']);

      assert.throws(
        () => reopenedRepository.deleteTodo(firstTodo.id),
        (error) => error instanceof TodoNotFoundError && error.code === 'not_found'
      );
    } finally {
      reopenedRepository.close();
    }
  } finally {
    repository.close();
    context.cleanup();
  }
});

test('repository rejects invalid create and update payloads', () => {
  const context = createTestContext();
  const repository = createTodoRepository({
    databasePath: context.databasePath,
    idGenerator: () => 'todo_fixed',
    now: () => '2026-03-07T14:30:00Z',
  });

  try {
    assert.throws(
      () => repository.createTodo({ title: '   ' }),
      (error) =>
        error instanceof TodoValidationError &&
        error.code === 'validation_error' &&
        error.fieldErrors.title[0] === 'Title must be between 1 and 200 characters.'
    );

    assert.throws(
      () => repository.createTodo({ title: 'Write docs', completed: false }),
      (error) =>
        error instanceof TodoValidationError &&
        error.fieldErrors.completed[0] === 'Field is not supported.'
    );

    const todo = repository.createTodo({ title: 'Write docs' });

    assert.throws(
      () => repository.updateTodo(todo.id, {}),
      (error) =>
        error instanceof TodoValidationError &&
        error.code === 'validation_error'
    );

    assert.throws(
      () => repository.updateTodo(todo.id, { completed: 'yes' }),
      (error) =>
        error instanceof TodoValidationError &&
        error.fieldErrors.completed[0] === 'Completed must be a boolean.'
    );

    assert.throws(
      () => repository.updateTodo(todo.id, { id: 'different' }),
      (error) =>
        error instanceof TodoValidationError &&
        error.fieldErrors.id[0] === 'Field is not supported.'
    );
  } finally {
    repository.close();
    context.cleanup();
  }
});
