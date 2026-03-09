const http = require('node:http');
const { startTodoHttpServer } = require('../../backend/src/server');
const { startTodoFrontendServer } = require('./frontend-server');

const DEFAULT_BACKEND_HOST = '127.0.0.1';
const DEFAULT_BACKEND_PORT = 3000;
const DEFAULT_FRONTEND_HOST = '127.0.0.1';
const DEFAULT_FRONTEND_PORT = 4173;

async function startTodoRuntime(options = {}) {
  const backendHost = options.backendHost || DEFAULT_BACKEND_HOST;
  const frontendHost = options.frontendHost || DEFAULT_FRONTEND_HOST;
  const backendPort = await resolveListenPort(
    backendHost,
    options.backendPort ?? DEFAULT_BACKEND_PORT,
    'TODO_BACKEND_PORT'
  );
  const frontendPort = await resolveListenPort(
    frontendHost,
    options.frontendPort ?? DEFAULT_FRONTEND_PORT,
    'TODO_FRONTEND_PORT'
  );
  const frontendOrigin = `http://${frontendHost}:${frontendPort}`;

  const backend = await startTodoHttpServer({
    allowedOrigin: frontendOrigin,
    databasePath: options.databasePath,
    host: backendHost,
    port: backendPort,
  });

  let frontend;

  try {
    frontend = await startTodoFrontendServer({
      apiBaseUrl: backend.url,
      host: frontendHost,
      port: frontendPort,
    });
  } catch (error) {
    await backend.close();
    throw error;
  }

  return {
    backend,
    close: async () => {
      const errors = [];

      await closeRuntimePart(frontend, errors);
      await closeRuntimePart(backend, errors);

      if (errors.length > 0) {
        throw errors[0];
      }
    },
    frontend,
  };
}

async function closeRuntimePart(runtimePart, errors) {
  if (!runtimePart) {
    return;
  }

  try {
    await runtimePart.close();
  } catch (error) {
    errors.push(error);
  }
}

async function resolveListenPort(host, port, envName) {
  if (!Number.isInteger(port) || port < 0) {
    throw new Error(`${envName} must be a non-negative integer.`);
  }

  if (port > 0) {
    return port;
  }

  return new Promise((resolve, reject) => {
    const probe = http.createServer();

    probe.once('error', reject);
    probe.listen(0, host, () => {
      const address = probe.address();

      if (!address || typeof address === 'string') {
        probe.close(() =>
          reject(new Error(`Could not resolve a port for ${envName}.`))
        );
        return;
      }

      probe.close((error) => {
        if (error) {
          reject(error);
          return;
        }

        resolve(address.port);
      });
    });
  });
}

module.exports = {
  startTodoRuntime,
};
