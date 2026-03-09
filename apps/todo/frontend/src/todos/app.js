const React = require('react');
const { createRoot } = require('react-dom/client');

const {
  TODO_CLIENT_ERROR_CODES,
  createTodoApiClient,
} = require('./api-client');

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
