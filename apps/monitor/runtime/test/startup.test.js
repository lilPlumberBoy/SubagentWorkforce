const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

test('runtime startup script reports the integrated workflow and serves readiness surfaces', async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-runtime-start-'));
  const child = spawn(
    process.execPath,
    ['--no-warnings', 'apps/monitor/runtime/scripts/start.js', 'auto-todo-mvp-094-full'],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        MONITOR_PROJECT_ROOT: tempDirectory,
        MONITOR_API_PORT: '0',
        MONITOR_FRONTEND_PORT: '0',
      },
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

  try {
    const startupPayload = await waitFor(async () => {
      assert.equal(child.exitCode, null, stderr || 'Runtime process exited early.');

      const line = stdout
        .split(/\r?\n/)
        .map((entry) => entry.trim())
        .find(Boolean);

      assert.ok(line, 'Expected runtime startup output.');

      return JSON.parse(line);
    });

    assert.equal(startupPayload.status, 'listening');
    assert.equal(
      fs.realpathSync(startupPayload.api.projectRoot),
      fs.realpathSync(path.resolve(tempDirectory))
    );
    assert.equal(startupPayload.frontend.apiBaseUrl, startupPayload.api.url);
    assert.equal(startupPayload.frontend.initialRunId, 'auto-todo-mvp-094-full');
    assert.match(startupPayload.frontend.url, /\?run=auto-todo-mvp-094-full$/);

    const apiHealthResponse = await fetch(
      new URL('/health', startupPayload.api.url)
    );
    assert.equal(apiHealthResponse.status, 200);
    const apiHealth = await apiHealthResponse.json();
    assert.equal(
      fs.realpathSync(apiHealth.projectRoot),
      fs.realpathSync(path.resolve(tempDirectory))
    );
    assert.equal(apiHealth.status, 'ok');

    const frontendHealthResponse = await fetch(
      new URL('/health', startupPayload.frontend.url)
    );
    assert.equal(frontendHealthResponse.status, 200);
    const frontendHealth = await frontendHealthResponse.json();
    assert.equal(frontendHealth.apiBaseUrl, startupPayload.api.url);
    assert.equal(frontendHealth.initialRunId, 'auto-todo-mvp-094-full');

    const frontendHtmlResponse = await fetch(startupPayload.frontend.url);
    assert.equal(frontendHtmlResponse.status, 200);
    assert.equal(frontendHtmlResponse.headers.get('cache-control'), 'no-store');
    const frontendHtml = await frontendHtmlResponse.text();
    assert.match(frontendHtml, /__MONITOR_RUNTIME_CONFIG__/);

    const apiRootResponse = await fetch(startupPayload.api.url);
    assert.equal(apiRootResponse.status, 200);
    const apiRootPayload = await apiRootResponse.json();
    assert.equal(apiRootPayload.service, 'monitor-api');
    assert.match(apiRootPayload.message, /not the browser frontend/i);
  } finally {
    child.kill('SIGTERM');
    await waitForChildExit(child);
    fs.rmSync(tempDirectory, { force: true, recursive: true });
  }

  assert.match(stderr, /Monitor frontend:/);
  assert.match(stderr, /Monitor API:/);
  assert.match(stderr, /Selected run: auto-todo-mvp-094-full/);
});

async function waitFor(assertion, options = {}) {
  const timeoutMs = options.timeoutMs || 5000;
  const intervalMs = options.intervalMs || 20;
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
