const { startTodoRuntime } = require('../src/runtime');

async function main() {
  const runtime = await startTodoRuntime({
    backendHost: process.env.TODO_BACKEND_HOST || '127.0.0.1',
    backendPort: parsePort(process.env.TODO_BACKEND_PORT, 3000, 'TODO_BACKEND_PORT'),
    databasePath: process.env.TODO_BACKEND_DB_PATH,
    frontendHost: process.env.TODO_FRONTEND_HOST || '127.0.0.1',
    frontendPort: parsePort(process.env.TODO_FRONTEND_PORT, 4173, 'TODO_FRONTEND_PORT'),
  });

  const shutdown = async () => {
    process.removeListener('SIGINT', shutdown);
    process.removeListener('SIGTERM', shutdown);

    try {
      await runtime.close();
    } finally {
      process.exit(0);
    }
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  process.stdout.write(
    `${JSON.stringify({
      backend: {
        allowedOrigin: runtime.backend.allowedOrigin || null,
        databasePath: runtime.backend.databasePath,
        url: runtime.backend.url,
      },
      frontend: {
        apiBaseUrl: runtime.frontend.apiBaseUrl,
        url: runtime.frontend.url,
      },
      status: 'listening',
    })}\n`
  );
}

function parsePort(rawValue, fallback, envName) {
  if (rawValue === undefined) {
    return fallback;
  }

  const parsedValue = Number(rawValue);

  if (!Number.isInteger(parsedValue) || parsedValue < 0) {
    throw new Error(`${envName} must be a non-negative integer.`);
  }

  return parsedValue;
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
