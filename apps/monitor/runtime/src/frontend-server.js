const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const express = require('express');
const { createMonitorBrowserBundle } = require('./browser-bundle');

const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_PORT = 4273;
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

function createMonitorFrontendServer(options = {}) {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  const initialRunId = normalizeInitialRunId(options.initialRunId);
  const app = express();
  const bundleSource = createMonitorBrowserBundle();

  app.disable('x-powered-by');
  app.get('/', (_request, response) => {
    applyNoStore(response);
    response.type('html');
    response.send(createMonitorFrontendHtml({ apiBaseUrl, initialRunId }));
  });
  app.get('/app.js', (_request, response) => {
    response.type('application/javascript');
    response.send(bundleSource);
  });
  app.get('/assets/react.js', (_request, response) => {
    response.type('application/javascript');
    response.send(fs.readFileSync(REACT_UMD_PATH, 'utf8'));
  });
  app.get('/assets/react-dom.js', (_request, response) => {
    response.type('application/javascript');
    response.send(fs.readFileSync(REACT_DOM_UMD_PATH, 'utf8'));
  });
  app.get('/health', (_request, response) => {
    response.json({
      apiBaseUrl,
      initialRunId,
      status: 'ok',
    });
  });
  app.use((_request, response) => {
    response.status(404).type('text/plain').send('Not found.');
  });

  const server = http.createServer(app);

  return {
    apiBaseUrl,
    initialRunId,
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

async function startMonitorFrontendServer(options = {}) {
  const host = options.host || DEFAULT_HOST;
  const publicHost = options.publicHost || host;
  const port = options.port ?? DEFAULT_PORT;
  const serverContext = createMonitorFrontendServer(options);

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
    url: createBaseUrl(serverContext.server, publicHost),
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
    throw new Error('The monitor frontend runtime requires an absolute apiBaseUrl.');
  }

  let normalizedUrl;

  try {
    normalizedUrl = new URL(apiBaseUrl);
  } catch {
    throw new Error('The monitor frontend apiBaseUrl must be a valid absolute URL.');
  }

  if (!/^https?:$/.test(normalizedUrl.protocol)) {
    throw new Error('The monitor frontend apiBaseUrl must use http or https.');
  }

  normalizedUrl.pathname = normalizedUrl.pathname.replace(/\/+$/, '');

  return normalizedUrl.toString().replace(/\/$/, '');
}

function normalizeInitialRunId(initialRunId) {
  if (typeof initialRunId !== 'string') {
    return '';
  }

  return initialRunId.trim();
}

function applyNoStore(response) {
  response.setHeader('cache-control', 'no-store');
}

function createMonitorFrontendHtml({ apiBaseUrl, initialRunId = '' }) {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Orchestrator Monitor</title>
    <style>
      :root {
        color: #10151d;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at top left, rgba(249, 196, 87, 0.2), transparent 26%),
          linear-gradient(180deg, #f3efe6 0%, #dbe3e9 100%);
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: 100vh;
      }

      button,
      input,
      table {
        font: inherit;
      }

      #app {
        padding: 0;
      }

      .monitor-page {
        display: grid;
        gap: 20px;
        padding-left: 10px;
      }

      .monitor-sidebar-panel,
      .panel-card,
      .run-header-card,
      .empty-state-card {
        border: 1px solid rgba(16, 21, 29, 0.12);
        border-radius: 22px;
        background: rgba(255, 255, 255, 0.9);
        box-shadow: 0 20px 70px rgba(16, 21, 29, 0.08);
      }

      .monitor-sidebar-panel,
      .panel-card,
      .run-header-card,
      .empty-state-card {
        padding: 20px;
      }

      .monitor-sidebar {
        position: fixed;
        top: 2px;
        left: 2px;
        z-index: 40;
        width: 0;
        height: 0;
      }

      .monitor-sidebar-panel {
        display: grid;
        gap: 16px;
        width: min(320px, calc(100vw - 36px));
        max-height: calc(100vh - 8px);
        padding: 72px 18px 18px;
        overflow: hidden;
        backdrop-filter: blur(10px);
        opacity: 0;
        pointer-events: none;
        transform: translateX(-18px);
        transition:
          opacity 140ms ease,
          transform 160ms ease;
      }

      .monitor-sidebar:hover .monitor-sidebar-panel,
      .monitor-sidebar:focus-within .monitor-sidebar-panel {
        opacity: 1;
        pointer-events: auto;
        transform: translateX(0);
      }

      .sidebar-hover-trigger {
        position: absolute;
        top: 0;
        left: 0;
        display: inline-grid;
        gap: 5px;
        width: 28px;
        height: 28px;
        padding: 5px 4px;
        border: none;
        border-radius: 0;
        background: transparent;
        box-shadow: none;
        cursor: pointer;
      }

      .sidebar-hover-trigger-bar {
        display: block;
        width: 100%;
        height: 2px;
        border-radius: 999px;
        background: #132033;
      }

      .eyebrow,
      .kv-label,
      .run-list-meta,
      .objective-meta {
        color: #5f6879;
      }

      .eyebrow {
        margin: 0 0 8px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.75rem;
      }

      h1,
      h2,
      h3,
      p {
        margin-top: 0;
      }

      .sidebar-copy {
        margin-bottom: 0;
        max-width: 28ch;
      }

      .run-list {
        margin-top: 0;
        min-height: 0;
      }

      .run-list-items,
      .objective-list,
      .simple-list,
      .kv-list {
        display: grid;
        gap: 12px;
      }

      .run-list-items {
        max-height: min(68vh, 760px);
        overflow-y: auto;
        padding-right: 4px;
      }

      .run-list-item,
      .table-button {
        border: none;
        background: transparent;
        color: inherit;
        cursor: pointer;
        text-align: left;
      }

      .run-list-item {
        display: grid;
        gap: 4px;
        width: 100%;
        padding: 12px;
        border-radius: 14px;
        border: 1px solid rgba(16, 21, 29, 0.1);
        background: #f8fafb;
      }

      .run-list-title {
        font-size: 0.95rem;
        line-height: 1.3;
      }

      .run-list-meta {
        font-size: 0.84rem;
        line-height: 1.35;
      }

      .run-list-item.is-selected {
        background: #132033;
        color: #f4f0e8;
      }

      .run-list-item.is-selected .run-list-meta {
        color: rgba(244, 240, 232, 0.74);
      }

      .table-button {
        color: #0f2f50;
        text-decoration: underline;
        text-underline-offset: 0.16em;
      }

      .path-link-button {
        padding: 0;
        border: none;
        background: transparent;
        color: #0f2f50;
        cursor: pointer;
        font: inherit;
        text-align: left;
        text-decoration: underline;
        text-underline-offset: 0.16em;
        word-break: break-word;
      }

      .activity-cell {
        display: grid;
        gap: 8px;
      }

      .activity-toggle-button {
        width: fit-content;
        padding: 5px 10px;
        border: 1px solid rgba(15, 47, 80, 0.16);
        border-radius: 999px;
        background: rgba(15, 47, 80, 0.06);
        color: #0f2f50;
        cursor: pointer;
      }

      .monitor-content {
        display: grid;
        gap: 20px;
      }

      .monitor-workspace {
        display: grid;
        gap: 20px;
      }

      .monitor-main-column {
        display: grid;
        gap: 20px;
        min-width: 0;
        overflow-x: auto;
        overflow-y: visible;
      }

      .monitor-main-column-inner {
        display: grid;
        gap: 20px;
        min-width: 50vw;
      }

      .monitor-detail-rail {
        min-width: 0;
        width: 100%;
        max-width: 100%;
      }

      .monitor-rail-resizer {
        display: none;
      }

      .run-status-overview {
        display: grid;
        gap: 12px;
        padding: 14px 16px;
      }

      .run-status-topline {
        display: grid;
        gap: 10px;
      }

      .run-status-main {
        display: grid;
        gap: 4px;
        min-width: 0;
      }

      .run-status-title {
        margin-bottom: 0;
        font-size: 1.02rem;
        line-height: 1.2;
      }

      .run-status-meta {
        margin-bottom: 0;
        color: #5f6879;
        font-size: 0.84rem;
        line-height: 1.3;
      }

      .run-status-health {
        display: grid;
        gap: 8px;
        justify-items: start;
        min-width: 0;
      }

      .run-status-reason {
        margin-bottom: 0;
        color: #314155;
        font-size: 0.86rem;
        line-height: 1.3;
        max-width: 72ch;
      }

      .run-status-actions {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .run-status-toggle {
        padding: 4px 10px;
        font-size: 0.86rem;
      }

      .run-status-summary-grid {
        display: grid;
        gap: 8px;
      }

      .compact-status-section {
        padding: 8px 10px;
        border-radius: 14px;
        border: 1px solid rgba(16, 21, 29, 0.08);
        background: #f8fafb;
        min-width: 0;
      }

      .compact-status-header {
        display: grid;
        gap: 6px;
        align-items: start;
      }

      .compact-status-title {
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: #5f6879;
      }

      .compact-status-summary {
        margin-bottom: 0;
        color: #132033;
        font-size: 0.86rem;
        line-height: 1.22;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .compact-status-body {
        margin-top: 10px;
        padding-top: 10px;
        border-top: 1px solid rgba(16, 21, 29, 0.08);
      }

      .compact-status-section .kv-list {
        gap: 10px;
      }

      .card-grid {
        display: grid;
        gap: 16px;
      }

      .summary-card {
        gap: 0;
        padding: 14px 16px;
      }

      .summary-card-header {
        display: grid;
        gap: 12px;
      }

      .summary-card-copy {
        min-width: 0;
      }

      .summary-card-copy h3 {
        margin-bottom: 6px;
      }

      .summary-card-text {
        margin: 0;
        color: #314155;
        font-size: 0.92rem;
        line-height: 1.35;
      }

      .summary-card-body {
        margin-top: 14px;
        padding-top: 14px;
        border-top: 1px solid rgba(16, 21, 29, 0.08);
      }

      .summary-toggle-button {
        align-self: start;
      }

      .run-header-card {
        display: grid;
        gap: 16px;
      }

      .run-header-status {
        display: grid;
        gap: 10px;
      }

      .status-pill {
        display: inline-flex;
        align-items: center;
        width: fit-content;
        padding: 6px 12px;
        border-radius: 999px;
        font-weight: 700;
        text-transform: capitalize;
      }

      .status-working {
        background: rgba(56, 139, 92, 0.15);
        color: #1f6b41;
      }

      .status-recoverable {
        background: rgba(224, 149, 36, 0.18);
        color: #925400;
      }

      .status-ready-for-review,
      .status-ready-to-advance {
        background: rgba(15, 47, 80, 0.14);
        color: #0f2f50;
      }

      .status-blocked {
        background: rgba(176, 58, 46, 0.16);
        color: #8b2a20;
      }

      .kv-item {
        display: grid;
        gap: 4px;
      }

      .kv-label {
        font-size: 0.84rem;
      }

      .kv-value {
        font-weight: 600;
        word-break: break-word;
      }

      .objective-row {
        display: grid;
        gap: 8px;
        padding: 12px 14px;
        border-radius: 16px;
        background: #f8fafb;
      }

      .objective-summary {
        display: grid;
        gap: 8px;
      }

      .objective-pill {
        display: inline-flex;
        align-items: center;
        width: fit-content;
        padding: 4px 9px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 700;
        line-height: 1;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }

      .objective-pill-phase {
        background: rgba(15, 47, 80, 0.1);
        color: #0f2f50;
      }

      .objective-pill-progress {
        background: rgba(56, 139, 92, 0.14);
        color: #1f6b41;
      }

      .objective-status-list {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
      }

      .objective-status-chip {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(16, 21, 29, 0.08);
        color: #4a5568;
        font-size: 0.76rem;
        line-height: 1.1;
      }

      .objective-activity-list {
        display: grid;
        gap: 6px;
        margin-top: 8px;
        padding-left: 14px;
        border-left: 2px solid rgba(15, 47, 80, 0.12);
      }

      .objective-activity-node {
        display: grid;
        gap: 6px;
      }

      .objective-activity-children {
        display: grid;
        gap: 6px;
        margin-left: 16px;
        padding-left: 14px;
        border-left: 2px solid rgba(15, 47, 80, 0.14);
      }

      .objective-activity-link {
        width: fit-content;
        padding: 0;
        border: none;
        background: transparent;
        color: #0f2f50;
        cursor: pointer;
        font-size: 0.84rem;
        line-height: 1.35;
        text-align: left;
      }

      .objective-activity-link.is-nested {
        position: relative;
      }

      .objective-activity-link.is-nested::before {
        content: "";
        position: absolute;
        left: -16px;
        top: 0.72em;
        width: 10px;
        border-top: 2px solid rgba(15, 47, 80, 0.14);
      }

      .objective-activity-link.is-selected {
        font-weight: 700;
        color: #132033;
      }

      .objective-activity-empty {
        margin-top: 8px;
        font-size: 0.82rem;
      }

      .history-tree {
        display: grid;
        gap: 14px;
      }

      .history-section-header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }

      .history-header-controls {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: flex-end;
        gap: 10px;
      }

      .history-phase-filters {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .history-phase-filter {
        padding: 4px 10px;
        border: 1px solid rgba(16, 21, 29, 0.1);
        border-radius: 999px;
        background: rgba(16, 21, 29, 0.06);
        color: #4a5568;
        cursor: pointer;
        font: inherit;
      }

      .history-phase-filter.is-selected {
        background: #132033;
        color: #f4f0e8;
      }

      .history-pagination {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .history-pagination-button[disabled] {
        cursor: default;
        opacity: 0.45;
      }

      .history-phase-group {
        display: grid;
        gap: 12px;
      }

      .history-phase-header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .history-phase-tree {
        display: grid;
        gap: 12px;
      }

      .history-node {
        display: grid;
        gap: 8px;
        padding: 12px 14px;
        border-radius: 16px;
        background: #f8fafb;
      }

      .history-node-header {
        display: grid;
        gap: 4px;
      }

      .history-node-title {
        font-size: 0.95rem;
        line-height: 1.3;
      }

      .history-node-title-button,
      .history-attempt-button,
      .history-entry-button {
        padding: 0;
        border: none;
        background: transparent;
        color: inherit;
        cursor: pointer;
        font: inherit;
        text-align: left;
      }

      .history-node-title-button {
        font-weight: 700;
      }

      .history-children {
        display: grid;
        gap: 8px;
        margin-left: 12px;
        padding-left: 14px;
        border-left: 2px solid rgba(15, 47, 80, 0.14);
      }

      .history-attempt-group {
        display: grid;
        gap: 8px;
      }

      .history-attempt-header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .history-attempt-children {
        display: grid;
        gap: 8px;
        margin-left: 12px;
        padding-left: 14px;
        border-left: 2px solid rgba(15, 47, 80, 0.12);
      }

      .history-entry {
        position: relative;
        display: grid;
        gap: 4px;
      }

      .history-entry-button.is-selected,
      .history-attempt-button.is-selected,
      .history-node-title-button.is-selected {
        color: #132033;
      }

      .history-entry-button.is-selected .history-entry-summary,
      .history-entry-button.is-selected .history-entry-timestamp {
        color: #132033;
      }

      .history-entry::before {
        content: "";
        position: absolute;
        left: -16px;
        top: 0.8em;
        width: 10px;
        border-top: 2px solid rgba(15, 47, 80, 0.14);
      }

      .history-entry-topline {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }

      .history-entry-status,
      .history-entry-attempt {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(16, 21, 29, 0.08);
        color: #4a5568;
        font-size: 0.76rem;
        line-height: 1.1;
      }

      .history-entry-timestamp {
        font-size: 0.78rem;
        line-height: 1.1;
      }

      .history-entry-status-recovered {
        background: rgba(56, 139, 92, 0.16);
        color: #1f6b41;
      }

      .history-entry-status-ready-for-bundle-review,
      .history-entry-status-completed {
        background: rgba(15, 47, 80, 0.14);
        color: #0f2f50;
      }

      .history-entry-status-failed,
      .history-entry-status-blocked {
        background: rgba(176, 58, 46, 0.16);
        color: #8b2a20;
      }

      .history-entry-status-repair-triggered {
        background: rgba(224, 149, 36, 0.18);
        color: #925400;
      }

      .history-entry-summary {
        margin-bottom: 0;
        color: #314155;
        font-size: 0.85rem;
        line-height: 1.32;
      }

      .table-scroll {
        overflow-x: auto;
      }

      .data-table {
        width: 100%;
        border-collapse: collapse;
      }

      .data-table th,
      .data-table td {
        padding: 10px 12px;
        border-top: 1px solid rgba(16, 21, 29, 0.08);
        vertical-align: top;
        text-align: left;
      }

      .data-table thead th {
        border-top: none;
        color: #5f6879;
        font-size: 0.85rem;
      }

      .is-selected-row {
        background: rgba(15, 47, 80, 0.06);
      }

      .activity-inline-row td {
        padding: 0;
        background: rgba(15, 47, 80, 0.03);
      }

      .activity-inline-cell {
        border-top: none;
      }

      .activity-inline-detail {
        display: grid;
        gap: 14px;
        padding: 14px;
      }

      .recovery-summary-card {
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(224, 149, 36, 0.18);
        background: rgba(249, 196, 87, 0.08);
      }

      .recovery-summary-head {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
      }

      .recovery-summary-title {
        margin: 0;
        font-size: 0.88rem;
        line-height: 1.1;
      }

      .recovery-summary-chip {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(224, 149, 36, 0.14);
        color: #8b5a00;
        font-size: 0.76rem;
        line-height: 1;
        font-weight: 700;
      }

      .recovery-summary-grid {
        display: grid;
        gap: 10px;
      }

      .recovery-summary-block {
        display: grid;
        gap: 3px;
      }

      .recovery-summary-label {
        color: #8b5a00;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        text-transform: uppercase;
      }

      .recovery-summary-copy {
        margin: 0;
        color: #3f3a2f;
        font-size: 0.82rem;
        line-height: 1.3;
      }

      .recovery-summary-command {
        margin: 0;
        color: #5b4631;
        font-size: 0.78rem;
        line-height: 1.3;
        font-family: "SFMono-Regular", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      }

      .recovery-summary-details {
        margin-top: 2px;
      }

      .recovery-summary-details summary {
        cursor: pointer;
        color: #8b5a00;
        font-size: 0.76rem;
        font-weight: 700;
      }

      .recovery-summary-details-body {
        display: grid;
        gap: 8px;
        margin-top: 8px;
      }

      .recovery-summary-path {
        display: grid;
        gap: 3px;
      }

      .recovery-summary-path-value {
        font-size: 0.78rem;
        line-height: 1.25;
      }

      .recovery-summary-pre {
        margin: 0;
        padding: 8px 9px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.6);
        color: #3f3a2f;
        font-size: 0.76rem;
        line-height: 1.35;
        white-space: pre-wrap;
        word-break: break-word;
        font-family: "SFMono-Regular", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      }

      .stdout-failure-card {
        border-color: rgba(176, 58, 46, 0.16);
        background: rgba(176, 58, 46, 0.05);
      }

      .stdout-failure-card .recovery-summary-label,
      .stdout-failure-card .recovery-summary-details summary {
        color: #8b2a20;
      }

      .activity-inspector-card {
        background: rgba(255, 255, 255, 0.96);
        padding: 16px;
        min-height: 220px;
        width: 100%;
        max-width: 100%;
      }

      .activity-inspector-card.is-idle {
        background: rgba(255, 255, 255, 0.82);
      }

      .activity-inspector-header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }

      .activity-inspector-head {
        display: grid;
        gap: 10px;
      }

      .activity-inspector-main {
        display: grid;
        gap: 4px;
      }

      .activity-inspector-identifier {
        margin-bottom: 0;
        color: #314155;
        font-size: 0.84rem;
        line-height: 1.3;
        font-weight: 600;
      }

      .activity-inspector-current {
        margin-bottom: 0;
        color: #132033;
        font-size: 0.98rem;
        line-height: 1.3;
        font-weight: 700;
      }

      .activity-inspector-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }

      .inspector-chip {
        display: inline-flex;
        align-items: center;
        padding: 4px 9px;
        border-radius: 999px;
        background: rgba(15, 47, 80, 0.08);
        color: #0f2f50;
        font-size: 0.8rem;
        line-height: 1;
        font-weight: 700;
      }

      .activity-meta-grid {
        display: grid;
        gap: 10px;
      }

      .activity-meta-section {
        padding: 10px 12px;
        border-radius: 14px;
        background: #f8fafb;
        border: 1px solid rgba(16, 21, 29, 0.08);
      }

      .activity-meta-section-title {
        margin: 0 0 8px;
        color: #5f6879;
        font-size: 0.72rem;
        line-height: 1.1;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }

      .activity-meta-list {
        display: grid;
        gap: 8px;
      }

      .activity-meta-row {
        display: grid;
        gap: 2px;
      }

      .activity-meta-label {
        color: #5f6879;
        font-size: 0.78rem;
        line-height: 1.2;
      }

      .activity-meta-value {
        color: #132033;
        font-size: 0.88rem;
        line-height: 1.3;
        font-weight: 600;
        word-break: break-word;
      }

      .artifact-grid {
        display: grid;
        gap: 12px;
      }

      .artifact-panel {
        padding: 12px;
        border-radius: 14px;
        background: #f8fafb;
        border: 1px solid rgba(16, 21, 29, 0.08);
      }

      .artifact-panel h4 {
        margin: 0;
        font-size: 0.92rem;
      }

      .simple-list {
        margin: 0;
        padding-left: 20px;
      }

      .prompt-debug {
        margin-top: 4px;
      }

      .prompt-debug summary {
        cursor: pointer;
        font-weight: 700;
      }

      .prompt-body {
        margin: 12px 0 0;
        padding: 12px;
        overflow: auto;
        border-radius: 14px;
        background: #0e1724;
        color: #f6f2e9;
        font-size: 0.84rem;
        line-height: 1.4;
        white-space: pre-wrap;
      }

      .inline-error {
        color: #8b2a20;
      }

      @media (min-width: 980px) {
        .monitor-workspace {
          grid-template-columns:
            minmax(0, calc(100% - var(--detail-rail-width, 420px) - 14px))
            6px
            minmax(320px, var(--detail-rail-width, 420px));
          align-items: start;
          column-gap: 4px;
        }

        .monitor-rail-resizer {
          position: relative;
          display: block;
          width: 6px;
          min-height: calc(100vh - 48px);
          cursor: col-resize;
        }

        .monitor-rail-resizer::before {
          content: "";
          position: absolute;
          left: 2px;
          top: 0;
          bottom: 0;
          width: 2px;
          border-radius: 999px;
          background: rgba(15, 47, 80, 0.14);
          transition: background 120ms ease;
        }

        .monitor-rail-resizer:hover::before {
          background: rgba(15, 47, 80, 0.34);
        }

        .monitor-detail-rail {
          position: sticky;
          top: 24px;
          align-self: start;
          max-height: calc(100vh - 48px);
          overflow: auto;
          padding-bottom: 4px;
        }

        .run-status-topline {
          grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
          align-items: start;
        }

        .run-status-health {
          justify-items: start;
        }

        .run-status-reason {
          text-align: left;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: normal;
          max-width: none;
        }

        .run-status-summary-grid {
          grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        }

        .compact-status-header {
          grid-template-columns: auto minmax(0, 1fr);
        }

        .compact-status-summary {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .card-grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .run-header-card {
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: start;
        }

        .detail-grid {
          grid-template-columns: repeat(3, minmax(0, 1fr));
        }

        .activity-inspector-head {
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: start;
        }

        .activity-inspector-chips {
          justify-content: flex-end;
        }

        .activity-meta-grid {
          grid-template-columns: repeat(3, minmax(0, 1fr));
        }

        .recovery-summary-grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .artifact-grid {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .artifact-panel:last-child:nth-child(odd) {
          grid-column: 1 / -1;
        }

        .summary-card-header {
          grid-template-columns: minmax(0, 1fr) auto;
          align-items: start;
        }

        .summary-card-text {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .objective-summary {
          grid-template-columns: auto auto minmax(0, 1fr);
          align-items: center;
        }
      }

      @media (max-width: 979px) {
        .monitor-workspace {
          grid-template-columns: minmax(0, 1fr);
        }

        #app {
          padding: 4px;
        }

        .monitor-page {
          gap: 16px;
          padding-left: 8px;
        }

        .monitor-sidebar {
          top: 2px;
          left: 2px;
        }

        .monitor-sidebar-panel {
          width: min(300px, calc(100vw - 24px));
          max-height: calc(100vh - 8px);
          padding: 64px 16px 16px;
        }

        .sidebar-hover-trigger {
          width: 28px;
          height: 28px;
          padding: 5px 4px;
          border-radius: 8px;
        }

        .run-status-actions {
          justify-content: flex-start;
        }
      }

      @media (min-width: 1320px) {
        .card-grid {
          grid-template-columns: repeat(4, minmax(0, 1fr));
        }
      }
    </style>
  </head>
  <body>
    <div id="app"></div>
    <script>
      window.__MONITOR_RUNTIME_CONFIG__ = ${JSON.stringify({
        apiBaseUrl,
        initialRunId,
      })};
    </script>
    <script src="/assets/react.js"></script>
    <script src="/assets/react-dom.js"></script>
    <script src="/app.js"></script>
  </body>
</html>`;
}

module.exports = {
  createMonitorFrontendHtml,
  createMonitorFrontendServer,
  startMonitorFrontendServer,
};
