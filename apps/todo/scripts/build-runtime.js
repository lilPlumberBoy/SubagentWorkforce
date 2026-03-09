const fs = require('node:fs');
const path = require('node:path');

const { createTodoBrowserBundle } = require('../runtime/src/browser-bundle');
const { createTodoFrontendHtml } = require('../runtime/src/frontend-server');

const APP_ROOT = path.resolve(__dirname, '..');
const DIST_ROOT = path.join(APP_ROOT, 'runtime', 'dist');
const ASSET_ROOT = path.join(DIST_ROOT, 'assets');
const DEFAULT_API_BASE_URL = 'http://127.0.0.1:3000';
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

function main() {
  fs.rmSync(DIST_ROOT, { force: true, recursive: true });
  fs.mkdirSync(ASSET_ROOT, { recursive: true });

  const bundleSource = createTodoBrowserBundle();
  const html = createTodoFrontendHtml({ apiBaseUrl: DEFAULT_API_BASE_URL });
  const manifest = {
    apiBaseUrl: DEFAULT_API_BASE_URL,
    files: ['app.js', 'assets/react.js', 'assets/react-dom.js', 'index.html'],
    generatedAt: new Date().toISOString(),
  };

  fs.writeFileSync(path.join(DIST_ROOT, 'app.js'), bundleSource);
  fs.writeFileSync(path.join(DIST_ROOT, 'index.html'), html);
  fs.writeFileSync(
    path.join(DIST_ROOT, 'manifest.json'),
    JSON.stringify(manifest, null, 2) + '\n'
  );
  fs.copyFileSync(REACT_UMD_PATH, path.join(ASSET_ROOT, 'react.js'));
  fs.copyFileSync(REACT_DOM_UMD_PATH, path.join(ASSET_ROOT, 'react-dom.js'));

  process.stdout.write(
    `Built todo runtime frontend assets at ${path.relative(APP_ROOT, DIST_ROOT)}.\n`
  );
}

main();
