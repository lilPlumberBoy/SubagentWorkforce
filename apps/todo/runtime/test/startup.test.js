const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');

test('runtime startup script reports the integrated workflow and serves both readiness surfaces', async () => {
  const tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'todo-runtime-start-'));
  const databasePath = path.join(tempDirectory, 'runtime.sqlite');
  const child = spawn(
    process.execPath,
    ['--no-warnings', 'apps/todo/runtime/scripts/start.js'],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        TODO_BACKEND_DB_PATH: databasePath,
        TODO_BACKEND_PORT: '0',
        TODO_FRONTEND_PORT: '0',
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
      startupPayload.backend.databasePath,
      path.resolve(databasePath)
    );
    assert.equal(
      startupPayload.backend.allowedOrigin,
      startupPayload.frontend.url
    );
    assert.equal(
      startupPayload.frontend.apiBaseUrl,
      startupPayload.backend.url
    );

    const frontendHealthResponse = await fetch(`${startupPayload.frontend.url}/health`);
    assert.equal(frontendHealthResponse.status, 200);
    const frontendHealth = await frontendHealthResponse.json();
    assert.equal(frontendHealth.apiBaseUrl, startupPayload.backend.url);

    const frontendHtmlResponse = await fetch(startupPayload.frontend.url);
    assert.equal(frontendHtmlResponse.status, 200);
    const frontendHtml = await frontendHtmlResponse.text();
    assert.match(frontendHtml, /__TODO_RUNTIME_CONFIG__/);

    const backendListResponse = await fetch(`${startupPayload.backend.url}/api/todos`);
    assert.equal(backendListResponse.status, 200);
    const backendList = await backendListResponse.json();
    assert.deepEqual(backendList, { items: [] });
  } finally {
    child.kill('SIGTERM');
    await waitForChildExit(child);
    fs.rmSync(tempDirectory, { force: true, recursive: true });
  }

  assert.equal(stderr.trim(), '');
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
