const test = require('node:test');
const assert = require('node:assert/strict');

const { createClientContext } = require('./helpers');
const { renderTodoApp } = require('./dom-helpers');

test('todo app wires persisted create, edit, toggle, and delete flows through the shared client', async () => {
  const context = await createClientContext({
    serverOptions: {
      idGenerator: (() => {
        const ids = ['todo_existing', 'todo_new'];
        return () => ids.shift();
      })(),
      now: (() => {
        const timestamps = [
          '2026-03-08T10:00:00Z',
          '2026-03-08T10:01:00Z',
          '2026-03-08T10:02:00Z',
          '2026-03-08T10:03:00Z',
          '2026-03-08T10:04:00Z',
        ];
        return () => timestamps.shift();
      })(),
    },
  });

  const client = {
    ...context.client,
    calls: [],
    async createTodo(input, options) {
      client.calls.push(['createTodo', input]);
      return context.client.createTodo(input, options);
    },
    async deleteTodo(input, options) {
      client.calls.push(['deleteTodo', input]);
      return context.client.deleteTodo(input, options);
    },
    async editTodoTitle(input, options) {
      client.calls.push(['editTodoTitle', input]);
      return context.client.editTodoTitle(input, options);
    },
    async listTodos(options) {
      client.calls.push(['listTodos']);
      return context.client.listTodos(options);
    },
    async setTodoCompleted(input, options) {
      client.calls.push(['setTodoCompleted', input]);
      return context.client.setTodoCompleted(input, options);
    },
  };

  const existing = await context.client.createTodo({ title: 'Existing todo' });
  const app = await renderTodoApp({ client });

  try {
    await app.waitFor(() => {
      assert.ok(app.queryByText('Loading todos…') === null);
      assert.ok(app.getByText(existing.item.title));
    });

    assert.equal(client.calls[0][0], 'listTodos');

    await app.setInputValue('New todo', '   ');
    await app.submitForm('Create todo');
    await app.waitFor(() => {
      assert.ok(app.getByText('Title is required.'));
    });

    await app.setInputValue('New todo', 'Write integration tests');
    await app.submitForm('Create todo');

    await app.waitFor(() => {
      assert.ok(app.getByText('Write integration tests'));
      assert.equal(app.getInputByLabelText('New todo').value, '');
    });

    await app.clickButton('Edit Existing todo');
    await app.waitFor(() => {
      assert.equal(app.getInputByLabelText('Edit todo').value, 'Existing todo');
    });

    await app.setInputValue('Edit todo', 'Existing todo updated');
    await app.clickButton('Save');

    await app.waitFor(() => {
      assert.ok(app.getByText('Existing todo updated'));
      assert.ok(app.queryByText('Save') === null);
    });

    const checkbox = app.getInputByLabelText('Write integration tests');
    assert.equal(checkbox.checked, false);

    await app.setCheckboxValue('Write integration tests', true);

    await app.waitFor(() => {
      const updatedCheckbox = app.getInputByLabelText('Write integration tests');
      assert.equal(updatedCheckbox.checked, true);
      assert.equal(updatedCheckbox.disabled, false);
    });

    await app.setCheckboxValue('Write integration tests', false);

    await app.waitFor(() => {
      const updatedCheckbox = app.getInputByLabelText('Write integration tests');
      assert.equal(updatedCheckbox.checked, false);
      assert.equal(updatedCheckbox.disabled, false);
    });

    await app.clickButton('Delete Write integration tests');

    await app.waitFor(() => {
      assert.equal(app.queryByText('Write integration tests'), null);
      assert.ok(app.getByText('Existing todo updated'));
    });

    const persistedTodos = await context.client.listTodos();
    assert.deepEqual(persistedTodos.items.map((todo) => todo.title), [
      'Existing todo updated',
    ]);
    assert.deepEqual(
      client.calls.map(([name]) => name),
      [
        'listTodos',
        'createTodo',
        'createTodo',
        'editTodoTitle',
        'setTodoCompleted',
        'setTodoCompleted',
        'deleteTodo',
      ]
    );
  } finally {
    await app.cleanup();
    await context.cleanup();
  }
});
