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
  const backendPublicHost = normalizePublicHost(backendHost);
  const frontendPublicHost = normalizePublicHost(frontendHost);
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
  const frontendOrigin = `http://${frontendPublicHost}:${frontendPort}`;

  const backendServer = await startTodoHttpServer({
    allowedOrigin: frontendOrigin,
    databasePath: options.databasePath,
    host: backendHost,
    port: backendPort,
  });
  const backend = {
    ...backendServer,
    url: createPublicBaseUrl(backendServer.server, backendPublicHost),
  };

  let frontend;

  try {
    frontend = await startTodoFrontendServer({
      apiBaseUrl: backend.url,
      host: frontendHost,
      port: frontendPort,
      publicHost: frontendPublicHost,
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

function normalizePublicHost(host) {
  if (typeof host !== 'string' || host.trim().length === 0) {
    return DEFAULT_FRONTEND_HOST;
  }

  const normalizedHost = host.trim();

  if (
    normalizedHost === '0.0.0.0' ||
    normalizedHost === '::' ||
    normalizedHost === '::0' ||
    normalizedHost === '::ffff:0.0.0.0'
  ) {
    return '127.0.0.1';
  }

  return normalizedHost;
}

function createPublicBaseUrl(server, host) {
  const address = server.address();

  if (!address || typeof address === 'string') {
    throw new Error('Runtime server address is unavailable.');
  }

  return `http://${host}:${address.port}`;
}

module.exports = {
  startTodoRuntime,
};
