const { randomUUID } = require('node:crypto');
const { openDatabase } = require('../db/database');
const { TodoNotFoundError, TodoValidationError } = require('./errors');
const { initializeTodoSchema } = require('./schema');

const CREATE_ALLOWED_FIELDS = new Set(['title']);
const UPDATE_ALLOWED_FIELDS = new Set(['title', 'completed']);
const TITLE_MIN_LENGTH = 1;
const TITLE_MAX_LENGTH = 200;

function createTodoRepository(options = {}) {
  const { database, databasePath } = openDatabase(options.databasePath);
  initializeTodoSchema(database);

  const repository = new TodoRepository({
    database,
    databasePath,
    idGenerator: options.idGenerator || defaultIdGenerator,
    now: options.now || defaultTimestamp,
  });

  return repository;
}

class TodoRepository {
  constructor({ database, databasePath, idGenerator, now }) {
    this.database = database;
    this.databasePath = databasePath;
    this.idGenerator = idGenerator;
    this.now = now;
    this.statements = {
      list: database.prepare(`
        SELECT id, title, completed, created_at, updated_at
        FROM todos
        ORDER BY created_at ASC, id ASC
      `),
      getById: database.prepare(`
        SELECT id, title, completed, created_at, updated_at
        FROM todos
        WHERE id = ?
      `),
      insert: database.prepare(`
        INSERT INTO todos (id, title, completed, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
      `),
      update: database.prepare(`
        UPDATE todos
        SET title = ?, completed = ?, updated_at = ?
        WHERE id = ?
      `),
      delete: database.prepare(`
        DELETE FROM todos
        WHERE id = ?
      `),
    };
  }

  listTodos() {
    return this.statements.list.all().map(mapRowToTodo);
  }

  createTodo(input) {
    const { title } = validateCreateInput(input);
    const timestamp = this.now();
    const todo = {
      id: this.idGenerator(),
      title,
      completed: false,
      createdAt: timestamp,
      updatedAt: timestamp,
    };

    withTransaction(this.database, () => {
      this.statements.insert.run(
        todo.id,
        todo.title,
        0,
        todo.createdAt,
        todo.updatedAt
      );
    });

    return todo;
  }

  updateTodo(id, updates) {
    assertTodoId(id);

    const existing = this.#getTodoById(id);
    const normalizedUpdates = validateUpdateInput(updates);

    const nextTodo = {
      ...existing,
      ...(Object.prototype.hasOwnProperty.call(normalizedUpdates, 'title')
        ? { title: normalizedUpdates.title }
        : {}),
      ...(Object.prototype.hasOwnProperty.call(normalizedUpdates, 'completed')
        ? { completed: normalizedUpdates.completed }
        : {}),
    };

    if (
      nextTodo.title === existing.title &&
      nextTodo.completed === existing.completed
    ) {
      return existing;
    }

    nextTodo.updatedAt = this.now();

    withTransaction(this.database, () => {
      const result = this.statements.update.run(
        nextTodo.title,
        nextTodo.completed ? 1 : 0,
        nextTodo.updatedAt,
        id
      );

      if (result.changes === 0) {
        throw new TodoNotFoundError(id);
      }
    });

    return this.#getTodoById(id);
  }

  deleteTodo(id) {
    assertTodoId(id);

    const result = withTransaction(this.database, () => this.statements.delete.run(id));

    if (result.changes === 0) {
      throw new TodoNotFoundError(id);
    }

    return { deletedId: id };
  }

  close() {
    this.database.close();
  }

  #getTodoById(id) {
    const row = this.statements.getById.get(id);

    if (!row) {
      throw new TodoNotFoundError(id);
    }

    return mapRowToTodo(row);
  }
}

function defaultIdGenerator() {
  return `todo_${randomUUID()}`;
}

function defaultTimestamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function validateCreateInput(input) {
  validatePlainObject(input, 'Create payload must be an object.');
  rejectUnsupportedFields(input, CREATE_ALLOWED_FIELDS);

  if (!Object.prototype.hasOwnProperty.call(input, 'title')) {
    throw new TodoValidationError('Title is required.', {
      fieldErrors: { title: ['Title is required.'] },
    });
  }

  return {
    title: normalizeTitle(input.title),
  };
}

function validateUpdateInput(input) {
  validatePlainObject(input, 'Update payload must be an object.');
  rejectUnsupportedFields(input, UPDATE_ALLOWED_FIELDS);

  if (Object.keys(input).length === 0) {
    throw new TodoValidationError(
      'Update payload must include at least one supported field.'
    );
  }

  const normalized = {};

  if (Object.prototype.hasOwnProperty.call(input, 'title')) {
    normalized.title = normalizeTitle(input.title);
  }

  if (Object.prototype.hasOwnProperty.call(input, 'completed')) {
    if (typeof input.completed !== 'boolean') {
      throw new TodoValidationError('Completed must be a boolean.', {
        fieldErrors: { completed: ['Completed must be a boolean.'] },
      });
    }

    normalized.completed = input.completed;
  }

  if (Object.keys(normalized).length === 0) {
    throw new TodoValidationError(
      'Update payload must include at least one supported field.'
    );
  }

  return normalized;
}

function validatePlainObject(value, message) {
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new TodoValidationError(message);
  }
}

function rejectUnsupportedFields(input, allowedFields) {
  const unsupportedFields = Object.keys(input).filter(
    (field) => !allowedFields.has(field)
  );

  if (unsupportedFields.length > 0) {
    throw new TodoValidationError(
      `Unsupported fields: ${unsupportedFields.join(', ')}.`,
      {
        fieldErrors: unsupportedFields.reduce((fieldErrors, field) => {
          fieldErrors[field] = ['Field is not supported.'];
          return fieldErrors;
        }, {}),
      }
    );
  }
}

function normalizeTitle(title) {
  if (typeof title !== 'string') {
    throw new TodoValidationError('Title must be a string.', {
      fieldErrors: { title: ['Title must be a string.'] },
    });
  }

  const normalizedTitle = title.trim();

  if (
    normalizedTitle.length < TITLE_MIN_LENGTH ||
    normalizedTitle.length > TITLE_MAX_LENGTH
  ) {
    throw new TodoValidationError(
      `Title must be between ${TITLE_MIN_LENGTH} and ${TITLE_MAX_LENGTH} characters.`,
      {
        fieldErrors: {
          title: [
            `Title must be between ${TITLE_MIN_LENGTH} and ${TITLE_MAX_LENGTH} characters.`,
          ],
        },
      }
    );
  }

  return normalizedTitle;
}

function assertTodoId(id) {
  if (typeof id !== 'string' || id.length === 0) {
    throw new TodoNotFoundError(id);
  }
}

function withTransaction(database, work) {
  database.exec('BEGIN');

  try {
    const result = work();
    database.exec('COMMIT');
    return result;
  } catch (error) {
    try {
      database.exec('ROLLBACK');
    } catch {
      // Ignore rollback failures because the original error is the actionable one.
    }

    throw error;
  }
}

function mapRowToTodo(row) {
  return {
    id: row.id,
    title: row.title,
    completed: Boolean(row.completed),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

module.exports = {
  TITLE_MAX_LENGTH,
  TITLE_MIN_LENGTH,
  TodoRepository,
  createTodoRepository,
};
