const assert = require('node:assert/strict');
const { JSDOM, VirtualConsole } = require('jsdom');

async function loadMonitorRuntimePage(url) {
  const virtualConsole = new VirtualConsole();
  const pageErrors = [];
  virtualConsole.on('jsdomError', (error) => {
    pageErrors.push(error);
  });

  const dom = await JSDOM.fromURL(url, {
    beforeParse(window) {
      window.fetch = globalThis.fetch.bind(globalThis);
      window.AbortController = globalThis.AbortController;
      window.TextDecoder = globalThis.TextDecoder;
      window.TextEncoder = globalThis.TextEncoder;
    },
    pretendToBeVisual: true,
    resources: 'usable',
    runScripts: 'dangerously',
    virtualConsole,
  });

  await waitForLoad(dom.window);
  assertNoPageErrors(pageErrors);

  return {
    cleanup() {
      dom.window.close();
    },
    clickButton: async (label) => {
      const button = getButton(dom.window.document, label);
      button.dispatchEvent(
        new dom.window.MouseEvent('click', {
          bubbles: true,
          cancelable: true,
        })
      );
      await flush(dom.window);
      assertNoPageErrors(pageErrors);
    },
    getByText: (text) => getByText(dom.window.document, text),
    waitFor: (assertion, options = {}) =>
      waitFor(async () => {
        await flush(dom.window);
        assertNoPageErrors(pageErrors);
        return assertion();
      }, options),
  };
}

function getButton(document, label) {
  const buttons = Array.from(document.querySelectorAll('button'));
  const matchingButton = buttons.find(
    (button) =>
      normalizeText(button.textContent || '') === normalizeText(label) ||
      button.getAttribute('aria-label') === label
  );

  assert.ok(matchingButton, `Expected button "${label}" to exist.`);

  return matchingButton;
}

function getByText(document, text) {
  const matcher =
    typeof text === 'string'
      ? (nodeText) => nodeText.includes(text)
      : (nodeText) => text.test(nodeText);
  const candidates = Array.from(document.querySelectorAll('body *'));
  const matchingNode = candidates.find((node) =>
    matcher(normalizeText(node.textContent || ''))
  );

  assert.ok(matchingNode, `Expected text "${text}" to exist.`);
  return matchingNode;
}

function normalizeText(value) {
  return value.replace(/\s+/g, ' ').trim();
}

function assertNoPageErrors(pageErrors) {
  if (pageErrors.length > 0) {
    throw pageErrors[0];
  }
}

async function flush(window) {
  await new Promise((resolve) => window.setTimeout(resolve, 0));
}

async function waitForLoad(window) {
  if (window.document.readyState === 'complete') {
    return;
  }

  await new Promise((resolve) => {
    window.addEventListener('load', resolve, { once: true });
  });
}

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

module.exports = {
  loadMonitorRuntimePage,
};
