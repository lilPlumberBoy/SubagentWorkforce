(() => {
  'use strict';

  const moduleFactories = {
"/browser-entry.js": function(module, exports, require) {

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

},
"/index.js": function(module, exports, require) {
module.exports = {
  ...require('/todos/api-client.js'),
  ...require('/todos/app.js'),
};

},
"/todos/api-client.js": function(module, exports, require) {
const TODO_API_BASE_URL_ENV_VAR = 'TODO_API_BASE_URL';
const TODO_COLLECTION_PATH = '/api/todos';

const TODO_CLIENT_ERROR_CODES = Object.freeze({
  ABORTED: 'ABORTED',
  CONFIGURATION_ERROR: 'CONFIGURATION_ERROR',
  CONFLICT_OR_STALE: 'CONFLICT_OR_STALE',
  INVALID_RESPONSE: 'INVALID_RESPONSE',
  TODO_NOT_FOUND: 'TODO_NOT_FOUND',
  UNAVAILABLE: 'UNAVAILABLE',
  VALIDATION_ERROR: 'VALIDATION_ERROR',
});

const BACKEND_ERROR_CODE_MAP = Object.freeze({
  conflict: TODO_CLIENT_ERROR_CODES.CONFLICT_OR_STALE,
  not_found: TODO_CLIENT_ERROR_CODES.TODO_NOT_FOUND,
  server_error: TODO_CLIENT_ERROR_CODES.UNAVAILABLE,
  validation_error: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
});

/**
 * @typedef {Object} Todo
 * @property {string} id
 * @property {string} title
 * @property {boolean} completed
 * @property {string} createdAt
 * @property {string} updatedAt
 */

/**
 * @typedef {Object} TodoListResult
 * @property {Todo[]} items
 */

/**
 * @typedef {Object} TodoItemResult
 * @property {Todo} item
 */

/**
 * @typedef {Object} TodoDeleteResult
 * @property {string} deletedId
 */

/**
 * @typedef {Object} TodoRequestOptions
 * @property {AbortSignal} [signal]
 */

/**
 * @typedef {Object} TodoApiClientOptions
 * @property {string} [baseUrl]
 * @property {typeof fetch} [fetch]
 */

class TodoApiClientError extends Error {
  constructor(
    message,
    {
      cause,
      code = TODO_CLIENT_ERROR_CODES.UNAVAILABLE,
      fieldErrors,
      isRetryable = false,
      status,
    } = {}
  ) {
    super(message, cause ? { cause } : undefined);
    this.name = 'TodoApiClientError';
    this.code = code;
    this.fieldErrors = fieldErrors;
    this.isRetryable = isRetryable;
    this.status = status;
  }
}

/**
 * @param {TodoApiClientOptions} [options]
 */
function createTodoApiClient(options = {}) {
  const baseUrl = normalizeBaseUrl(
    options.baseUrl ?? process.env[TODO_API_BASE_URL_ENV_VAR]
  );
  const fetchImplementation = resolveFetchImplementation(options.fetch);

  return Object.freeze({
    baseUrl,
    completeTodo,
    createTodo,
    deleteTodo,
    editTodoTitle,
    listTodos,
    setTodoCompleted,
    uncompleteTodo,
    updateTodo,
  });

  /**
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoListResult>}
   */
  async function listTodos(requestOptions = {}) {
    const response = await sendJsonRequest(TODO_COLLECTION_PATH, {
      method: 'GET',
      signal: requestOptions.signal,
    });

    return {
      items: parseTodoArrayResponse(response, 'items'),
    };
  }

  /**
   * @param {{ title: string }} input
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  async function createTodo(input, requestOptions = {}) {
    const title = validateTitleInput(input, 'create');
    const response = await sendJsonRequest(TODO_COLLECTION_PATH, {
      json: { title },
      method: 'POST',
      signal: requestOptions.signal,
    });

    return {
      item: parseTodoMutationResponse(response),
    };
  }

  /**
   * @param {{ id: string, title?: string, completed?: boolean }} input
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  async function updateTodo(input, requestOptions = {}) {
    const { id, payload } = validateUpdateInput(input);
    const response = await sendJsonRequest(
      `${TODO_COLLECTION_PATH}/${encodeURIComponent(id)}`,
      {
        json: payload,
        method: 'PATCH',
        signal: requestOptions.signal,
      }
    );

    return {
      item: parseTodoMutationResponse(response),
    };
  }

  /**
   * @param {{ id: string, title: string }} input
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  function editTodoTitle(input, requestOptions = {}) {
    validateInputObject(input, 'Edit payload must be an object.');

    return updateTodo(
      {
        id: validateTodoId(input.id),
        title: validateTitleString(input.title),
      },
      requestOptions
    );
  }

  /**
   * @param {{ id: string, completed: boolean }} input
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  function setTodoCompleted(input, requestOptions = {}) {
    validateInputObject(input, 'Completion payload must be an object.');

    return updateTodo(
      {
        completed: validateCompletedValue(input.completed),
        id: validateTodoId(input.id),
      },
      requestOptions
    );
  }

  /**
   * @param {string} id
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  function completeTodo(id, requestOptions = {}) {
    return setTodoCompleted(
      { completed: true, id: validateTodoId(id) },
      requestOptions
    );
  }

  /**
   * @param {string} id
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoItemResult>}
   */
  function uncompleteTodo(id, requestOptions = {}) {
    return setTodoCompleted(
      { completed: false, id: validateTodoId(id) },
      requestOptions
    );
  }

  /**
   * @param {{ id: string }} input
   * @param {TodoRequestOptions} [requestOptions]
   * @returns {Promise<TodoDeleteResult>}
   */
  async function deleteTodo(input, requestOptions = {}) {
    validateInputObject(input, 'Delete payload must be an object.');

    const response = await sendJsonRequest(
      `${TODO_COLLECTION_PATH}/${encodeURIComponent(validateTodoId(input.id))}`,
      {
        method: 'DELETE',
        signal: requestOptions.signal,
      }
    );

    return parseDeleteResponse(response);
  }

  async function sendJsonRequest(pathname, options = {}) {
    const requestOptions = {
      headers: {
        accept: 'application/json',
      },
      method: options.method || 'GET',
      signal: options.signal,
    };

    if (Object.prototype.hasOwnProperty.call(options, 'json')) {
      requestOptions.body = JSON.stringify(options.json);
      requestOptions.headers['content-type'] = 'application/json';
    }

    let response;

    try {
      response = await fetchImplementation(
        new URL(pathname, `${baseUrl}/`).toString(),
        requestOptions
      );
    } catch (error) {
      throw normalizeTransportError(error);
    }

    const payload = await parseJsonResponse(response);

    if (!response.ok) {
      throw normalizeErrorResponse(response.status, payload);
    }

    return payload;
  }
}

function resolveFetchImplementation(fetchImplementation) {
  const candidate = fetchImplementation || globalThis.fetch;

  if (typeof candidate !== 'function') {
    throw new TodoApiClientError(
      'A fetch implementation is required to create the todo API client.',
      {
        code: TODO_CLIENT_ERROR_CODES.CONFIGURATION_ERROR,
      }
    );
  }

  return candidate;
}

function normalizeBaseUrl(baseUrl) {
  if (typeof baseUrl !== 'string' || baseUrl.trim().length === 0) {
    throw new TodoApiClientError(
      `Set ${TODO_API_BASE_URL_ENV_VAR} or pass baseUrl when creating the todo API client.`,
      {
        code: TODO_CLIENT_ERROR_CODES.CONFIGURATION_ERROR,
      }
    );
  }

  let normalizedUrl;

  try {
    normalizedUrl = new URL(baseUrl);
  } catch (error) {
    throw new TodoApiClientError('Todo API base URL must be a valid absolute URL.', {
      cause: error,
      code: TODO_CLIENT_ERROR_CODES.CONFIGURATION_ERROR,
    });
  }

  if (!/^https?:$/.test(normalizedUrl.protocol)) {
    throw new TodoApiClientError(
      'Todo API base URL must use the http or https protocol.',
      {
        code: TODO_CLIENT_ERROR_CODES.CONFIGURATION_ERROR,
      }
    );
  }

  normalizedUrl.pathname = normalizedUrl.pathname.replace(/\/+$/, '');

  return normalizedUrl.toString().replace(/\/$/, '');
}

async function parseJsonResponse(response) {
  const rawBody = await response.text();

  if (rawBody.length === 0) {
    return null;
  }

  try {
    return JSON.parse(rawBody);
  } catch (error) {
    throw new TodoApiClientError(
      'Todo API returned invalid JSON.',
      {
        cause: error,
        code: TODO_CLIENT_ERROR_CODES.INVALID_RESPONSE,
        isRetryable: response.status >= 500,
        status: response.status,
      }
    );
  }
}

function normalizeTransportError(error) {
  if (error instanceof TodoApiClientError) {
    return error;
  }

  if (isAbortError(error)) {
    return new TodoApiClientError('Todo request was aborted.', {
      cause: error,
      code: TODO_CLIENT_ERROR_CODES.ABORTED,
    });
  }

  return new TodoApiClientError(
    'Todo API is unavailable.',
    {
      cause: error,
      code: TODO_CLIENT_ERROR_CODES.UNAVAILABLE,
      isRetryable: true,
    }
  );
}

function normalizeErrorResponse(status, payload) {
  const fallbackCode =
    status >= 500
      ? TODO_CLIENT_ERROR_CODES.UNAVAILABLE
      : TODO_CLIENT_ERROR_CODES.INVALID_RESPONSE;

  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return new TodoApiClientError(
      'Todo API returned an invalid error response.',
      {
        code: fallbackCode,
        isRetryable: status >= 500,
        status,
      }
    );
  }

  const errorPayload = payload.error;

  if (
    !errorPayload ||
    typeof errorPayload !== 'object' ||
    Array.isArray(errorPayload) ||
    typeof errorPayload.message !== 'string'
  ) {
    return new TodoApiClientError(
      'Todo API returned an invalid error response.',
      {
        code: fallbackCode,
        isRetryable: status >= 500,
        status,
      }
    );
  }

  return new TodoApiClientError(errorPayload.message, {
    code:
      BACKEND_ERROR_CODE_MAP[errorPayload.code] ||
      (status >= 500
        ? TODO_CLIENT_ERROR_CODES.UNAVAILABLE
        : TODO_CLIENT_ERROR_CODES.INVALID_RESPONSE),
    fieldErrors: normalizeFieldErrors(errorPayload.fieldErrors),
    isRetryable: status >= 500,
    status,
  });
}

function parseTodoArrayResponse(payload, key) {
  validateResponseObject(payload, 'Todo API returned an invalid response body.');

  if (!Array.isArray(payload[key])) {
    throw invalidResponseError(`Todo API response is missing "${key}".`);
  }

  return payload[key].map((todo) => normalizeTodo(todo));
}

function parseTodoMutationResponse(payload) {
  validateResponseObject(payload, 'Todo API returned an invalid response body.');

  if (!Object.prototype.hasOwnProperty.call(payload, 'todo')) {
    throw invalidResponseError('Todo API response is missing "todo".');
  }

  return normalizeTodo(payload.todo);
}

function parseDeleteResponse(payload) {
  validateResponseObject(payload, 'Todo API returned an invalid response body.');

  if (typeof payload.deletedId !== 'string' || payload.deletedId.length === 0) {
    throw invalidResponseError('Todo API response is missing "deletedId".');
  }

  return {
    deletedId: payload.deletedId,
  };
}

function normalizeTodo(todo) {
  validateResponseObject(todo, 'Todo API returned an invalid todo item.');

  if (typeof todo.id !== 'string' || todo.id.length === 0) {
    throw invalidResponseError('Todo item is missing a valid "id".');
  }

  if (typeof todo.title !== 'string') {
    throw invalidResponseError('Todo item is missing a valid "title".');
  }

  if (typeof todo.completed !== 'boolean') {
    throw invalidResponseError('Todo item is missing a valid "completed" flag.');
  }

  if (typeof todo.createdAt !== 'string' || todo.createdAt.length === 0) {
    throw invalidResponseError('Todo item is missing a valid "createdAt" timestamp.');
  }

  if (typeof todo.updatedAt !== 'string' || todo.updatedAt.length === 0) {
    throw invalidResponseError('Todo item is missing a valid "updatedAt" timestamp.');
  }

  return {
    completed: todo.completed,
    createdAt: todo.createdAt,
    id: todo.id,
    title: todo.title,
    updatedAt: todo.updatedAt,
  };
}

function invalidResponseError(message) {
  return new TodoApiClientError(message, {
    code: TODO_CLIENT_ERROR_CODES.INVALID_RESPONSE,
  });
}

function normalizeFieldErrors(fieldErrors) {
  if (
    fieldErrors === undefined ||
    fieldErrors === null ||
    typeof fieldErrors !== 'object' ||
    Array.isArray(fieldErrors)
  ) {
    return undefined;
  }

  const normalized = {};

  for (const [field, messages] of Object.entries(fieldErrors)) {
    if (Array.isArray(messages) && messages.every((message) => typeof message === 'string')) {
      normalized[field] = [...messages];
    }
  }

  return Object.keys(normalized).length > 0 ? normalized : undefined;
}

function validateUpdateInput(input) {
  validateInputObject(input, 'Update payload must be an object.');

  const id = validateTodoId(input.id);
  const payload = {};

  if (Object.prototype.hasOwnProperty.call(input, 'title')) {
    payload.title = validateTitleString(input.title);
  }

  if (Object.prototype.hasOwnProperty.call(input, 'completed')) {
    payload.completed = validateCompletedValue(input.completed);
  }

  if (Object.keys(payload).length === 0) {
    throw new TodoApiClientError(
      'Update payload must include at least one supported field.',
      {
        code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      }
    );
  }

  return { id, payload };
}

function validateTitleInput(input, mode) {
  validateInputObject(input, `${capitalize(mode)} payload must be an object.`);

  if (!Object.prototype.hasOwnProperty.call(input, 'title')) {
    throw new TodoApiClientError('Title is required.', {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      fieldErrors: { title: ['Title is required.'] },
    });
  }

  return validateTitleString(input.title);
}

function validateTitleString(title) {
  if (typeof title !== 'string') {
    throw new TodoApiClientError('Title must be a string.', {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      fieldErrors: { title: ['Title must be a string.'] },
    });
  }

  if (title.trim().length === 0) {
    throw new TodoApiClientError('Title is required.', {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      fieldErrors: { title: ['Title is required.'] },
    });
  }

  return title;
}

function validateCompletedValue(completed) {
  if (typeof completed !== 'boolean') {
    throw new TodoApiClientError('Completed must be a boolean.', {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      fieldErrors: { completed: ['Completed must be a boolean.'] },
    });
  }

  return completed;
}

function validateTodoId(id) {
  if (typeof id !== 'string' || id.length === 0) {
    throw new TodoApiClientError('Todo id is required.', {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
      fieldErrors: { id: ['Todo id is required.'] },
    });
  }

  return id;
}

function validateInputObject(value, message) {
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new TodoApiClientError(message, {
      code: TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR,
    });
  }
}

function validateResponseObject(value, message) {
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw invalidResponseError(message);
  }
}

function isAbortError(error) {
  return Boolean(error && typeof error === 'object' && error.name === 'AbortError');
}

function capitalize(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

module.exports = {
  TODO_API_BASE_URL_ENV_VAR,
  TODO_CLIENT_ERROR_CODES,
  TODO_COLLECTION_PATH,
  TodoApiClientError,
  createTodoApiClient,
};

},
"/todos/app.js": function(module, exports, require) {
const React = require('react');
const { createRoot } = require('react-dom/client');

const {
  TODO_CLIENT_ERROR_CODES,
  createTodoApiClient,
} = require('/todos/api-client.js');

function TodoApp({ client: providedClient }) {
  const client = React.useMemo(
    () => providedClient || createTodoApiClient(),
    [providedClient]
  );
  const createInputRef = React.useRef(null);
  const createRequestInFlightRef = React.useRef(false);
  const editRequestInFlightRef = React.useRef(false);
  const editInputRef = React.useRef(null);
  const actionButtonRefs = React.useRef(new Map());
  const pendingRowActionFocusRef = React.useRef(null);
  const rowRequestInFlightRef = React.useRef(new Set());
  const [todos, setTodos] = React.useState([]);
  const [loadState, setLoadState] = React.useState({
    errorMessage: '',
    status: 'loading',
  });
  const [createState, setCreateState] = React.useState({
    errorMessage: '',
    fieldError: '',
    status: 'idle',
  });
  const [createDraft, setCreateDraft] = React.useState('');
  const [editingSession, setEditingSession] = React.useState(null);
  const [rowStateById, setRowStateById] = React.useState({});
  const [pageMessage, setPageMessage] = React.useState('');

  React.useEffect(() => {
    const abortController = new AbortController();

    void loadTodos({
      signal: abortController.signal,
      showLoading: true,
    });

    return () => {
      abortController.abort();
    };
  }, [client]);

  React.useEffect(() => {
    if (!editingSession || editingSession.status === 'saving') {
      return;
    }

    try {
      editInputRef.current?.focus();
      editInputRef.current?.select();
    } catch {
      // JSDOM focus support is partial; runtime browsers still receive autofocus.
    }
  }, [editingSession]);

  async function loadTodos({ focusCreateInput = false, signal, showLoading = false } = {}) {
    if (showLoading) {
      setLoadState({
        errorMessage: '',
        status: 'loading',
      });
    } else {
      setLoadState((currentState) => ({
        ...currentState,
        errorMessage: '',
      }));
    }

    try {
      const result = await client.listTodos({ signal });

      if (signal?.aborted) {
        return;
      }

      setTodos(result.items);
      setLoadState({
        errorMessage: '',
        status: 'success',
      });

      if (focusCreateInput) {
        queueFocus(createInputRef.current);
      }
    } catch (error) {
      if (signal?.aborted || error.code === TODO_CLIENT_ERROR_CODES.ABORTED) {
        return;
      }

      setLoadState({
        errorMessage: toDisplayMessage(
          error,
          'The todo list could not be loaded.'
        ),
        status: 'error',
      });
    }
  }

  async function handleCreateSubmit(event) {
    event.preventDefault();

    if (createRequestInFlightRef.current) {
      return;
    }

    createRequestInFlightRef.current = true;
    setCreateState({
      errorMessage: '',
      fieldError: '',
      status: 'submitting',
    });
    setPageMessage('');

    try {
      const result = await client.createTodo({ title: createDraft });

      setTodos((currentTodos) => [...currentTodos, result.item]);
      setCreateDraft('');
      setCreateState({
        errorMessage: '',
        fieldError: '',
        status: 'idle',
      });
      queueFocus(createInputRef.current);
    } catch (error) {
      setCreateState({
        errorMessage:
          error.code === TODO_CLIENT_ERROR_CODES.VALIDATION_ERROR
            ? error.fieldErrors?.title?.[0] || ''
            : toDisplayMessage(error, 'The todo could not be created.'),
        fieldError: error.fieldErrors?.title?.[0] || '',
        status: 'idle',
      });
    } finally {
      createRequestInFlightRef.current = false;
    }
  }

  function beginEdit(todo) {
    if (editingSession?.status === 'saving') {
      return;
    }

    setPageMessage('');
    setEditingSession({
      draft: todo.title,
      errorMessage: '',
      fieldError: '',
      focusAction: 'edit',
      id: todo.id,
      status: 'idle',
    });
  }

  function cancelEdit() {
    if (!editingSession || editingSession.status === 'saving') {
      return;
    }

    const todoId = editingSession.id;

    setEditingSession(null);
    focusRowAction(todoId, 'edit');
  }

  async function handleEditSubmit(event) {
    event.preventDefault();

    if (!editingSession || editRequestInFlightRef.current) {
      return;
    }

    const todoId = editingSession.id;
    const nextTitle = editingSession.draft;

    editRequestInFlightRef.current = true;
    setEditingSession((currentSession) => ({
      ...currentSession,
      errorMessage: '',
      fieldError: '',
      status: 'saving',
    }));
    setPageMessage('');

    try {
      const result = await client.editTodoTitle({
        id: todoId,
        title: nextTitle,
      });

      setTodos((currentTodos) => replaceTodo(currentTodos, result.item));
      setEditingSession(null);
      focusRowAction(todoId, 'edit');
    } catch (error) {
      if (isStaleMutationError(error)) {
        setEditingSession(null);
        setPageMessage('That todo was refreshed because it no longer matched persisted state.');
        await loadTodos();
        return;
      }

      setEditingSession((currentSession) => ({
        ...currentSession,
        errorMessage: toDisplayMessage(error, 'The todo could not be updated.'),
        fieldError: error.fieldErrors?.title?.[0] || '',
        status: 'idle',
      }));
    } finally {
      editRequestInFlightRef.current = false;
    }
  }

  async function handleToggle(todoId, completed) {
    if (!beginRowRequest(todoId)) {
      return;
    }

    setPageMessage('');
    setRowStateById((currentState) => ({
      ...currentState,
      [todoId]: {
        errorMessage: '',
        status: 'toggling',
      },
    }));

    try {
      const result = await client.setTodoCompleted({
        completed,
        id: todoId,
      });

      setTodos((currentTodos) => replaceTodo(currentTodos, result.item));
      clearRowState(todoId);
    } catch (error) {
      if (isStaleMutationError(error)) {
        clearRowState(todoId);
        setPageMessage('That todo was refreshed because it no longer matched persisted state.');
        await loadTodos();
        return;
      }

      setRowStateById((currentState) => ({
        ...currentState,
        [todoId]: {
          errorMessage: toDisplayMessage(error, 'The todo could not be updated.'),
          status: 'idle',
        },
      }));
    } finally {
      finishRowRequest(todoId);
    }
  }

  async function handleDelete(todoId) {
    if (!beginRowRequest(todoId)) {
      return;
    }

    setPageMessage('');
    setRowStateById((currentState) => ({
      ...currentState,
      [todoId]: {
        errorMessage: '',
        status: 'deleting',
      },
    }));

    try {
      const result = await client.deleteTodo({ id: todoId });

      setTodos((currentTodos) => {
        const nextTodos = currentTodos.filter((todo) => todo.id !== result.deletedId);
        queueDeleteFocus({
          currentTodos,
          deletedId: result.deletedId,
          nextTodos,
        });

        return nextTodos;
      });
      clearRowState(todoId);
      setEditingSession((currentSession) =>
        currentSession?.id === todoId ? null : currentSession
      );
    } catch (error) {
      if (isStaleMutationError(error)) {
        clearRowState(todoId);
        setPageMessage('That todo was refreshed because it no longer matched persisted state.');
        await loadTodos();
        return;
      }

      setRowStateById((currentState) => ({
        ...currentState,
        [todoId]: {
          errorMessage: toDisplayMessage(error, 'The todo could not be deleted.'),
          status: 'idle',
        },
      }));
    } finally {
      finishRowRequest(todoId);
    }
  }

  function clearRowState(todoId) {
    setRowStateById((currentState) => {
      if (!Object.prototype.hasOwnProperty.call(currentState, todoId)) {
        return currentState;
      }

      const nextState = { ...currentState };
      delete nextState[todoId];
      return nextState;
    });
  }

  function beginRowRequest(todoId) {
    if (rowRequestInFlightRef.current.has(todoId)) {
      return false;
    }

    rowRequestInFlightRef.current.add(todoId);
    return true;
  }

  function finishRowRequest(todoId) {
    rowRequestInFlightRef.current.delete(todoId);
  }

  function queueDeleteFocus({ currentTodos, deletedId, nextTodos }) {
    if (nextTodos.length === 0) {
      queueFocus(createInputRef.current);
      return;
    }

    const deletedIndex = currentTodos.findIndex((todo) => todo.id === deletedId);
    const nextFocusIndex =
      deletedIndex >= 0
        ? Math.min(deletedIndex, nextTodos.length - 1)
        : 0;

    queueFocus(actionButtonRefs.current.get(`${nextTodos[nextFocusIndex].id}:edit`));
  }

  const collectionContent = renderCollectionState({
    editInputRef,
    editingSession,
    loadState,
    onCancelEdit: cancelEdit,
    onDelete: handleDelete,
    onEditDraftChange: (draft) =>
      setEditingSession((currentSession) => ({
        ...currentSession,
        draft,
        errorMessage: '',
        fieldError: '',
      })),
    onEditSubmit: handleEditSubmit,
    onRetry: () => {
      void loadTodos({
        focusCreateInput: true,
        showLoading: true,
      });
    },
    onStartEdit: beginEdit,
    onToggle: handleToggle,
    registerActionButton: (todoId, action, element) => {
      const actionKey = `${todoId}:${action}`;

      if (element) {
        actionButtonRefs.current.set(actionKey, element);

        if (pendingRowActionFocusRef.current === actionKey) {
          pendingRowActionFocusRef.current = null;
          queueFocus(element);
        }

        return;
      }

      actionButtonRefs.current.delete(actionKey);
    },
    rowStateById,
    todos,
  });

  return React.createElement(
    'main',
    {
      'aria-busy': loadState.status === 'loading' ? 'true' : undefined,
      className: 'todo-page',
    },
    [
      React.createElement(
        'header',
        {
          className: 'todo-header',
          key: 'header',
        },
        [
          React.createElement(
            'h1',
            { key: 'title' },
            'Todos'
          ),
          React.createElement(
            'p',
            {
              key: 'copy',
            },
            'Create, edit, complete, and delete persisted todos.'
          ),
        ]
      ),
      React.createElement(
        'form',
        {
          'aria-label': 'Create todo',
          className: 'todo-create-form',
          key: 'create-form',
          onSubmit: handleCreateSubmit,
        },
        [
          React.createElement(
            'label',
            {
              htmlFor: 'todo-create-input',
              key: 'create-label',
            },
            'New todo'
          ),
          React.createElement('input', {
            'aria-describedby': createState.fieldError
              ? 'todo-create-error'
              : undefined,
            disabled: createState.status === 'submitting',
            id: 'todo-create-input',
            key: 'create-input',
            onInput: (event) => setCreateDraft(event.target.value),
            onChange: (event) => setCreateDraft(event.target.value),
            ref: createInputRef,
            type: 'text',
            value: createDraft,
          }),
          createState.fieldError
            ? React.createElement(
                'p',
                {
                  id: 'todo-create-error',
                  key: 'create-field-error',
                  role: 'alert',
                },
                createState.fieldError
              )
            : null,
          createState.errorMessage && !createState.fieldError
            ? React.createElement(
                'p',
                {
                  key: 'create-error',
                  role: 'alert',
                },
                createState.errorMessage
              )
            : null,
          React.createElement(
            'button',
            {
              disabled: createState.status === 'submitting',
              key: 'create-button',
              type: 'submit',
            },
            createState.status === 'submitting' ? 'Adding…' : 'Add'
          ),
        ]
      ),
      pageMessage
        ? React.createElement(
            'p',
            {
              'aria-live': 'polite',
              key: 'page-message',
            },
            pageMessage
          )
        : null,
      React.createElement(
        'section',
        {
          'aria-label': 'Todo list',
          className: 'todo-list-region',
          key: 'collection',
        },
        collectionContent
      ),
    ]
  );

  function focusRowAction(todoId, action) {
    const actionKey = `${todoId}:${action}`;
    const element = actionButtonRefs.current.get(actionKey);

    if (element) {
      pendingRowActionFocusRef.current = null;
      queueFocus(element);
      return;
    }

    pendingRowActionFocusRef.current = actionKey;
  }
}

function renderCollectionState({
  editInputRef,
  editingSession,
  loadState,
  onCancelEdit,
  onDelete,
  onEditDraftChange,
  onEditSubmit,
  onRetry,
  onStartEdit,
  onToggle,
  registerActionButton,
  rowStateById,
  todos,
}) {
  if (loadState.status === 'loading') {
    return React.createElement(
      'p',
      {
        key: 'loading',
      },
      'Loading todos…'
    );
  }

  if (loadState.status === 'error') {
    return React.createElement(
      'div',
      {
        key: 'load-error',
      },
      [
        React.createElement(
          'p',
          {
            key: 'load-error-text',
            role: 'alert',
          },
          loadState.errorMessage
        ),
        React.createElement(
          'button',
          {
            key: 'retry-button',
            onClick: onRetry,
            type: 'button',
          },
          'Retry'
        ),
      ]
    );
  }

  if (todos.length === 0) {
    return React.createElement(
      'p',
      {
        key: 'empty',
      },
      'No todos yet.'
    );
  }

  return React.createElement(
    'ul',
    {
      key: 'list',
    },
    todos.map((todo) => {
      const rowState = rowStateById[todo.id] || {
        errorMessage: '',
        status: 'idle',
      };
      const isEditing = editingSession?.id === todo.id;
      const isRowBusy =
        rowState.status === 'toggling' ||
        rowState.status === 'deleting' ||
        (isEditing && editingSession.status === 'saving');

      return React.createElement(
        'li',
        {
          key: todo.id,
        },
        [
          isEditing
            ? React.createElement(
                'form',
                {
                  'aria-label': `Edit ${todo.title}`,
                  key: 'edit-form',
                  onSubmit: onEditSubmit,
                },
                [
                  React.createElement('label', {
                    className: 'sr-only',
                    htmlFor: `todo-edit-input-${todo.id}`,
                    key: 'edit-label',
                  }, 'Edit todo'),
                  React.createElement('input', {
                    'aria-describedby': editingSession.fieldError
                      ? `todo-edit-error-${todo.id}`
                      : undefined,
                    autoFocus: true,
                    disabled: editingSession.status === 'saving',
                    id: `todo-edit-input-${todo.id}`,
                    key: 'edit-input',
                    onInput: (event) => onEditDraftChange(event.target.value),
                    onChange: (event) => onEditDraftChange(event.target.value),
                    onKeyDown: (event) => {
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        onCancelEdit();
                      }
                    },
                    ref: editInputRef,
                    type: 'text',
                    value: editingSession.draft,
                  }),
                  editingSession.fieldError
                    ? React.createElement(
                        'p',
                        {
                          id: `todo-edit-error-${todo.id}`,
                          key: 'edit-field-error',
                          role: 'alert',
                        },
                        editingSession.fieldError
                      )
                    : null,
                  editingSession.errorMessage && !editingSession.fieldError
                    ? React.createElement(
                        'p',
                        {
                          key: 'edit-error',
                          role: 'alert',
                        },
                        editingSession.errorMessage
                      )
                    : null,
                  React.createElement(
                    'button',
                    {
                      disabled: editingSession.status === 'saving',
                      key: 'save',
                      type: 'submit',
                    },
                    editingSession.status === 'saving' ? 'Saving…' : 'Save'
                  ),
                  React.createElement(
                    'button',
                    {
                      disabled: editingSession.status === 'saving',
                      key: 'cancel',
                      onClick: (event) => {
                        event.preventDefault();
                        onCancelEdit();
                      },
                      type: 'button',
                    },
                    'Cancel'
                  ),
                ]
              )
            : React.createElement(
                'div',
                {
                  key: 'display-row',
                },
                [
                  React.createElement(
                    'label',
                    {
                      key: 'toggle-label',
                    },
                    [
                      React.createElement('input', {
                        checked: todo.completed,
                        disabled: isRowBusy,
                        key: 'toggle',
                        onChange: (event) => {
                          void onToggle(todo.id, event.target.checked);
                        },
                        type: 'checkbox',
                      }),
                      React.createElement(
                        'span',
                        {
                          key: 'title',
                          style: todo.completed
                            ? { opacity: 0.7, textDecoration: 'line-through' }
                            : undefined,
                        },
                        todo.title
                      ),
                    ]
                  ),
                  React.createElement(
                    'button',
                    {
                      disabled: isRowBusy,
                      'aria-label': `Edit ${todo.title}`,
                      key: 'edit',
                      onClick: () => onStartEdit(todo),
                      ref: (element) => registerActionButton(todo.id, 'edit', element),
                      type: 'button',
                    },
                    'Edit'
                  ),
                  React.createElement(
                    'button',
                    {
                      disabled: isRowBusy,
                      'aria-label': `Delete ${todo.title}`,
                      key: 'delete',
                      onClick: () => {
                        void onDelete(todo.id);
                      },
                      ref: (element) => registerActionButton(todo.id, 'delete', element),
                      type: 'button',
                    },
                    rowState.status === 'deleting' ? 'Deleting…' : 'Delete'
                  ),
                ]
              ),
          rowState.status === 'toggling'
            ? React.createElement(
                'p',
                {
                  'aria-live': 'polite',
                  key: 'row-status',
                },
                'Saving…'
              )
            : null,
          rowState.errorMessage
            ? React.createElement(
                'p',
                {
                  key: 'row-error',
                  role: 'alert',
                },
                rowState.errorMessage
              )
            : null,
        ]
      );
    })
  );
}

function createTodoAppRoot(container, options = {}) {
  if (!container || typeof container !== 'object') {
    throw new TypeError('A container element is required to mount the todo app.');
  }

  const root = createRoot(container);

  root.render(
    React.createElement(TodoApp, {
      client: options.client,
    })
  );

  return {
    root,
    unmount() {
      root.unmount();
    },
  };
}

function queueFocus(element) {
  if (!element || typeof element.focus !== 'function') {
    return;
  }

  setTimeout(() => {
    try {
      element.focus();
    } catch {
      // Ignore focus failures in non-browser environments.
    }
  }, 0);
}

function isStaleMutationError(error) {
  return (
    error?.code === TODO_CLIENT_ERROR_CODES.TODO_NOT_FOUND ||
    error?.code === TODO_CLIENT_ERROR_CODES.CONFLICT_OR_STALE
  );
}

function toDisplayMessage(error, fallbackMessage) {
  if (error && typeof error.message === 'string' && error.message.length > 0) {
    return error.message;
  }

  return fallbackMessage;
}

function replaceTodo(todos, nextTodo) {
  return todos.map((todo) => (todo.id === nextTodo.id ? nextTodo : todo));
}

module.exports = {
  TodoApp,
  createTodoAppRoot,
};

}
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
})();