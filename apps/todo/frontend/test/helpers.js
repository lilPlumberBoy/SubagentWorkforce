const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { startTodoHttpServer } = require('../../backend/src/server');
const { createTodoApiClient } = require('../src');

async function createClientContext(options = {}) {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-frontend-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');
  const server = await startTodoHttpServer({
    databasePath,
    host: '127.0.0.1',
    port: 0,
    ...options.serverOptions,
  });
  const client = createTodoApiClient({
    baseUrl: server.url,
    ...options.clientOptions,
  });

  return {
    client,
    databasePath,
    server,
    async cleanup() {
      await server.close();
      fs.rmSync(tempDirectory, { force: true, recursive: true });
    },
  };
}

module.exports = {
  createClientContext,
};
