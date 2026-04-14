const { startMonitorRuntime } = require('../src/runtime');

async function main() {
  const initialRunId = readInitialRunId(process.argv.slice(2));
  const runtime = await startMonitorRuntime({
    apiHost: process.env.MONITOR_API_HOST || '127.0.0.1',
    apiPort: parsePort(process.env.MONITOR_API_PORT, 8765, 'MONITOR_API_PORT'),
    frontendHost: process.env.MONITOR_FRONTEND_HOST || '127.0.0.1',
    frontendPort: parsePort(
      process.env.MONITOR_FRONTEND_PORT,
      4273,
      'MONITOR_FRONTEND_PORT'
    ),
    initialRunId,
    projectRoot: process.env.MONITOR_PROJECT_ROOT,
    pythonCommand: process.env.MONITOR_API_PYTHON,
  });
  const frontendUrl = buildFrontendUrl(runtime.frontend.url, runtime.frontend.initialRunId);

  writeLog(
    `Monitor frontend: ${frontendUrl}`
  );
  writeLog(`Monitor API: ${runtime.api.url}`);
  if (runtime.frontend.initialRunId) {
    writeLog(`Selected run: ${runtime.frontend.initialRunId}`);
  }

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
      api: {
        projectRoot: runtime.api.projectRoot,
        url: runtime.api.url,
      },
      frontend: {
        apiBaseUrl: runtime.frontend.apiBaseUrl,
        initialRunId: runtime.frontend.initialRunId,
        url: frontendUrl,
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

function readInitialRunId(args) {
  if (typeof process.env.MONITOR_RUN_ID === 'string' && process.env.MONITOR_RUN_ID.trim().length > 0) {
    return process.env.MONITOR_RUN_ID.trim();
  }

  const [firstArg] = args;
  if (typeof firstArg !== 'string' || firstArg.trim().length === 0) {
    return '';
  }

  return firstArg.trim();
}

function buildFrontendUrl(baseUrl, initialRunId) {
  if (!initialRunId) {
    return baseUrl;
  }

  const url = new URL(baseUrl);
  url.searchParams.set('run', initialRunId);
  return url.toString();
}

function writeLog(message) {
  process.stderr.write(`${message}\n`);
}

main().catch((error) => {
  writeLog(`Failed to start monitor runtime: ${error.message}`);
  process.exitCode = 1;
});
