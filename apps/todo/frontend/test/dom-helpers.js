const assert = require('node:assert/strict');

const React = require('react');
const { JSDOM } = require('jsdom');
const ReactClient = require('react-dom/client');

const { TodoApp } = require('../src');
const { act } = React;

async function renderTodoApp(options) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1/',
  });
  const previousGlobals = installDomGlobals(dom.window);
  const container = dom.window.document.createElement('div');
  dom.window.document.body.append(container);
  const root = ReactClient.createRoot(container);

  await act(async () => {
    root.render(
      React.createElement(TodoApp, {
        client: options.client,
      })
    );
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  return {
    cleanup: async () => {
      await act(async () => {
        root.unmount();
      });
      restoreDomGlobals(previousGlobals);
      dom.window.close();
    },
    clickButton: async (label) => {
      const button = getButton(dom.window.document, label);

      await act(async () => {
        button.dispatchEvent(
          new dom.window.MouseEvent('click', {
            bubbles: true,
            cancelable: true,
          })
        );
      });
    },
    container,
    document: dom.window.document,
    getButton: (label) => getButton(dom.window.document, label),
    getByText: (text) => getByText(dom.window.document, text),
    getInputByLabelText: (label) => getInputByLabelText(dom.window.document, label),
    queryByText: (text) => queryByText(dom.window.document, text),
    setCheckboxValue: async (label, checked) => {
      const input = getInputByLabelText(dom.window.document, label);

      if (input.checked === checked) {
        return;
      }

      await act(async () => {
        input.click();
      });
    },
    setInputValue: async (label, value) => {
      const input = getInputByLabelText(dom.window.document, label);

      setInputValue(input, value);

      await act(async () => {
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
        input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
    },
    submitForm: async (label) => {
      const form = getForm(dom.window.document, label);

      await act(async () => {
        form.dispatchEvent(
          new dom.window.Event('submit', {
            bubbles: true,
            cancelable: true,
          })
        );
      });
    },
    waitFor: async (assertion, options = {}) => {
      const timeoutMs = options.timeoutMs || 2000;
      const startTime = Date.now();
      let lastError;

      while (Date.now() - startTime < timeoutMs) {
        try {
          await act(async () => {
            await new Promise((resolve) => setTimeout(resolve, 0));
          });
          return assertion();
        } catch (error) {
          lastError = error;
          await new Promise((resolve) => setTimeout(resolve, 20));
        }
      }

      throw lastError;
    },
  };
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
      button.textContent.trim() === label ||
      button.getAttribute('aria-label') === label
  );

  assert.ok(matchingButton, `Expected button "${label}" to exist.`);

  return matchingButton;
}

function getInputByLabelText(document, labelText) {
  const labels = Array.from(document.querySelectorAll('label'));
  const matchingLabel = labels.find(
    (label) => normalizeText(label.textContent) === normalizeText(labelText)
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
  const matcher = typeof text === 'string'
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

function installDomGlobals(window) {
  const globalKeys = [
    'window',
    'document',
    'navigator',
    'HTMLElement',
    'HTMLInputElement',
    'Node',
    'Event',
    'MouseEvent',
    'KeyboardEvent',
    'DOMException',
    'IS_REACT_ACT_ENVIRONMENT',
  ];
  const previousGlobals = new Map();
  const prototypeTargets = [
    window.Element?.prototype,
    window.HTMLElement?.prototype,
    window.HTMLInputElement?.prototype,
  ].filter(Boolean);

  for (const prototype of prototypeTargets) {
    if (typeof prototype.attachEvent !== 'function') {
      prototype.attachEvent = function attachEvent() {};
    }

    if (typeof prototype.detachEvent !== 'function') {
      prototype.detachEvent = function detachEvent() {};
    }
  }

  for (const key of globalKeys) {
    previousGlobals.set(key, globalThis[key]);

    if (key === 'IS_REACT_ACT_ENVIRONMENT') {
      globalThis[key] = true;
      continue;
    }

    globalThis[key] = window[key];
  }

  return previousGlobals;
}

function restoreDomGlobals(previousGlobals) {
  for (const [key, value] of previousGlobals.entries()) {
    if (value === undefined) {
      delete globalThis[key];
      continue;
    }

    globalThis[key] = value;
  }
}

module.exports = {
  renderTodoApp,
};
