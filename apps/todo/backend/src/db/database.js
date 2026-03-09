const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const DEFAULT_DATABASE_PATH = path.resolve(
  __dirname,
  '..',
  '..',
  'data',
  'todos.sqlite'
);

function resolveDatabasePath(databasePath = process.env.TODO_BACKEND_DB_PATH) {
  return path.resolve(databasePath || DEFAULT_DATABASE_PATH);
}

function openDatabase(databasePath) {
  const resolvedDatabasePath = resolveDatabasePath(databasePath);
  fs.mkdirSync(path.dirname(resolvedDatabasePath), { recursive: true });

  return {
    database: new DatabaseSync(resolvedDatabasePath),
    databasePath: resolvedDatabasePath,
  };
}

module.exports = {
  DEFAULT_DATABASE_PATH,
  openDatabase,
  resolveDatabasePath,
};
