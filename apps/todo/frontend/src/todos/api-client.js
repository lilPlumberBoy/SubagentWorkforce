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
