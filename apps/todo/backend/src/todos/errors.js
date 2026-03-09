class TodoValidationError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = 'TodoValidationError';
    this.code = 'validation_error';
    this.fieldErrors = options.fieldErrors;
  }
}

class TodoNotFoundError extends Error {
  constructor(id) {
    super(`Todo with id "${id}" was not found.`);
    this.name = 'TodoNotFoundError';
    this.code = 'not_found';
    this.todoId = id;
  }
}

module.exports = {
  TodoNotFoundError,
  TodoValidationError,
};
