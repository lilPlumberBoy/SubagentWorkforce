const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { startTodoHttpServer } = require('../src/server');

async function createServerContext(options = {}) {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-server-'));
  const databasePath = path.join(tempDirectory, 'todos.sqlite');
  const server = await startTodoHttpServer({
    databasePath,
    host: '127.0.0.1',
    port: 0,
    ...options,
  });

  return {
    databasePath,
    async cleanup() {
      await server.close();
      fs.rmSync(tempDirectory, { recursive: true, force: true });
    },
    server,
  };
}

async function requestJson(server, pathname, options = {}) {
  const headers = { ...(options.headers || {}) };
  const requestOptions = {
    body: options.body,
    headers,
    method: options.method || 'GET',
  };

  if (Object.prototype.hasOwnProperty.call(options, 'json')) {
    requestOptions.body = JSON.stringify(options.json);

    if (!headers['content-type']) {
      headers['content-type'] = 'application/json';
    }
  }

  const response = await fetch(`${server.url}${pathname}`, requestOptions);
  const rawBody = await response.text();

  return {
    body: rawBody.length > 0 ? JSON.parse(rawBody) : null,
    response,
    status: response.status,
  };
}

module.exports = {
  createServerContext,
  requestJson,
};
