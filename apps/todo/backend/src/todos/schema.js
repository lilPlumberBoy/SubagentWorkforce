const TODO_SCHEMA_SQL = `
  CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(title) BETWEEN 1 AND 200)
  );
`;

function initializeTodoSchema(database) {
  database.exec(TODO_SCHEMA_SQL);
}

module.exports = {
  TODO_SCHEMA_SQL,
  initializeTodoSchema,
};
