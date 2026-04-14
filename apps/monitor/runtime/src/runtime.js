const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { startMonitorFrontendServer } = require('./frontend-server');

const DEFAULT_API_HOST = '127.0.0.1';
const DEFAULT_API_PORT = 8765;
const DEFAULT_FRONTEND_HOST = '127.0.0.1';
const DEFAULT_FRONTEND_PORT = 4273;
const DEFAULT_PYTHON = process.env.MONITOR_API_PYTHON || process.env.PYTHON || 'python3';
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

async function startMonitorRuntime(options = {}) {
  const apiHost = options.apiHost || DEFAULT_API_HOST;
  const frontendHost = options.frontendHost || DEFAULT_FRONTEND_HOST;
  const apiPublicHost = normalizePublicHost(apiHost);
  const frontendPublicHost = normalizePublicHost(frontendHost);
  const apiPort = await resolveListenPort(
    apiHost,
    options.apiPort ?? DEFAULT_API_PORT,
    'MONITOR_API_PORT'
  );
  const frontendPort = await resolveListenPort(
    frontendHost,
    options.frontendPort ?? DEFAULT_FRONTEND_PORT,
    'MONITOR_FRONTEND_PORT'
  );
  const projectRoot = path.resolve(options.projectRoot || REPO_ROOT);

  const api = await startMonitorApiProcess({
    host: apiHost,
    port: apiPort,
    projectRoot,
    publicHost: apiPublicHost,
    pythonCommand: options.pythonCommand || DEFAULT_PYTHON,
  });

  let frontend;

  try {
    frontend = await startMonitorFrontendServer({
      apiBaseUrl: api.url,
      host: frontendHost,
      initialRunId: options.initialRunId,
      port: frontendPort,
      publicHost: frontendPublicHost,
    });
  } catch (error) {
    await api.close();
    throw wrapListenError('monitor frontend', frontendHost, frontendPort, error);
  }

  return {
    api,
    close: async () => {
      const errors = [];

      await closeRuntimePart(frontend, errors);
      await closeRuntimePart(api, errors);

      if (errors.length > 0) {
        throw errors[0];
      }
    },
    frontend,
  };
}

async function startMonitorApiProcess({
  host,
  port,
  projectRoot,
  publicHost,
  pythonCommand,
}) {
  const child = spawn(
    pythonCommand,
    [
      '-m',
      'company_orchestrator.monitor_api',
      '--project-root',
      projectRoot,
      '--host',
      host,
      '--port',
      String(port),
    ],
    {
      cwd: REPO_ROOT,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );

  let stdout = '';
  let stderr = '';

  child.stdout.on('data', (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });

  const url = `http://${publicHost}:${port}`;

  try {
    await waitFor(async () => {
      if (child.exitCode !== null) {
        throw new Error(
          formatApiStartupFailure({
            host,
            port,
            projectRoot,
            stderr,
            stdout,
          })
        );
      }

      const response = await fetch(`${url}/health`);
      if (response.status !== 200) {
        throw new Error(`Monitor API health returned ${response.status}.`);
      }
    });
  } catch (error) {
    child.kill('SIGTERM');
    await waitForChildExit(child).catch(() => {});
    throw error;
  }

  return {
    close: async () => {
      if (child.exitCode !== null) {
        return;
      }

      child.kill('SIGTERM');
      await waitForChildExit(child);
    },
    projectRoot,
    url,
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

async function waitFor(assertion, options = {}) {
  const timeoutMs = options.timeoutMs || 5000;
  const intervalMs = options.intervalMs || 30;
  const startTime = Date.now();
  let lastError;

  while (Date.now() - startTime < timeoutMs) {
    try {
      return await assertion();
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
  }

  throw lastError;
}

async function waitForChildExit(child) {
  if (child.exitCode !== null) {
    return;
  }

  await new Promise((resolve) => {
    child.once('exit', resolve);
  });
}

module.exports = {
  startMonitorRuntime,
};

function formatApiStartupFailure({
  host,
  port,
  projectRoot,
  stderr,
  stdout,
}) {
  const output = stderr.trim() || stdout.trim() || 'Monitor API exited early.';
  if (/EADDRINUSE|Address already in use/i.test(output)) {
    return `Could not start monitor API on http://${host}:${port} because the port is already in use. Pass MONITOR_API_PORT=0 or --api-port <port>. Project root: ${projectRoot}`;
  }

  return `Could not start monitor API on http://${host}:${port}. Project root: ${projectRoot}. Details: ${output}`;
}

function wrapListenError(serviceName, host, port, error) {
  if (!error || typeof error.message !== 'string') {
    return error;
  }

  if (error.code === 'EADDRINUSE' || /EADDRINUSE/i.test(error.message)) {
    return new Error(
      `Could not start ${serviceName} on http://${host}:${port} because the port is already in use. Pass MONITOR_FRONTEND_PORT=0 or --frontend-port <port>.`
    );
  }

  return new Error(`Could not start ${serviceName} on http://${host}:${port}. ${error.message}`);
}
