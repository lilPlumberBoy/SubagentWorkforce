const fs = require('node:fs');
const path = require('node:path');

const FRONTEND_SOURCE_ROOT = path.resolve(
  __dirname,
  '..',
  '..',
  'frontend',
  'src'
);

function createTodoBrowserBundle() {
  const moduleSources = {
    '/browser-entry.js': createBrowserEntrySource(),
    '/index.js': loadModuleSource('index.js', (source) =>
      source
        .replace(
          "require('./todos/api-client')",
          "require('/todos/api-client.js')"
        )
        .replace("require('./todos/app')", "require('/todos/app.js')")
    ),
    '/todos/api-client.js': loadModuleSource(path.join('todos', 'api-client.js')),
    '/todos/app.js': loadModuleSource(path.join('todos', 'app.js'), (source) =>
      source.replace("require('./api-client')", "require('/todos/api-client.js')")
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

function loadModuleSource(relativePath, transform = (source) => source) {
  const sourcePath = path.resolve(FRONTEND_SOURCE_ROOT, relativePath);
  return transform(fs.readFileSync(sourcePath, 'utf8'));
}

function createBrowserEntrySource() {
  return `
const runtimeConfig = window.__TODO_RUNTIME_CONFIG__ || {};
const apiBaseUrl =
  typeof runtimeConfig.apiBaseUrl === 'string' && runtimeConfig.apiBaseUrl.length > 0
    ? runtimeConfig.apiBaseUrl
    : window.location.origin;

window.process = window.process || {};
window.process.env = {
  ...(window.process.env || {}),
  TODO_API_BASE_URL: apiBaseUrl,
};

const container = document.getElementById('app');

if (!container) {
  throw new Error('Todo app container not found.');
}

try {
  const { createTodoAppRoot } = require('/index.js');
  createTodoAppRoot(container);
} catch (error) {
  container.textContent =
    error && typeof error.message === 'string'
      ? error.message
      : 'Todo app failed to start.';
  throw error;
}
`;
}

module.exports = {
  createTodoBrowserBundle,
};
