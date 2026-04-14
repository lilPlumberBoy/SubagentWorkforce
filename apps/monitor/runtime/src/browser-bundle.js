const fs = require('node:fs');
const path = require('node:path');

const FRONTEND_SOURCE_ROOT = path.resolve(
  __dirname,
  '..',
  '..',
  'frontend',
  'src'
);

function createMonitorBrowserBundle() {
  const moduleSources = {
    '/browser-entry.js': createBrowserEntrySource(),
    '/index.js': loadModuleSource('index.js', (source) =>
      source
        .replace(
          "require('./monitor/api-client')",
          "require('/monitor/api-client.js')"
        )
        .replace("require('./monitor/app')", "require('/monitor/app.js')")
    ),
    '/monitor/api-client.js': loadModuleSource(
      path.join('monitor', 'api-client.js')
    ),
    '/monitor/app.js': loadModuleSource(
      path.join('monitor', 'app.js'),
      (source) =>
        source.replace("require('./api-client')", "require('/monitor/api-client.js')")
    ),
  };

  const moduleFactories = Object.entries(moduleSources)
    .map(
      ([moduleId, source]) =>
        `${JSON.stringify(moduleId)}: function(module, exports, require) {\n${source}\n}`
    )
    .join(',\n');

  return `(() => {
  'use strict';

  const moduleFactories = {
${moduleFactories}
  };
  const moduleCache = Object.create(null);

  function require(moduleId) {
    if (moduleId === 'react') {
      return window.React;
    }

    if (moduleId === 'react-dom/client') {
      return { createRoot: window.ReactDOM.createRoot };
    }

    if (Object.prototype.hasOwnProperty.call(moduleCache, moduleId)) {
      return moduleCache[moduleId].exports;
    }

    const factory = moduleFactories[moduleId];

    if (typeof factory !== 'function') {
      throw new Error('Unknown browser module: ' + moduleId);
    }

    const module = { exports: {} };
    moduleCache[moduleId] = module;
    factory(module, module.exports, require);
    return module.exports;
  }

  require('/browser-entry.js');
})();`;
}

function createBrowserEntrySource() {
  return `
const runtimeConfig = window.__MONITOR_RUNTIME_CONFIG__ || {};
const apiBaseUrl =
  typeof runtimeConfig.apiBaseUrl === 'string' && runtimeConfig.apiBaseUrl.length > 0
    ? runtimeConfig.apiBaseUrl
    : window.location.origin;
const initialRunId =
  typeof runtimeConfig.initialRunId === 'string' && runtimeConfig.initialRunId.length > 0
    ? runtimeConfig.initialRunId
    : '';

window.process = window.process || {};
window.process.env = {
  ...(window.process.env || {}),
  MONITOR_API_BASE_URL: apiBaseUrl,
};

const container = document.getElementById('app');

if (!container) {
  throw new Error('Monitor app container not found.');
}

try {
  const { createMonitorAppRoot } = require('/index.js');
  createMonitorAppRoot(container, {
    initialRunId,
  });
} catch (error) {
  container.textContent =
    error && typeof error.message === 'string'
      ? error.message
      : 'Monitor app failed to start.';
  throw error;
}
`;
}

function loadModuleSource(relativePath, transform = (source) => source) {
  const sourcePath = path.resolve(FRONTEND_SOURCE_ROOT, relativePath);
  return transform(fs.readFileSync(sourcePath, 'utf8'));
}

module.exports = {
  createMonitorBrowserBundle,
};
