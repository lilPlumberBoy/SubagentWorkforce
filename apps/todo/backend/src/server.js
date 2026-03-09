const http = require('node:http');
const express = require('express');
const { initializeTodoDatastore } = require('./bootstrap');
const { TodoNotFoundError, TodoValidationError } = require('./todos/errors');

const CORS_ALLOW_HEADERS = 'Accept, Content-Type';
const CORS_ALLOW_METHODS = 'GET,POST,PATCH,DELETE,OPTIONS';
const JSON_CONTENT_TYPE = 'application/json; charset=utf-8';
const TODO_COLLECTION_PATH = '/api/todos';

function createTodoHttpServer(options = {}) {
  const allowedOrigin = normalizeAllowedOrigin(
    options.allowedOrigin ?? process.env.TODO_ALLOWED_ORIGIN
  );
  const datastore = options.repository ? null : initializeTodoDatastore(options);
  const repository = options.repository || datastore.repository;
  const app = express();

  let datastoreClosed = false;

  const closeDatastore = () => {
    if (datastoreClosed) {
      return;
    }

    datastoreClosed = true;

    if (datastore) {
      datastore.close();
    }
  };

  app.disable('x-powered-by');
  app.use((request, response, next) => {
    const isAllowedCorsRequest = applyCorsHeaders(request, response, allowedOrigin);

    if (request.method === 'OPTIONS' && isAllowedCorsRequest) {
      response.writeHead(204);
      response.end();
      return;
    }

    next();
  });
  app.get(TODO_COLLECTION_PATH, (_request, response) => {
    writeJson(response, 200, { items: repository.listTodos() });
  });
  app.post(TODO_COLLECTION_PATH, asyncHandler(async (request, response) => {
    const payload = await readJsonBody(request);
    writeJson(response, 201, { todo: repository.createTodo(payload) });
  }));
  app.patch(
    `${TODO_COLLECTION_PATH}/:id`,
    asyncHandler(async (request, response) => {
      const payload = await readJsonBody(request);
      writeJson(response, 200, {
        todo: repository.updateTodo(request.params.id, payload),
      });
    })
  );
  app.delete(`${TODO_COLLECTION_PATH}/:id`, (request, response) => {
    writeJson(response, 200, repository.deleteTodo(request.params.id));
  });
  app.use((_request, response) => {
    writeJson(response, 404, {
      error: {
        code: 'not_found',
        message: 'Route not found.',
      },
    });
  });
  app.use((error, _request, response, _next) => {
    if (response.writableEnded) {
      response.destroy(error);
      return;
    }

    writeErrorResponse(response, error);
  });

  const server = http.createServer(app);

  server.once('close', closeDatastore);

  return {
    allowedOrigin,
    close: () =>
      new Promise((resolve, reject) => {
        if (!server.listening) {
          closeDatastore();
          resolve();
          return;
        }

        server.close((error) => {
          if (error) {
            reject(error);
            return;
          }

          resolve();
        });
      }),
    databasePath: datastore ? datastore.databasePath : undefined,
    repository,
    server,
  };
}

async function startTodoHttpServer(options = {}) {
  const host = options.host || '127.0.0.1';
  const port = options.port ?? 0;
  const serverContext = createTodoHttpServer(options);

  try {
    await new Promise((resolve, reject) => {
      serverContext.server.listen(port, host, (error) => {
        if (error) {
          reject(error);
          return;
        }

        resolve();
      });
    });
  } catch (error) {
    await serverContext.close();
    throw error;
  }

  return {
    ...serverContext,
    url: createBaseUrl(serverContext.server, host),
  };
}

async function readJsonBody(request) {
  const contentType = request.headers['content-type'];

  if (!isJsonContentType(contentType)) {
    throw new TodoValidationError('Content-Type must be application/json.');
  }

  let rawBody = '';

  for await (const chunk of request) {
    rawBody += chunk;
  }

  if (rawBody.length === 0) {
    throw new TodoValidationError('Request body must be valid JSON.');
  }

  try {
    return JSON.parse(rawBody);
  } catch {
    throw new TodoValidationError('Request body must be valid JSON.');
  }
}

function isJsonContentType(contentType) {
  return typeof contentType === 'string' && /^application\/json\b/i.test(contentType);
}

function normalizeAllowedOrigin(allowedOrigin) {
  if (typeof allowedOrigin !== 'string' || allowedOrigin.trim().length === 0) {
    return undefined;
  }

  let normalizedUrl;

  try {
    normalizedUrl = new URL(allowedOrigin);
  } catch {
    throw new Error('TODO_ALLOWED_ORIGIN must be a valid absolute URL.');
  }

  if (!/^https?:$/.test(normalizedUrl.protocol)) {
    throw new Error('TODO_ALLOWED_ORIGIN must use the http or https protocol.');
  }

  return normalizedUrl.origin;
}

function createBaseUrl(server, host) {
  const address = server.address();

  if (!address || typeof address === 'string') {
    throw new Error('Server address is unavailable.');
  }

  return `http://${host}:${address.port}`;
}

function applyCorsHeaders(request, response, allowedOrigin) {
  if (!allowedOrigin) {
    return false;
  }

  const requestOrigin = request.headers.origin;

  if (typeof requestOrigin !== 'string' || requestOrigin !== allowedOrigin) {
    return false;
  }

  appendVaryHeader(response, 'Origin');
  appendVaryHeader(response, 'Access-Control-Request-Method');
  appendVaryHeader(response, 'Access-Control-Request-Headers');
  response.setHeader('access-control-allow-origin', allowedOrigin);
  response.setHeader('access-control-allow-methods', CORS_ALLOW_METHODS);
  response.setHeader('access-control-allow-headers', CORS_ALLOW_HEADERS);
  response.setHeader('access-control-max-age', '600');

  return true;
}

function appendVaryHeader(response, value) {
  const currentValue = response.getHeader('vary');

  if (typeof currentValue !== 'string' || currentValue.length === 0) {
    response.setHeader('vary', value);
    return;
  }

  const values = currentValue
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);

  if (!values.includes(value)) {
    values.push(value);
    response.setHeader('vary', values.join(', '));
  }
}

function writeJson(response, statusCode, body) {
  response.writeHead(statusCode, {
    'content-type': JSON_CONTENT_TYPE,
  });
  response.end(JSON.stringify(body));
}

function writeErrorResponse(response, error) {
  const payload = buildErrorPayload(error);

  writeJson(response, payload.statusCode, {
    error: payload.fieldErrors
      ? {
          code: payload.code,
          message: payload.message,
          fieldErrors: payload.fieldErrors,
        }
      : {
          code: payload.code,
          message: payload.message,
        },
  });
}

function buildErrorPayload(error) {
  if (error instanceof TodoValidationError) {
    return {
      code: error.code,
      fieldErrors: error.fieldErrors,
      message: error.message,
      statusCode: 400,
    };
  }

  if (error instanceof TodoNotFoundError) {
    return {
      code: error.code,
      message: error.message,
      statusCode: 404,
    };
  }

  return {
    code: 'server_error',
    message: 'The server could not complete the request.',
    statusCode: 500,
  };
}

function asyncHandler(handler) {
  return (request, response, next) => {
    void Promise.resolve(handler(request, response)).catch(next);
  };
}

module.exports = {
  TODO_COLLECTION_PATH,
  createTodoHttpServer,
  startTodoHttpServer,
};
