const http = require('node:http');
const path = require('node:path');
const express = require('express');
const { createTodoBrowserBundle } = require('./browser-bundle');

const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_PORT = 4173;
const REACT_UMD_PATH = path.join(
  path.dirname(require.resolve('react/package.json')),
  'umd',
  'react.development.js'
);
const REACT_DOM_UMD_PATH = path.join(
  path.dirname(require.resolve('react-dom/package.json')),
  'umd',
  'react-dom.development.js'
);

function createTodoFrontendServer(options = {}) {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  const app = express();
  const bundleSource = createTodoBrowserBundle();

  app.disable('x-powered-by');
  app.get('/', (_request, response) => {
    response.type('html');
    response.send(createTodoFrontendHtml({ apiBaseUrl }));
  });
  app.get('/app.js', (_request, response) => {
    response.type('application/javascript');
    response.send(bundleSource);
  });
  app.get('/assets/react.js', (_request, response) => {
    response.sendFile(REACT_UMD_PATH);
  });
  app.get('/assets/react-dom.js', (_request, response) => {
    response.sendFile(REACT_DOM_UMD_PATH);
  });
  app.get('/health', (_request, response) => {
    response.json({
      apiBaseUrl,
      status: 'ok',
    });
  });
  app.use((_request, response) => {
    response.status(404).type('text/plain').send('Not found.');
  });

  const server = http.createServer(app);

  return {
    apiBaseUrl,
    close: () =>
      new Promise((resolve, reject) => {
        if (!server.listening) {
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
    server,
  };
}

async function startTodoFrontendServer(options = {}) {
  const host = options.host || DEFAULT_HOST;
  const port = options.port ?? DEFAULT_PORT;
  const serverContext = createTodoFrontendServer(options);

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

function createBaseUrl(server, host) {
  const address = server.address();

  if (!address || typeof address === 'string') {
    throw new Error('Frontend server address is unavailable.');
  }

  return `http://${host}:${address.port}`;
}

function normalizeApiBaseUrl(apiBaseUrl) {
  if (typeof apiBaseUrl !== 'string' || apiBaseUrl.trim().length === 0) {
    throw new Error('The todo frontend runtime requires an absolute apiBaseUrl.');
  }

  let normalizedUrl;

  try {
    normalizedUrl = new URL(apiBaseUrl);
  } catch {
    throw new Error('The todo frontend apiBaseUrl must be a valid absolute URL.');
  }

  if (!/^https?:$/.test(normalizedUrl.protocol)) {
    throw new Error('The todo frontend apiBaseUrl must use http or https.');
  }

  normalizedUrl.pathname = normalizedUrl.pathname.replace(/\/+$/, '');

  return normalizedUrl.toString().replace(/\/$/, '');
}

function createTodoFrontendHtml({ apiBaseUrl }) {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Todo Runtime</title>
    <style>
      :root {
        color: #172033;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at top, #fff6d9, transparent 32%),
          linear-gradient(180deg, #f6f2ea 0%, #e9efe8 100%);
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
      }

      #app {
        padding: 48px 20px 64px;
      }

      .todo-page {
        width: min(720px, 100%);
        margin: 0 auto;
        padding: 32px;
        border: 1px solid rgba(23, 32, 51, 0.1);
        border-radius: 24px;
        background: rgba(255, 255, 255, 0.9);
        box-shadow: 0 24px 80px rgba(23, 32, 51, 0.12);
      }

      .todo-header h1 {
        margin: 0 0 8px;
        font-size: clamp(2rem, 5vw, 3.5rem);
        line-height: 0.95;
      }

      .todo-header p {
        margin: 0 0 24px;
        color: #4f5d75;
      }

      .todo-create-form,
      .todo-list-region ul,
      .todo-list-region li,
      .todo-list-region form,
      .todo-list-region div {
        display: grid;
        gap: 12px;
      }

      .todo-list-region ul {
        list-style: none;
        padding: 0;
        margin: 0;
      }

      .todo-list-region li {
        padding: 16px 18px;
        border-radius: 18px;
        background: #f8faf7;
        border: 1px solid rgba(23, 32, 51, 0.08);
      }

      input[type="text"] {
        width: 100%;
        padding: 12px 14px;
        border: 1px solid rgba(23, 32, 51, 0.18);
        border-radius: 14px;
        font: inherit;
        background: white;
      }

      button {
        padding: 10px 14px;
        border: 0;
        border-radius: 999px;
        font: inherit;
        font-weight: 600;
        color: white;
        background: #1e5eff;
        cursor: pointer;
      }

      button[disabled],
      input[disabled] {
        cursor: not-allowed;
        opacity: 0.65;
      }

      label {
        display: grid;
        gap: 8px;
      }

      p[role="alert"] {
        margin: 0;
        color: #9d1c36;
      }

      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
    </style>
    <script>
      window.__TODO_RUNTIME_CONFIG__ = ${JSON.stringify({ apiBaseUrl })};
    </script>
  </head>
  <body>
    <div id="app" aria-live="polite"></div>
    <script src="/assets/react.js"></script>
    <script src="/assets/react-dom.js"></script>
    <script src="/app.js"></script>
  </body>
</html>`;
}

module.exports = {
  createTodoFrontendHtml,
  createTodoFrontendServer,
  startTodoFrontendServer,
};
