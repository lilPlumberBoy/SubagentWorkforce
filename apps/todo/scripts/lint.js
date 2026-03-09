const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const ROOTS = [
  'apps/todo/backend',
  'apps/todo/frontend',
  'apps/todo/runtime',
  'apps/todo/scripts',
];

function main() {
  const files = ROOTS.flatMap((root) => collectJavaScriptFiles(path.join(REPO_ROOT, root)));

  if (files.length === 0) {
    throw new Error('No JavaScript files were found for lint validation.');
  }

  for (const file of files) {
    const result = spawnSync(process.execPath, ['--check', file], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
    });

    if (result.status !== 0) {
      process.stdout.write(result.stdout || '');
      process.stderr.write(result.stderr || '');
      throw new Error(`Syntax validation failed for ${path.relative(REPO_ROOT, file)}.`);
    }
  }

  process.stdout.write(`Syntax lint passed for ${files.length} JavaScript files.\n`);
}

function collectJavaScriptFiles(directory) {
  if (!fs.existsSync(directory)) {
    return [];
  }

  const entries = fs.readdirSync(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    if (entry.name === 'dist' || entry.name === 'node_modules') {
      continue;
    }

    const fullPath = path.join(directory, entry.name);

    if (entry.isDirectory()) {
      files.push(...collectJavaScriptFiles(fullPath));
      continue;
    }

    if (entry.isFile() && entry.name.endsWith('.js')) {
      files.push(fullPath);
    }
  }

  return files.sort();
}

main();
