const assert = require('node:assert/strict');
const { JSDOM, VirtualConsole } = require('jsdom');

async function loadTodoRuntimePage(url) {
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
    document: dom.window.document,
    getByText: (text) => getByText(dom.window.document, text),
    getInputByLabelText: (label) => getInputByLabelText(dom.window.document, label),
    queryByText: (text) => queryByText(dom.window.document, text),
    setCheckboxValue: async (label, checked) => {
      const input = getInputByLabelText(dom.window.document, label);

      if (input.checked === checked) {
        return;
      }

      input.click();
      await flush(dom.window);
      assertNoPageErrors(pageErrors);
    },
    setInputValue: async (label, value) => {
      const input = getInputByLabelText(dom.window.document, label);
      setInputValue(input, value);
      input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      await flush(dom.window);
      assertNoPageErrors(pageErrors);
    },
    submitForm: async (label) => {
      const form = getForm(dom.window.document, label);
      form.dispatchEvent(
        new dom.window.Event('submit', {
          bubbles: true,
          cancelable: true,
        })
      );
      await flush(dom.window);
      assertNoPageErrors(pageErrors);
    },
    waitFor: (assertion, options = {}) =>
      waitFor(async () => {
        await flush(dom.window);
        assertNoPageErrors(pageErrors);
        return assertion();
      }, options),
    window: dom.window,
  };
}

function assertNoPageErrors(pageErrors) {
  if (pageErrors.length > 0) {
    throw pageErrors[0];
  }
}

function getForm(document, label) {
  const forms = Array.from(document.querySelectorAll('form'));
  const matchingForm = forms.find(
    (form) => form.getAttribute('aria-label') === label
  );

  assert.ok(matchingForm, `Expected form "${label}" to exist.`);

  return matchingForm;
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

function getInputByLabelText(document, labelText) {
  const labels = Array.from(document.querySelectorAll('label'));
  const matchingLabel = labels.find(
    (label) => normalizeText(label.textContent || '') === normalizeText(labelText)
  );

  assert.ok(matchingLabel, `Expected label "${labelText}" to exist.`);

  const inputId = matchingLabel.getAttribute('for');

  if (inputId) {
    const input = document.getElementById(inputId);
    assert.ok(input, `Expected input "${labelText}" to exist.`);
    return input;
  }

  const nestedInput = matchingLabel.querySelector('input');

  assert.ok(nestedInput, `Expected input "${labelText}" to exist.`);

  return nestedInput;
}

function getByText(document, text) {
  const matchingNode = queryByText(document, text);

  assert.ok(matchingNode, `Expected text "${text}" to exist.`);

  return matchingNode;
}

function queryByText(document, text) {
  const matcher =
    typeof text === 'string'
      ? (nodeText) => nodeText.includes(text)
      : (nodeText) => text.test(nodeText);
  const candidates = Array.from(document.querySelectorAll('body *'));

  return (
    candidates.find((node) => matcher(normalizeText(node.textContent || ''))) || null
  );
}

function normalizeText(value) {
  return value.replace(/\s+/g, ' ').trim();
}

function setInputValue(input, value) {
  const descriptor = Object.getOwnPropertyDescriptor(
    input.ownerDocument.defaultView.HTMLInputElement.prototype,
    'value'
  );

  descriptor.set.call(input, value);
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
  loadTodoRuntimePage,
};
