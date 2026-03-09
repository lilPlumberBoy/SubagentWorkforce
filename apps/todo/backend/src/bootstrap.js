const { createTodoRepository } = require('./todos/repository');

function initializeTodoDatastore(options = {}) {
  const repository = createTodoRepository(options);

  return {
    close: () => repository.close(),
    databasePath: repository.databasePath,
    repository,
  };
}

module.exports = {
  initializeTodoDatastore,
};
