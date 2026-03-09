const { startTodoHttpServer } = require('../src/server');

async function main() {
  const port = process.env.PORT ? Number(process.env.PORT) : 3000;
  const host = process.env.HOST || '127.0.0.1';
  const allowedOrigin = process.env.TODO_ALLOWED_ORIGIN;

  if (!Number.isInteger(port) || port < 0) {
    throw new Error('PORT must be a non-negative integer.');
  }

  const server = await startTodoHttpServer({ allowedOrigin, host, port });

  const shutdown = async () => {
    process.removeListener('SIGINT', shutdown);
    process.removeListener('SIGTERM', shutdown);

    try {
      await server.close();
    } finally {
      process.exit(0);
    }
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  process.stdout.write(
    JSON.stringify({
      allowedOrigin: server.allowedOrigin || null,
      databasePath: server.databasePath,
      status: 'listening',
      url: server.url,
    })
  );
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
