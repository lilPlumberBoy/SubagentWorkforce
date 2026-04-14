const React = require('react');
const { createRoot } = require('react-dom/client');

const { createMonitorApiClient } = require('./api-client');

const h = React.createElement;
const RUN_LIST_REFRESH_MS = 10000;
const DASHBOARD_REFRESH_MS = 5000;
const HISTORY_GROUPS_PER_PAGE = 10;
const DEFAULT_DETAIL_RAIL_WIDTH = 420;
const MIN_DETAIL_RAIL_WIDTH = 320;
const MAX_DETAIL_RAIL_WIDTH = 760;

function MonitorApp({ client: providedClient, initialRunId: providedInitialRunId }) {
  const client = React.useMemo(
    () => providedClient || createMonitorApiClient(),
    [providedClient]
  );
  const openPath = React.useCallback(
    async (path) => {
      try {
        await client.openFile(path);
      } catch (error) {
        const message = toDisplayMessage(error, 'The file could not be opened.');
        if (typeof window !== 'undefined' && typeof window.alert === 'function') {
          window.alert(message);
          return;
        }
        throw error;
      }
    },
    [client]
  );
  const initialRunIdRef = React.useRef(readInitialRunId(providedInitialRunId));
  const [runsState, setRunsState] = React.useState({
      errorMessage: '',
      items: [],
      status: 'loading',
  });
  const [selectedRunId, setSelectedRunId] = React.useState(initialRunIdRef.current);
  const [dashboardState, setDashboardState] = React.useState({
    errorMessage: '',
    payload: null,
    status: 'idle',
  });
  const [selectedActivityId, setSelectedActivityId] = React.useState(null);
  const [selectedActivityVariant, setSelectedActivityVariant] = React.useState('current');
  const [detailState, setDetailState] = React.useState({
    errorMessage: '',
    payload: null,
    promptDebug: null,
    status: 'idle',
  });
  const [isRunStatusExpanded, setIsRunStatusExpanded] = React.useState(false);
  const [isSelectedActivityCollapsed, setIsSelectedActivityCollapsed] =
    React.useState(false);
  const [historyPage, setHistoryPage] = React.useState(1);
  const [historySelectedPhase, setHistorySelectedPhase] = React.useState('all');
  const [detailRailWidth, setDetailRailWidth] = React.useState(
    DEFAULT_DETAIL_RAIL_WIDTH
  );

  const handleDetailRailResizeStart = React.useCallback((event) => {
    if (
      !globalThis.window ||
      typeof window.addEventListener !== 'function' ||
      typeof window.removeEventListener !== 'function'
    ) {
      return;
    }

    event.preventDefault();

    const updateWidth = (clientX) => {
      const viewportWidth = window.innerWidth || 0;
      const nextWidth = clampDetailRailWidth(viewportWidth - clientX - 24, viewportWidth);
      setDetailRailWidth(nextWidth);
    };

    const handlePointerMove = (moveEvent) => {
      updateWidth(moveEvent.clientX);
    };

    const handlePointerUp = () => {
      document.body.style.removeProperty('cursor');
      document.body.style.removeProperty('user-select');
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
    };

    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    updateWidth(event.clientX);
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
  }, []);

  React.useEffect(() => {
    let disposed = false;
    let intervalId = null;
    let inFlight = false;
    let activeController = null;

    const load = async () => {
      if (inFlight) {
        return;
      }
      inFlight = true;
      activeController = new AbortController();
      try {
        const payload = await client.listRuns({ signal: activeController.signal });

        if (disposed) {
          return;
        }

        setRunsState({
          errorMessage: '',
          items: payload.runs || [],
          status: 'success',
        });
        setSelectedRunId((currentRunId) => {
          if (payload.runs.length === 0) {
            syncRunQueryParam(null);
            return null;
          }
          const requestedRunId =
            currentRunId || initialRunIdRef.current || readRunIdFromUrl();
          if (requestedRunId) {
            const matchingRun = payload.runs.find(
              (item) => item.run_id === requestedRunId
            );
            if (matchingRun) {
              syncRunQueryParam(matchingRun.run_id);
              return matchingRun.run_id;
            }
          }
          syncRunQueryParam(payload.runs[0].run_id);
          return payload.runs[0].run_id;
        });
      } catch (error) {
        if (activeController && activeController.signal.aborted) {
          return;
        }
        if (disposed) {
          return;
        }
        setRunsState({
          errorMessage: toDisplayMessage(error, 'Run list could not be loaded.'),
          items: [],
          status: 'error',
        });
      } finally {
        inFlight = false;
        activeController = null;
      }
    };

    void load();
    intervalId = setInterval(() => {
      void load();
    }, RUN_LIST_REFRESH_MS);

    return () => {
      disposed = true;
      clearInterval(intervalId);
      if (activeController) {
        activeController.abort();
      }
    };
  }, [client]);

  React.useEffect(() => {
    syncRunQueryParam(selectedRunId);
  }, [selectedRunId]);

  React.useEffect(() => {
    if (!selectedRunId) {
      setDashboardState({
        errorMessage: '',
        payload: null,
        status: runsState.status === 'loading' ? 'loading' : 'idle',
      });
      setSelectedActivityId(null);
      setSelectedActivityVariant('current');
      return;
    }

    let disposed = false;
    let intervalId = null;
    let inFlight = false;
    let activeController = null;

    const load = async (showLoading) => {
      if (inFlight) {
        return;
      }
      inFlight = true;
      activeController = new AbortController();
      if (showLoading) {
        setDashboardState((currentState) => ({
          ...currentState,
          errorMessage: '',
          status: 'loading',
        }));
      }

      try {
        const payload = await client.getRunDashboard(selectedRunId, {
          limit: 30,
          signal: activeController.signal,
        });

        if (disposed) {
          return;
        }

        setDashboardState({
          errorMessage: '',
          payload,
          status: 'success',
        });
        setSelectedActivityId((currentActivityId) => {
          if (!currentActivityId) {
            return null;
          }
          if (isActivityStillSelectable(payload, currentActivityId)) {
            return currentActivityId;
          }
          setSelectedActivityVariant('current');
          return null;
        });
      } catch (error) {
        if (activeController && activeController.signal.aborted) {
          return;
        }
        if (disposed) {
          return;
        }
        setDashboardState({
          errorMessage: toDisplayMessage(
            error,
            'Run dashboard could not be loaded.'
          ),
          payload: null,
          status: 'error',
        });
      } finally {
        inFlight = false;
        activeController = null;
      }
    };

    void load(true);
    intervalId = setInterval(() => {
      void load(false);
    }, DASHBOARD_REFRESH_MS);

    return () => {
      disposed = true;
      clearInterval(intervalId);
      if (activeController) {
        activeController.abort();
      }
    };
  }, [client, runsState.status, selectedRunId]);

  React.useEffect(() => {
    if (!selectedRunId || !selectedActivityId) {
      setDetailState({
        errorMessage: '',
        payload: null,
        promptDebug: null,
        status: 'idle',
      });
      return;
    }

    let disposed = false;
    let intervalId = null;
    let inFlight = false;
    let activeDetailController = null;

    const loadDetail = async (showLoading) => {
      if (inFlight) {
        return;
      }
      inFlight = true;
      activeDetailController = new AbortController();
      if (showLoading) {
        setDetailState((currentState) => ({
          errorMessage: '',
          payload: currentState.promptDebug ? currentState.payload : null,
          promptDebug: currentState.promptDebug,
          status: 'loading',
        }));
      }

      try {
        const payload = await client.getActivityDetail(selectedRunId, selectedActivityId, {
          limit: 10,
          signal: activeDetailController.signal,
        });

        if (disposed) {
          return;
        }

        setDetailState((currentState) => ({
          errorMessage: '',
          payload,
          promptDebug: currentState.promptDebug,
          status: currentState.promptDebug ? 'success' : currentState.status,
        }));
      } catch (error) {
        if (activeDetailController && activeDetailController.signal.aborted) {
          return;
        }
        if (disposed) {
          return;
        }
        setDetailState({
          errorMessage: toDisplayMessage(
            error,
            'Activity detail could not be loaded.'
          ),
          payload: null,
          promptDebug: null,
          status: 'error',
        });
      } finally {
        inFlight = false;
        activeDetailController = null;
      }
    };

    void loadDetail(true);
    intervalId = setInterval(() => {
      void loadDetail(false);
    }, DASHBOARD_REFRESH_MS);

    return () => {
      disposed = true;
      clearInterval(intervalId);
      if (activeDetailController) {
        activeDetailController.abort();
      }
    };
  }, [client, selectedActivityId, selectedActivityVariant, selectedRunId]);

  React.useEffect(() => {
    if (!selectedRunId || !selectedActivityId) {
      return undefined;
    }

    let disposed = false;
    const controller = new AbortController();

    setDetailState((currentState) => ({
      errorMessage: '',
      payload: currentState.payload,
      promptDebug: null,
      status: currentState.payload ? 'loading' : 'loading',
    }));

    const loadPromptDebug = async () => {
      try {
        const promptDebug = await client.getPromptDebug(selectedRunId, selectedActivityId, {
          signal: controller.signal,
          variant: selectedActivityVariant,
        });

        if (disposed) {
          return;
        }

        setDetailState((currentState) => ({
          errorMessage: '',
          payload: currentState.payload,
          promptDebug,
          status: currentState.payload ? 'success' : currentState.status,
        }));
      } catch (error) {
        if (controller.signal.aborted || disposed) {
          return;
        }
        setDetailState({
          errorMessage: toDisplayMessage(
            error,
            'Activity prompt detail could not be loaded.'
          ),
          payload: null,
          promptDebug: null,
          status: 'error',
        });
      }
    };

    void loadPromptDebug();

    return () => {
      disposed = true;
      controller.abort();
    };
  }, [client, selectedActivityId, selectedActivityVariant, selectedRunId]);

  React.useEffect(() => {
    if (selectedActivityId) {
      setIsSelectedActivityCollapsed(false);
    }
  }, [selectedActivityId, selectedActivityVariant]);

  React.useEffect(() => {
    setHistoryPage(1);
    setHistorySelectedPhase('all');
  }, [selectedRunId]);

  const dashboard = dashboardState.payload;
  const selectedRun =
    runsState.items.find((item) => item.run_id === selectedRunId) || null;

  return h(
    'main',
    {
      className: 'monitor-page',
    },
    [
      h(
        'aside',
        {
          className: 'monitor-sidebar',
          key: 'sidebar',
        },
        [
          h(
            'button',
            {
              'aria-label': 'Open run monitor sidebar',
              className: 'sidebar-hover-trigger',
              key: 'sidebar-trigger',
              type: 'button',
            },
            [
              h('span', { className: 'sidebar-hover-trigger-bar', key: 'bar-1' }),
              h('span', { className: 'sidebar-hover-trigger-bar', key: 'bar-2' }),
              h('span', { className: 'sidebar-hover-trigger-bar', key: 'bar-3' }),
            ]
          ),
          h(
            'div',
            {
              className: 'monitor-sidebar-panel',
              key: 'sidebar-panel',
            },
            [
              h(
                'div',
                {
                  className: 'sidebar-header',
                  key: 'sidebar-header',
                },
                [
                  h('p', { className: 'eyebrow', key: 'eyebrow' }, 'Subagent Workforce'),
                  h('h1', { key: 'title' }, 'Run Monitor'),
                  h(
                    'p',
                    { className: 'sidebar-copy', key: 'copy' },
                    'Read-only browser view over the live orchestration state.'
                  ),
                ]
              ),
              runsState.status === 'error'
                ? h(
                    'p',
                    {
                      className: 'inline-error',
                      key: 'runs-error',
                      role: 'alert',
                    },
                    runsState.errorMessage
                  )
                : null,
              h(
                'section',
                {
                  className: 'run-list',
                  key: 'run-list',
                },
                [
                  h('h2', { key: 'heading' }, 'Runs'),
                  runsState.status === 'loading'
                    ? h('p', { key: 'loading' }, 'Loading runs…')
                    : runsState.items.length === 0
                    ? h('p', { key: 'empty' }, 'No runs detected.')
                    : h(
                        'div',
                        {
                          className: 'run-list-items',
                          key: 'items',
                        },
                        runsState.items.map((item) =>
                          h(
                            'button',
                            {
                              'aria-label': `Open run ${item.run_id}`,
                              className:
                                item.run_id === selectedRunId
                                  ? 'run-list-item is-selected'
                                  : 'run-list-item',
                              key: item.run_id,
                              onClick: () => {
                                setSelectedRunId(item.run_id);
                                setSelectedActivityId(null);
                                setSelectedActivityVariant('current');
                              },
                              type: 'button',
                            },
                            [
                              h(
                                'strong',
                                { className: 'run-list-title', key: 'title' },
                                item.run_id
                              ),
                              h(
                                'span',
                                { className: 'run-list-meta', key: 'phase' },
                                `${item.current_phase} · ${item.run_status}`
                              ),
                              h(
                                'span',
                                { className: 'run-list-meta', key: 'started' },
                                `started ${item.started_at || 'unknown'}`
                              ),
                              h(
                                'span',
                                { className: 'run-list-meta', key: 'reason' },
                                item.run_status_reason
                              ),
                              h(
                                'span',
                                { className: 'run-list-meta', key: 'counts' },
                                `active ${item.active_activity_count} · queued ${item.queued_activity_count}`
                              ),
                            ]
                          )
                        )
                      ),
                ]
              ),
            ]
          ),
        ]
      ),
      h(
        'section',
        {
          className: 'monitor-content',
          key: 'content',
        },
        renderContent({
          dashboard,
          dashboardState,
          detailRailWidth,
          detailState,
        handleDetailRailResizeStart,
        historyPage,
        historySelectedPhase,
        isRunStatusExpanded,
        isSelectedActivityCollapsed,
        openPath,
          selectedActivityId,
          selectedActivityVariant,
          selectedRun,
          selectedRunId,
        setHistoryPage,
        setHistorySelectedPhase,
        setIsRunStatusExpanded,
        setIsSelectedActivityCollapsed,
        setSelectedActivityId,
          setSelectedActivityVariant,
        })
      ),
    ]
  );
}

function renderContent({
  dashboard,
  dashboardState,
  detailRailWidth,
  detailState,
  handleDetailRailResizeStart,
  historyPage,
  historySelectedPhase,
  isRunStatusExpanded,
  isSelectedActivityCollapsed,
  openPath,
  selectedActivityId,
  selectedActivityVariant,
  selectedRun,
  selectedRunId,
  setHistoryPage,
  setHistorySelectedPhase,
  setIsRunStatusExpanded,
  setIsSelectedActivityCollapsed,
  setSelectedActivityId,
  setSelectedActivityVariant,
}) {
  let leftSections = [];

  if (!selectedRunId) {
    leftSections = [
      h(
        'section',
        {
          className: 'empty-state-card',
          key: 'empty',
        },
        [
          h('h2', { key: 'title' }, 'No run selected'),
          h(
            'p',
            { key: 'copy' },
            'Choose a run from the sidebar to inspect its live orchestration state.'
          ),
        ]
      ),
    ];
  } else if (dashboardState.status === 'loading' && !dashboard) {
    leftSections = [
      h(
        'section',
        {
          className: 'empty-state-card',
          key: 'loading',
        },
        [h('p', { key: 'copy' }, `Loading dashboard for ${selectedRunId}…`)]
      ),
    ];
  } else if (dashboardState.status === 'error') {
    leftSections = [
      h(
        'section',
        {
          className: 'empty-state-card',
          key: 'error',
        },
        [
          h('h2', { key: 'title' }, 'Dashboard unavailable'),
          h(
            'p',
            { key: 'copy', role: 'alert' },
            dashboardState.errorMessage
          ),
        ]
      ),
    ];
  } else if (dashboard) {
    leftSections = [
      renderRunStatusOverview({
        counts: dashboard.counts,
        dashboard,
        isExpanded: isRunStatusExpanded,
        key: 'run-status-overview',
        openPath,
        selectedRun,
        setIsExpanded: setIsRunStatusExpanded,
      }),
      renderObjectiveProgress(
        dashboard.objective_progress,
        dashboard.activities,
        selectedActivityId,
        selectedActivityVariant,
        setSelectedActivityId,
        setSelectedActivityVariant
      ),
      renderHistoryTable(
        dashboard.history,
        historyPage,
        historySelectedPhase,
        selectedActivityId,
        selectedActivityVariant,
        setHistoryPage,
        setHistorySelectedPhase,
        setSelectedActivityId,
        setSelectedActivityVariant
      ),
      renderEventsTable(dashboard.events),
    ];
  }

  return h(
    'div',
    {
      className: 'monitor-workspace',
      key: 'workspace',
      style: {
        '--detail-rail-width': `${detailRailWidth}px`,
      },
    },
    [
      h(
        'div',
        {
          className: 'monitor-main-column',
          key: 'main-column',
        },
        [
          h(
            'div',
            {
              className: 'monitor-main-column-inner',
              key: 'main-column-inner',
            },
            leftSections
          ),
        ]
      ),
      h('div', {
        'aria-label': 'Resize selected activity panel',
        className: 'monitor-rail-resizer',
        key: 'detail-resizer',
        onPointerDown: handleDetailRailResizeStart,
        role: 'separator',
      }),
      h(
        'aside',
        {
          className: selectedActivityId
            ? 'monitor-detail-rail is-active'
            : 'monitor-detail-rail',
          key: 'detail-rail',
        },
        [
          renderSelectedActivityInspector(
            detailState,
            isSelectedActivityCollapsed,
            openPath,
            selectedActivityId,
            selectedActivityVariant,
            setIsSelectedActivityCollapsed
          ),
        ]
      ),
    ]
  );
}

function renderRunStatusOverview({
  counts,
  dashboard,
  isExpanded,
  key,
  openPath,
  selectedRun,
  setIsExpanded,
}) {
  const sections = [
    {
      body: renderGuidanceCard(dashboard.guidance, openPath),
      summary: summarizeGuidance(dashboard.guidance),
      title: 'Next Action',
    },
    {
      body: renderAutonomyCard(dashboard.autonomy, openPath),
      summary: summarizeAutonomy(dashboard.autonomy),
      title: 'Autonomy',
    },
    {
      body: renderObservabilityCard(dashboard.observability),
      summary: summarizeObservability(dashboard.observability),
      title: 'Observability',
    },
    {
      body: renderCountsCard(counts),
      summary: summarizeCounts(dashboard.run, counts),
      title: 'Activity Counts',
    },
  ];

  return h(
    'section',
    {
      className: 'run-status-overview panel-card',
      key,
    },
    [
      h(
        'div',
        {
          className: 'run-status-topline',
          key: 'topline',
        },
        [
          h(
            'div',
            {
              className: 'run-status-main',
              key: 'main',
            },
            [
              h('h2', { className: 'run-status-title', key: 'title' }, dashboard.run.run_id),
              h(
                'p',
                {
                  className: 'run-status-meta',
                  key: 'meta',
                },
                `${dashboard.run.current_phase} · started ${dashboard.run.started_at || 'unknown'} · updated ${dashboard.run.updated_at}`
              ),
            ]
          ),
          selectedRun
            ? h(
                'div',
                {
                  className: 'run-status-health',
                  key: 'health',
                },
                [
                  h(
                    'p',
                    {
                      className: 'run-status-reason',
                      key: 'reason',
                    },
                    selectedRun.run_status_reason
                  ),
                  h(
                    'div',
                    {
                      className: 'run-status-actions',
                      key: 'actions',
                    },
                    [
                      h(
                        'button',
                        {
                          className:
                            'activity-toggle-button summary-toggle-button run-status-toggle',
                          key: 'toggle',
                          onClick: () => setIsExpanded((currentValue) => !currentValue),
                          type: 'button',
                        },
                        isExpanded ? 'Hide details' : 'Details'
                      ),
                      h(
                        'span',
                        {
                          className: `status-pill status-${slugify(
                            selectedRun.run_status
                          )}`,
                          key: 'pill',
                        },
                        selectedRun.run_status
                      ),
                    ]
                  ),
                ]
              )
            : h(
                'div',
                {
                  className: 'run-status-actions',
                  key: 'actions',
                },
                [
                  h(
                    'button',
                    {
                      className:
                        'activity-toggle-button summary-toggle-button run-status-toggle',
                      key: 'toggle',
                      onClick: () => setIsExpanded((currentValue) => !currentValue),
                      type: 'button',
                    },
                    isExpanded ? 'Hide details' : 'Details'
                  ),
                ]
              ),
        ]
      ),
      h(
        'div',
        {
          className: 'run-status-summary-grid',
          key: 'summary-grid',
        },
        sections.map((section) =>
          renderCompactStatusSection({
            ...section,
            isExpanded,
            key: section.title,
          })
        )
      ),
    ]
  );
}

function renderCompactStatusSection({
  body,
  isExpanded,
  key,
  summary,
  title,
}) {
  return h(
    'section',
    {
      className: isExpanded
        ? 'compact-status-section is-expanded'
        : 'compact-status-section',
      key,
    },
    [
      h(
        'div',
        {
          className: 'compact-status-header',
          key: 'header',
        },
        [
          h(
            'span',
            {
              className: 'compact-status-title',
              key: 'title',
            },
            title
          ),
          h(
            'p',
            {
              className: 'compact-status-summary',
              key: 'summary',
              title: summary,
            },
            summary
          ),
        ]
      ),
      isExpanded
        ? h(
            'div',
            {
              className: 'compact-status-body',
              key: 'body',
            },
            body
          )
        : null,
    ]
  );
}

function summarizeGuidance(guidance) {
  return guidance.next_action_command
    ? `${guidance.next_action_command} · ${guidance.next_action_reason}`
    : `${guidance.run_status} · ${guidance.run_status_reason}`;
}

function summarizeAutonomy(autonomy) {
  return autonomy.execution_note
    ? `${autonomy.controller_status} · ${autonomy.execution_note}`
    : `${autonomy.controller_status} · approval ${autonomy.approval_scope}`;
}

function summarizeObservability(observability) {
  return `${observability.active_processes} active · ${observability.total_calls} calls · avg latency ${observability.latency_summary.average}`;
}

function summarizeCounts(run, counts) {
  const blocked = counts.counts_by_status.blocked || 0;
  const running = counts.counts_by_status.running || 0;
  return `active ${run.active_activity_count} · queued ${run.queued_activity_count} · running ${running} · blocked ${blocked}`;
}

function renderGuidanceCard(guidance, openPath) {
  return h(
    'div',
    { className: 'kv-list' },
    [
      renderKv('Status', guidance.run_status, 'status'),
      renderKv('Reason', guidance.run_status_reason, 'reason'),
      renderKv('Next action', guidance.next_action_command || 'none', 'command'),
      renderKv('Action reason', guidance.next_action_reason, 'action-reason'),
      renderKv('Review doc', guidance.review_doc_path || 'none', 'review', {
        openPath,
      }),
      renderKv(
        'Recommendation',
        guidance.phase_recommendation || 'none',
        'recommendation'
      ),
    ]
  );
}

function renderAutonomyCard(autonomy, openPath) {
  return h(
    'div',
    { className: 'kv-list' },
    [
      renderKv('Controller', autonomy.controller_status, 'controller'),
      renderKv('Approval scope', autonomy.approval_scope, 'approval-scope'),
      renderKv(
        'Stop before',
        autonomy.stop_before_phases.length > 0
          ? autonomy.stop_before_phases.join(', ')
          : 'none',
        'stop-before'
      ),
      renderKv(
        'Stop on recovery',
        autonomy.stop_on_recovery ? 'true' : 'false',
        'stop-on-recovery'
      ),
      renderKv(
        'Adaptive tuning',
        autonomy.adaptive_tuning ? 'true' : 'false',
        'adaptive'
      ),
      renderKv('Last action', autonomy.last_action || 'none', 'last-action'),
      renderKv(
        'Last action status',
        autonomy.last_action_status || 'none',
        'last-action-status'
      ),
      renderKv('Audit log', autonomy.audit_log_path || 'none', 'audit-log', {
        openPath,
      }),
      renderKv('Execution note', autonomy.execution_note || 'none', 'note'),
    ]
  );
}

function renderObservabilityCard(observability) {
  return h(
    'div',
    { className: 'kv-list' },
    [
      renderKv(
        'Calls',
        `${observability.total_calls} total / ${observability.completed_calls} completed / ${observability.failed_calls} failed`,
        'calls'
      ),
      renderKv(
        'Timed out / retries',
        `${observability.timed_out_calls} / ${observability.retry_scheduled_calls}`,
        'timeouts'
      ),
      renderKv(
        'Tokens',
        `in ${observability.total_input_tokens} / cached ${observability.total_cached_input_tokens} / out ${observability.total_output_tokens}`,
        'tokens'
      ),
      renderKv(
        'Latency',
        `avg ${observability.latency_summary.average} / max ${observability.latency_summary.max}`,
        'latency'
      ),
      renderKv(
        'Queue wait',
        observability.latency_summary.queue_wait_average,
        'queue'
      ),
      renderKv('Active processes', String(observability.active_processes), 'active'),
      renderKv(
        'Calls by kind',
        formatCounts(observability.calls_by_kind),
        'calls-by-kind'
      ),
    ]
  );
}

function renderCountsCard(counts) {
  return h(
    'div',
    { className: 'kv-list' },
    [
      renderKv('By status', formatCounts(counts.counts_by_status), 'status'),
      renderKv('By kind', formatCounts(counts.counts_by_kind), 'kind'),
    ]
  );
}

function renderObjectiveProgress(
  items,
  activityGroups,
  selectedActivityId,
  selectedActivityVariant,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  const activitiesByObjective = groupActivitiesByObjective(activityGroups);

  return h(
    'section',
    {
      className: 'panel-card',
      key: 'objective-progress',
    },
    [
      h('h3', { key: 'title' }, 'Objective Progress'),
      items.length === 0
        ? h('p', { key: 'empty' }, 'No objectives available.')
        : h(
            'div',
            {
              className: 'objective-list',
              key: 'list',
            },
            items.map((item) =>
              {
                const objectiveActivities = activitiesByObjective[item.objective_id] || [];
                return h(
                  'div',
                  {
                    className: 'objective-row',
                    key: item.objective_id,
                  },
                  [
                    h('strong', { key: 'label' }, item.title || item.label),
                    renderObjectiveSummaryMeta(item, 'summary'),
                    objectiveActivities.length > 0
                      ? h(
                          'div',
                          {
                            className: 'objective-activity-list',
                            key: 'activities',
                          },
                          objectiveActivities.flatMap((activity) =>
                            renderObjectiveActivityLinks(
                              activity,
                              selectedActivityId,
                              selectedActivityVariant,
                              setSelectedActivityId,
                              setSelectedActivityVariant
                            )
                          )
                        )
                      : h(
                          'span',
                          {
                            className: 'objective-meta objective-activity-empty',
                            key: 'empty-activities',
                          },
                          'No current activities in this phase.'
                        ),
                  ]
                );
              }
            )
          ),
    ]
  );
}

function renderObjectiveSummaryMeta(item, key) {
  return h(
    'div',
    {
      className: 'objective-summary',
      key,
    },
    [
      h(
        'span',
        {
          className: 'objective-pill objective-pill-phase',
          key: 'phase',
        },
        item.phase
      ),
      h(
        'span',
        {
          className: 'objective-pill objective-pill-progress',
          key: 'progress',
        },
        item.progress_percent
      ),
      h(
        'div',
        {
          className: 'objective-status-list',
          key: 'statuses',
        },
        parseObjectiveStatusSummary(item.status_summary).map((status) =>
          h(
            'span',
            {
              className: 'objective-status-chip',
              key: `${status.status}:${status.count}`,
            },
            `${status.status} ${status.count}`
          )
        )
      ),
    ]
  );
}

function renderObjectiveActivityLinks(
  activity,
  selectedActivityId,
  selectedActivityVariant,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  return [
    renderObjectiveActivityNode(
      activity,
      selectedActivityId,
      selectedActivityVariant,
      setSelectedActivityId,
      setSelectedActivityVariant
    ),
  ];
}

function renderObjectiveActivityNode(
  activity,
  selectedActivityId,
  selectedActivityVariant,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  const attemptGroups = buildObjectiveAttemptGroups(activity);

  return h(
    'section',
    {
      className: 'objective-activity-node history-node',
      key: activity.activity_id,
    },
    [
      h(
        'div',
        {
          className: 'history-node-header',
          key: 'header',
        },
        [
          renderObjectiveActivityButton({
            className:
              activity.activity_id === selectedActivityId &&
              selectedActivityVariant === 'current'
                ? 'history-node-title history-node-title-button is-selected'
                : 'history-node-title history-node-title-button',
            key: 'title',
            label: activity.display_name,
            onClick: () =>
              toggleSelectedActivity(
                setSelectedActivityId,
                setSelectedActivityVariant,
                activity.activity_id,
                'current'
              ),
          }),
        ]
      ),
      h(
        'div',
        {
          className: 'objective-activity-children history-children',
          key: 'children',
        },
        attemptGroups.map((attemptGroup, attemptIndex) =>
          h(
            'section',
            {
              className: 'history-attempt-group',
              key: `${activity.activity_id}:attempt:${attemptGroup.attempt}:${attemptIndex}`,
            },
            [
              h(
                'div',
                {
                  className: 'history-attempt-header',
                  key: 'attempt-header',
                },
                [
                  renderObjectiveActivityButton({
                    className:
                      activity.activity_id === selectedActivityId &&
                      selectedActivityVariant === objectiveVariantForAttempt(attemptGroup, attemptGroups)
                        ? 'history-entry-attempt history-attempt-button is-selected'
                        : 'history-entry-attempt history-attempt-button',
                    key: 'attempt',
                    label: `attempt ${attemptGroup.attempt}`,
                    onClick: () =>
                      toggleSelectedActivity(
                        setSelectedActivityId,
                        setSelectedActivityVariant,
                        activity.activity_id,
                        objectiveVariantForAttempt(attemptGroup, attemptGroups)
                      ),
                  }),
                  h(
                    'span',
                    {
                      className: 'history-attempt-kind objective-meta',
                      key: 'kind',
                    },
                    attemptGroup.attempt > 1 ? 'repair attempt' : 'initial attempt'
                  ),
                ]
              ),
              h(
                'div',
                {
                  className: 'history-attempt-children',
                  key: 'attempt-children',
                },
                attemptGroup.entries.map((entry, entryIndex) =>
                  renderObjectiveAttemptEntry(
                    activity,
                    attemptGroup,
                    attemptGroups,
                    entry,
                    entryIndex,
                    selectedActivityId,
                    selectedActivityVariant,
                    setSelectedActivityId,
                    setSelectedActivityVariant
                  )
                )
              ),
            ]
          )
        )
      ),
    ]
  );
}

function renderObjectiveActivityButton({
  className,
  key,
  label,
  onClick,
}) {
  return h(
    'button',
    {
      className,
      key,
      onClick,
      type: 'button',
    },
    label
  );
}

function renderObjectiveAttemptEntry(
  activity,
  attemptGroup,
  attemptGroups,
  entry,
  entryIndex,
  selectedActivityId,
  selectedActivityVariant,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  const variant = objectiveVariantForAttempt(attemptGroup, attemptGroups);
  const isSelected =
    activity.activity_id === selectedActivityId && selectedActivityVariant === variant;

  return h(
    'button',
    {
      className: isSelected
        ? 'history-entry history-entry-button is-selected'
        : 'history-entry history-entry-button',
      key: `${activity.activity_id}:${attemptGroup.attempt}:${entryIndex}`,
      onClick: () =>
        toggleSelectedActivity(
          setSelectedActivityId,
          setSelectedActivityVariant,
          activity.activity_id,
          variant
        ),
      type: 'button',
    },
    [
      h(
        'div',
        {
          className: 'history-entry-topline',
          key: 'topline',
        },
        [
          h(
            'span',
            {
              className: `history-entry-status history-entry-status-${slugify(
                entry.status
              )}`,
              key: 'status',
            },
            entry.status
          ),
        ]
      ),
      entry.summary
        ? h(
            'p',
            {
              className: 'history-entry-summary',
              key: 'summary',
            },
            entry.summary
          )
        : null,
    ]
  );
}

function buildObjectiveAttemptGroups(activity) {
  if (!activityHasRecoveryHierarchy(activity)) {
    return [
      {
        attempt: 1,
        entries: [
          {
            status: activity.status_label,
            summary: activity.current_activity || activity.status_reason || '',
          },
        ],
      },
    ];
  }

  return [
    {
      attempt: 1,
      entries: [
        {
          status: 'repair triggered',
          summary: describeObjectiveRepairTrigger(activity),
        },
      ],
    },
    {
      attempt: Number(activity.attempt || 2),
      entries: [
        {
          status: activity.status_label,
          summary: activity.current_activity || activity.status_reason || '',
        },
      ],
    },
  ];
}

function describeObjectiveRepairTrigger(activity) {
  if (activity.status_reason && activity.status !== 'recovered') {
    return humanizeObjectiveText(activity.status_reason);
  }
  if (activity.recovery_action) {
    return `Initial attempt required repair via ${humanizeObjectiveText(activity.recovery_action)}.`;
  }
  return 'Initial attempt led to a repair attempt.';
}

function objectiveVariantForAttempt(attemptGroup, attemptGroups) {
  return attemptGroup.attempt === 1 && attemptGroups.length > 1 ? 'original' : 'current';
}

function humanizeObjectiveText(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.replace(/_/g, ' ');
}

function activityHasRecoveryHierarchy(activity) {
  return (
    Number(activity.attempt || 1) > 1 ||
    activity.status === 'recovered' ||
    activity.status === 'interrupted'
  );
}

function toggleSelectedActivity(
  setSelectedActivityId,
  setSelectedActivityVariant,
  activityId,
  variant
) {
  setSelectedActivityId((currentActivityId) => {
    let nextActivityId = activityId;
    setSelectedActivityVariant((currentVariant) => {
      const isSameSelection =
        currentActivityId === activityId && currentVariant === variant;
      if (isSameSelection) {
        nextActivityId = null;
      }
      return isSameSelection ? 'current' : variant;
    });
    return nextActivityId;
  });
}

function renderSelectedActivityInspector(
  detailState,
  isCollapsed,
  openPath,
  selectedActivityId,
  selectedActivityVariant,
  setIsCollapsed
) {
  return h(
    'section',
    {
      className: selectedActivityId
        ? 'panel-card activity-inspector-card'
        : 'panel-card activity-inspector-card is-idle',
      key: 'activity-inspector',
    },
    [
      h(
        'div',
        {
          className: 'activity-inspector-header',
          key: 'header',
        },
        [
          h('h3', { key: 'title' }, 'Selected Activity'),
          selectedActivityId
            ? h(
                'button',
                {
                  className: 'activity-toggle-button inspector-collapse-button',
                  key: 'toggle',
                  onClick: () => setIsCollapsed((currentValue) => !currentValue),
                  type: 'button',
                },
                isCollapsed ? 'Expand' : 'Collapse'
              )
            : null,
        ]
      ),
      !selectedActivityId
        ? h(
            'p',
            {
              className: 'objective-meta',
              key: 'empty',
            },
            'Select an activity from the left side to inspect its prompt, response, and failure details here.'
          )
        : isCollapsed
          ? h(
              'p',
              {
                className: 'objective-meta',
                key: 'collapsed',
              },
              'Inspector collapsed.'
            )
        : renderInlineActivityDetail(
            detailState,
            openPath,
            selectedActivityId,
            selectedActivityVariant
          ),
    ]
  );
}

function renderActivityTable(
  title,
  rows,
  openPath,
  selectedActivityId,
  detailState,
  setSelectedActivityId,
  setSelectedActivityVariant,
  key
) {
  return h(
    'section',
    {
      className: 'panel-card',
      key,
    },
    [
      h('h3', { key: 'title' }, title),
      rows.length === 0
        ? h('p', { key: 'empty' }, 'No activities in this section.')
        : h(
            'div',
            {
              className: 'table-scroll',
              key: 'table-wrap',
            },
            [
              h(
                'table',
                { className: 'data-table', key: 'table' },
                [
                  h(
                    'thead',
                    { key: 'head' },
                    h('tr', {}, [
                      h('th', { key: 'activity' }, 'Activity'),
                      h('th', { key: 'objective' }, 'Objective'),
                      h('th', { key: 'status' }, 'Status'),
                      h('th', { key: 'llm' }, 'LLM'),
                      h('th', { key: 'warnings' }, 'Warnings'),
                      h('th', { key: 'progress' }, 'Progress'),
                      h('th', { key: 'elapsed' }, 'Elapsed'),
                      h('th', { key: 'current' }, 'Current'),
                    ])
                  ),
                  h(
                    'tbody',
                    { key: 'body' },
                    rows.flatMap((row) => {
                      const isSelected = row.activity_id === selectedActivityId;
                      const result = [
                        h(
                          'tr',
                          {
                            className: isSelected ? 'is-selected-row' : '',
                            key: row.activity_id,
                          },
                          [
                            h(
                              'td',
                              { key: 'activity' },
                              h(
                                'div',
                                { className: 'activity-cell' },
                                [
                                  h(
                                    'button',
                                    {
                                      'aria-label': `Inspect ${row.activity_id}`,
                                      className: 'table-button',
                                      onClick: () =>
                                        toggleSelectedActivity(
                                          setSelectedActivityId,
                                          setSelectedActivityVariant,
                                          row.activity_id,
                                          'current'
                                        ),
                                      type: 'button',
                                    },
                                    row.label
                                  ),
                                  h(
                                    'button',
                                    {
                                      className: 'activity-toggle-button',
                                      key: 'toggle',
                                      onClick: () =>
                                        toggleSelectedActivity(
                                          setSelectedActivityId,
                                          setSelectedActivityVariant,
                                          row.activity_id,
                                          'current'
                                        ),
                                      type: 'button',
                                    },
                                    isSelected ? 'Hide prompt' : 'Show prompt'
                                  ),
                                ]
                              )
                            ),
                            h('td', { key: 'objective' }, row.objective_label),
                            h('td', { key: 'status' }, row.status_label),
                            h('td', { key: 'llm' }, row.llm_summary),
                            h('td', { key: 'warnings' }, row.warnings_text || '-'),
                            h('td', { key: 'progress' }, row.progress_percent),
                            h('td', { key: 'elapsed' }, row.elapsed),
                            h('td', { key: 'current' }, row.current_activity || '-'),
                          ]
                        ),
                      ];

                      if (isSelected) {
                        result.push(
                          h(
                            'tr',
                            {
                              className: 'activity-inline-row',
                              key: `${row.activity_id}:detail`,
                            },
                            h(
                              'td',
                              {
                                className: 'activity-inline-cell',
                                colSpan: 8,
                                key: 'detail',
                              },
                              renderInlineActivityDetail(
                                detailState,
                                openPath,
                                row.activity_id,
                                'current'
                              )
                            )
                          )
                        );
                      }

                      return result;
                    })
                  ),
                ]
              ),
            ]
          ),
    ]
  );
}

function renderHandoffTable(rows) {
  return h(
    'section',
    {
      className: 'panel-card',
      key: 'handoffs',
    },
    [
      h('h3', { key: 'title' }, 'Collaboration Handoffs'),
      rows.length === 0
        ? h('p', { key: 'empty' }, 'No handoffs for the current phase.')
        : h(
            'div',
            { className: 'table-scroll', key: 'table-wrap' },
            [
              h(
                'table',
                { className: 'data-table', key: 'table' },
                [
                  h(
                    'thead',
                    { key: 'head' },
                    h('tr', {}, [
                      h('th', { key: 'id' }, 'Handoff'),
                      h('th', { key: 'objective' }, 'Objective'),
                      h('th', { key: 'status' }, 'Status'),
                      h('th', { key: 'from' }, 'From'),
                      h('th', { key: 'to' }, 'To Tasks'),
                      h('th', { key: 'blocking' }, 'Blocking'),
                    ])
                  ),
                  h(
                    'tbody',
                    { key: 'body' },
                    rows.map((row) =>
                      h('tr', { key: row.handoff_id }, [
                        h('td', { key: 'id' }, row.handoff_id),
                        h('td', { key: 'objective' }, row.objective_label),
                        h(
                          'td',
                          { key: 'status' },
                          `${row.status} (${row.status_reason || '-'})`
                        ),
                        h('td', { key: 'from' }, row.from_task_id),
                        h(
                          'td',
                          { key: 'to' },
                          row.to_task_ids.length > 0
                            ? row.to_task_ids.join(', ')
                            : '-'
                        ),
                        h(
                          'td',
                          { key: 'blocking' },
                          row.blocking ? 'yes' : 'no'
                        ),
                      ])
                    )
                  ),
                ]
              ),
            ]
          ),
    ]
  );
}

function renderHistoryTable(
  rows,
  historyPage,
  historySelectedPhase,
  selectedActivityId,
  selectedActivityVariant,
  setHistoryPage,
  setHistorySelectedPhase,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  const allPhaseGroups = groupHistoryRowsByPhase(rows);
  const availablePhase = resolveHistorySelectedPhase(
    historySelectedPhase,
    allPhaseGroups
  );
  const filteredPhaseGroups =
    availablePhase === 'all'
      ? allPhaseGroups
      : allPhaseGroups.filter((phaseGroup) => phaseGroup.phase === availablePhase);
  const pagination = paginateHistoryPhaseGroups(filteredPhaseGroups, historyPage, HISTORY_GROUPS_PER_PAGE);
  const phaseGroups = pagination.phaseGroups;

  return h(
    'section',
    {
      className: 'panel-card',
      key: 'history',
    },
    [
      h(
        'div',
        { className: 'history-section-header', key: 'header' },
        [
          h('h3', { key: 'title' }, 'Activity History'),
          h(
            'div',
            {
              className: 'history-header-controls',
              key: 'controls',
            },
            [
              renderHistoryPhaseFilters(
                allPhaseGroups,
                availablePhase,
                setHistoryPage,
                setHistorySelectedPhase
              ),
              pagination.totalPages > 1
                ? renderHistoryPagination(pagination, setHistoryPage)
                : null,
            ]
          ),
        ]
      ),
      rows.length === 0
        ? h('p', { key: 'empty' }, 'No terminal activity history yet.')
        : h(
            'div',
            { className: 'history-tree', key: 'history-tree' },
            phaseGroups.map((phaseGroup, phaseIndex) =>
              h(
                'section',
                {
                  className: 'history-phase-group',
                  key: `${phaseGroup.phase}:${phaseIndex}`,
                },
                [
                  h(
                    'div',
                    {
                      className: 'history-phase-header',
                      key: 'phase-header',
                    },
                    [
                      h(
                        'span',
                        {
                          className: 'objective-pill objective-pill-phase',
                          key: 'phase-pill',
                        },
                        phaseGroup.phase
                      ),
                      h(
                        'span',
                        {
                          className: 'history-phase-count objective-meta',
                          key: 'phase-count',
                        },
                        `${phaseGroup.groups.length} activities`
                      ),
                    ]
                  ),
                  h(
                    'div',
                    {
                      className: 'history-phase-tree',
                      key: 'phase-tree',
                    },
                    phaseGroup.groups.map((group, index) =>
                      renderHistoryNode(
                        group,
                        `${phaseGroup.phase}:${group.activity_id}:${index}`,
                        selectedActivityId,
                        selectedActivityVariant,
                        setSelectedActivityId,
                        setSelectedActivityVariant
                      )
                    )
                  ),
                ]
              )
            )
          ),
    ]
  );
}

function renderHistoryPagination(pagination, setHistoryPage) {
  return h(
    'div',
    {
      className: 'history-pagination',
      key: 'pagination',
    },
    [
      h(
        'button',
        {
          className: 'activity-toggle-button history-pagination-button',
          disabled: pagination.page <= 1,
          key: 'previous',
          onClick: () => setHistoryPage((currentPage) => Math.max(1, currentPage - 1)),
          type: 'button',
        },
        'Previous'
      ),
      h(
        'span',
        {
          className: 'history-pagination-copy objective-meta',
          key: 'copy',
        },
        `Page ${pagination.page} of ${pagination.totalPages}`
      ),
      h(
        'button',
        {
          className: 'activity-toggle-button history-pagination-button',
          disabled: pagination.page >= pagination.totalPages,
          key: 'next',
          onClick: () =>
            setHistoryPage((currentPage) =>
              Math.min(pagination.totalPages, currentPage + 1)
            ),
          type: 'button',
        },
        'Next'
      ),
    ]
  );
}

function renderHistoryPhaseFilters(
  phaseGroups,
  selectedPhase,
  setHistoryPage,
  setHistorySelectedPhase
) {
  const filters = [
    {
      count: phaseGroups.reduce(
        (total, phaseGroup) => total + phaseGroup.groups.length,
        0
      ),
      label: 'All',
      value: 'all',
    },
  ].concat(
    phaseGroups.map((phaseGroup) => ({
      count: phaseGroup.groups.length,
      label: phaseGroup.phase,
      value: phaseGroup.phase,
    }))
  );

  return h(
    'div',
    {
      className: 'history-phase-filters',
      key: 'filters',
    },
    filters.map((filter) =>
      h(
        'button',
        {
          className:
            filter.value === selectedPhase
              ? 'history-phase-filter is-selected'
              : 'history-phase-filter',
          key: filter.value,
          onClick: () => {
            setHistorySelectedPhase(filter.value);
            setHistoryPage(1);
          },
          type: 'button',
        },
        `${filter.label} ${filter.count}`
      )
    )
  );
}

function renderHistoryNode(
  group,
  key,
  selectedActivityId,
  selectedActivityVariant,
  setSelectedActivityId,
  setSelectedActivityVariant
) {
  const attemptGroups = groupHistoryAttempts(group.entries);

  return h(
    'section',
    {
      className: 'history-node',
      key,
    },
    [
      h(
        'div',
        {
          className: 'history-node-header',
          key: 'header',
        },
        [
          h(
            'button',
            {
              className:
                group.activity_id === selectedActivityId &&
                selectedActivityVariant === 'current'
                  ? 'history-node-title history-node-title-button is-selected'
                  : 'history-node-title history-node-title-button',
              key: 'title',
              onClick: () =>
                toggleSelectedActivity(
                  setSelectedActivityId,
                  setSelectedActivityVariant,
                  group.activity_id,
                  'current'
                ),
              type: 'button',
            },
            historyBaseLabel(group.label)
          ),
          h(
            'span',
            {
              className: 'history-node-objective objective-meta',
              key: 'objective',
            },
            compactObjectiveLabel(group.objective_label)
          ),
        ]
      ),
      h(
        'div',
        {
          className: 'history-children',
          key: 'children',
        },
        attemptGroups.map((attemptGroup, attemptIndex) =>
          h(
            'section',
            {
              className: 'history-attempt-group',
              key: `${group.activity_id}:attempt:${attemptGroup.attempt}:${attemptIndex}`,
            },
            [
              h(
                'div',
                {
                  className: 'history-attempt-header',
                  key: 'attempt-header',
                },
                [
                  h(
                    'button',
                    {
                      className:
                        group.activity_id === selectedActivityId &&
                        selectedActivityVariant ===
                          historyVariantForAttempt(attemptGroup, attemptGroups)
                          ? 'history-entry-attempt history-attempt-button is-selected'
                          : 'history-entry-attempt history-attempt-button',
                      key: 'attempt',
                      onClick: () =>
                        toggleSelectedActivity(
                          setSelectedActivityId,
                          setSelectedActivityVariant,
                          group.activity_id,
                          historyVariantForAttempt(attemptGroup, attemptGroups)
                        ),
                      type: 'button',
                    },
                    `attempt ${attemptGroup.attempt}`
                  ),
                  h(
                    'span',
                    {
                      className: 'history-attempt-kind objective-meta',
                      key: 'kind',
                    },
                    attemptGroup.attempt > 1 ? 'repair attempt' : 'initial attempt'
                  ),
                ]
              ),
              h(
                'div',
                {
                  className: 'history-attempt-children',
                  key: 'attempt-children',
                },
                attemptGroup.entries.map((entry, entryIndex) =>
                  h(
                    'button',
                    {
                      className:
                        group.activity_id === selectedActivityId &&
                        selectedActivityVariant ===
                          historyVariantForAttempt(attemptGroup, attemptGroups)
                          ? 'history-entry history-entry-button is-selected'
                          : 'history-entry history-entry-button',
                      key: historyRowKey(entry, entryIndex),
                      onClick: () =>
                        toggleSelectedActivity(
                          setSelectedActivityId,
                          setSelectedActivityVariant,
                          group.activity_id,
                          historyVariantForAttempt(attemptGroup, attemptGroups)
                        ),
                      type: 'button',
                    },
                    [
                      h(
                        'div',
                        {
                          className: 'history-entry-topline',
                          key: 'topline',
                        },
                        [
                          h(
                            'span',
                            {
                              className: `history-entry-status history-entry-status-${slugify(
                                entry.synthetic
                                  ? 'repair triggered'
                                  : entry.status.replace(/_/g, ' ')
                              )}`,
                              key: 'status',
                            },
                            entry.synthetic
                              ? 'repair triggered'
                              : entry.status.replace(/_/g, ' ')
                          ),
                          h(
                            'span',
                            {
                              className: 'history-entry-timestamp objective-meta',
                              key: 'timestamp',
                            },
                            entry.timestamp || '-'
                          ),
                        ]
                      ),
                      historyEntrySummary(entry)
                        ? h(
                            'p',
                            {
                              className: 'history-entry-summary',
                              key: 'summary',
                            },
                            historyEntrySummary(entry)
                          )
                        : null,
                    ]
                  )
                )
              ),
            ]
          )
        )
      ),
    ]
  );
}

function historyVariantForAttempt(attemptGroup, attemptGroups) {
  return attemptGroup.attempt === 1 && attemptGroups.length > 1 ? 'original' : 'current';
}

function renderEventsTable(rows) {
  return h(
    'section',
    {
      className: 'panel-card',
      key: 'events',
    },
    [
      h('h3', { key: 'title' }, 'Recent Run Events'),
      rows.length === 0
        ? h('p', { key: 'empty' }, 'No events recorded.')
        : h(
            'div',
            { className: 'table-scroll', key: 'table-wrap' },
            [
              h(
                'table',
                { className: 'data-table', key: 'table' },
                [
                  h(
                    'thead',
                    { key: 'head' },
                    h('tr', {}, [
                      h('th', { key: 'timestamp' }, 'Timestamp'),
                      h('th', { key: 'activity' }, 'Activity'),
                      h('th', { key: 'type' }, 'Type'),
                      h('th', { key: 'message' }, 'Message'),
                    ])
                  ),
                  h(
                    'tbody',
                    { key: 'body' },
                    rows.map((row, index) =>
                      h('tr', { key: `${row.timestamp}:${index}` }, [
                        h('td', { key: 'timestamp' }, row.timestamp),
                        h('td', { key: 'activity' }, row.activity_id || '-'),
                        h('td', { key: 'type' }, row.event_type),
                        h('td', { key: 'message' }, row.message),
                      ])
                    )
                  ),
                ]
              ),
            ]
          ),
    ]
  );
}

function renderSimpleListCard(title, items, key) {
  return h(
    'section',
    {
      className: 'panel-card',
      key,
    },
    [
      h('h3', { key: 'title' }, title),
      items.length === 0
        ? h('p', { key: 'empty' }, 'None.')
        : h(
            'ul',
            { className: 'simple-list', key: 'list' },
            items.map((item, index) => h('li', { key: index }, item))
          ),
    ]
  );
}

function historyRowKey(row, index) {
  return [
    row.activity_id || 'activity',
    row.timestamp || 'timestamp',
    row.status || 'status',
    String(row.attempt ?? 1),
    String(index),
  ].join(':');
}

function groupHistoryRows(rows) {
  const groups = new Map();

  rows.forEach((row, index) => {
    const groupKey = row.activity_id || `${row.label}:${index}`;
    if (!groups.has(groupKey)) {
      groups.set(groupKey, {
        activity_id: row.activity_id || groupKey,
        entries: [],
        label: row.label,
        objective_label: row.objective_label,
      });
    }

    groups.get(groupKey).entries.push({ ...row, _index: index });
  });

  return Array.from(groups.values())
    .map((group) => {
      const entries = group.entries
        .slice()
        .sort((left, right) => compareHistoryTimestamps(left.timestamp, right.timestamp) || left._index - right._index);
      const rootEntry = entries[0] || {};
      return {
        activity_id: group.activity_id,
        entries,
        label: historyBaseLabel(rootEntry.label || group.label),
        latest_timestamp: entries[entries.length - 1]?.timestamp || null,
        objective_label: rootEntry.objective_label || group.objective_label,
      };
    })
    .sort(
      (left, right) =>
        compareHistoryTimestamps(right.latest_timestamp, left.latest_timestamp) ||
        left.label.localeCompare(right.label)
    );
}

function groupHistoryAttempts(entries) {
  const byAttempt = new Map();

  entries.forEach((entry, index) => {
    const attempt = Number(entry.attempt || 1);
    if (!byAttempt.has(attempt)) {
      byAttempt.set(attempt, []);
    }
    byAttempt.get(attempt).push({ ...entry, _index: index });
  });

  const attempts = Array.from(byAttempt.keys()).sort((left, right) => left - right);
  const maxAttempt = attempts.length > 0 ? attempts[attempts.length - 1] : 1;
  const groups = [];

  for (let attempt = 1; attempt <= maxAttempt; attempt += 1) {
    const attemptEntries = byAttempt.get(attempt);
    if (attemptEntries && attemptEntries.length > 0) {
      groups.push({
        attempt,
        entries: attemptEntries
          .slice()
          .sort(
            (left, right) =>
              compareHistoryTimestamps(left.timestamp, right.timestamp) ||
              left._index - right._index
          ),
      });
      continue;
    }

    const nextAttemptEntries = byAttempt.get(attempt + 1) || [];
    const repairSource = nextAttemptEntries[0] || {};
    groups.push({
      attempt,
      entries: [
        {
          attempt,
          current_activity: null,
          recovery_action: repairSource.recovery_action || null,
          status: 'repair_triggered',
          status_reason: repairSource.status_reason || null,
          synthetic: true,
          timestamp: repairSource.timestamp || null,
        },
      ],
    });
  }

  return groups;
}

function groupHistoryRowsByPhase(rows) {
  const phaseGroups = new Map();

  rows.forEach((row) => {
    const phase = row.phase || 'unknown';
    if (!phaseGroups.has(phase)) {
      phaseGroups.set(phase, []);
    }
    phaseGroups.get(phase).push(row);
  });

  return Array.from(phaseGroups.entries())
    .map(([phase, phaseRows]) => ({
      phase,
      groups: groupHistoryRows(phaseRows),
      latest_timestamp: phaseRows.reduce((latest, row) => {
        if (!latest) {
          return row.timestamp || null;
        }
        return compareHistoryTimestamps(row.timestamp, latest) > 0 ? row.timestamp : latest;
      }, null),
    }))
    .sort(
      (left, right) =>
        compareHistoryTimestamps(right.latest_timestamp, left.latest_timestamp) ||
        left.phase.localeCompare(right.phase)
    );
}

function paginateHistoryPhaseGroups(phaseGroups, page, pageSize) {
  const flattened = [];

  phaseGroups.forEach((phaseGroup) => {
    phaseGroup.groups.forEach((group) => {
      flattened.push({
        group,
        phase: phaseGroup.phase,
      });
    });
  });

  const totalPages = Math.max(1, Math.ceil(flattened.length / pageSize));
  const currentPage = Math.min(Math.max(page, 1), totalPages);
  const startIndex = (currentPage - 1) * pageSize;
  const pagedItems = flattened.slice(startIndex, startIndex + pageSize);
  const phaseOrder = [];
  const byPhase = new Map();

  pagedItems.forEach((item) => {
    if (!byPhase.has(item.phase)) {
      byPhase.set(item.phase, []);
      phaseOrder.push(item.phase);
    }
    byPhase.get(item.phase).push(item.group);
  });

  return {
    page: currentPage,
    phaseGroups: phaseOrder.map((phase) => ({
      groups: byPhase.get(phase) || [],
      phase,
    })),
    totalPages,
    totalGroups: flattened.length,
  };
}

function resolveHistorySelectedPhase(selectedPhase, phaseGroups) {
  if (selectedPhase === 'all') {
    return 'all';
  }

  if (phaseGroups.some((phaseGroup) => phaseGroup.phase === selectedPhase)) {
    return selectedPhase;
  }

  return 'all';
}

function compareHistoryTimestamps(left, right) {
  const leftValue = Date.parse(left || '');
  const rightValue = Date.parse(right || '');
  const normalizedLeft = Number.isFinite(leftValue) ? leftValue : 0;
  const normalizedRight = Number.isFinite(rightValue) ? rightValue : 0;
  return normalizedLeft - normalizedRight;
}

function historyEntrySummary(entry) {
  if (entry.synthetic) {
    return entry.recovery_action
      ? `Initial attempt required repair via ${entry.recovery_action.replace(/_/g, ' ')}.`
      : 'Initial attempt led to a follow-up repair attempt.';
  }
  return entry.current_activity || entry.status_reason || entry.recovery_action || '';
}

function historyBaseLabel(label) {
  if (typeof label !== 'string') {
    return label || '-';
  }

  return label.replace(/\s*\[attempt \d+\]\s*$/i, '');
}

function renderInlineActivityDetail(
  detailState,
  openPath,
  selectedActivityId,
  selectedActivityVariant
) {
  if (detailState.status === 'loading' && !detailState.payload) {
    return h('div', { className: 'activity-inline-detail' }, [
      h('p', { key: 'loading' }, `Loading ${selectedActivityId} (${selectedActivityVariant})…`),
    ]);
  }

  if (detailState.status === 'error') {
    return h('div', { className: 'activity-inline-detail' }, [
      h(
        'p',
        {
          className: 'inline-error',
          key: 'error',
          role: 'alert',
        },
        detailState.errorMessage
      ),
    ]);
  }

  if (!detailState.payload || !detailState.promptDebug) {
    return h('div', { className: 'activity-inline-detail' }, [
      h('p', { key: 'empty' }, 'Activity detail is unavailable.'),
    ]);
  }

  const activity = detailState.payload.activity;
  const promptDebug = detailState.promptDebug;

  return h('div', { className: 'activity-inline-detail' }, [
    promptDebug.repair_context && promptDebug.repair_context.is_repair
      ? renderRecoverySummary(promptDebug.repair_context, openPath)
      : null,
    !(
      promptDebug.repair_context && promptDebug.repair_context.is_repair
    ) && promptDebug.stdout_failure
      ? renderStdoutFailureSummary(promptDebug.stdout_failure, openPath)
      : null,
    h(
      'div',
      { className: 'activity-inspector-head', key: 'head' },
      [
        h(
          'div',
          { className: 'activity-inspector-main', key: 'main' },
          [
            h(
              'p',
              {
                className: 'objective-meta',
                key: 'variant',
              },
              promptDebug.variant_label || 'Current attempt'
            ),
            h(
              'p',
              {
                className: 'activity-inspector-identifier',
                key: 'identifier',
              },
              activity.label || activity.activity_id
            ),
            h(
              'p',
              {
                className: 'activity-inspector-current',
                key: 'current',
              },
              activity.current_activity || activity.label
            ),
          ]
        ),
        h(
          'div',
          { className: 'activity-inspector-chips', key: 'chips' },
          [
            renderInspectorChip(activity.kind, 'kind'),
            renderInspectorChip(activity.status_label, 'status'),
            renderInspectorChip(`attempt ${activity.attempt}`, 'attempt'),
            renderInspectorChip(activity.elapsed, 'elapsed'),
          ]
        ),
      ]
    ),
    h(
      'div',
      { className: 'activity-meta-grid', key: 'meta' },
      [
        renderActivityMetaSection(
          'Activity',
          [
            ['Objective', compactObjectiveLabel(activity.objective_label)],
            ['Stage', activity.progress_stage],
            ['Status reason', activity.status_reason || '-'],
            ['Recovery action', activity.recovery_action || '-'],
          ],
          'activity'
        ),
        renderActivityMetaSection(
          'Context',
          [
            { label: 'Workspace', value: activity.workspace_path || '-' },
            { label: 'Branch', value: activity.branch_name || '-' },
            { label: 'Prompt path', value: promptDebug.prompt_path || '-', openPath },
            { label: 'Stdout path', value: promptDebug.stdout_path || '-', openPath },
            { label: 'Stderr path', value: promptDebug.stderr_path || '-', openPath },
            { label: 'Response path', value: promptDebug.response_path || '-', openPath },
            {
              label: 'Structured output',
              value: promptDebug.structured_output_path || '-',
              openPath,
            },
          ],
          'context'
        ),
        renderActivityMetaSection(
          'Metrics',
          [
            [
              'Prompt size',
              `${promptDebug.observability.prompt_char_count} chars / ${promptDebug.observability.prompt_line_count} lines / ${promptDebug.observability.prompt_bytes} bytes`,
            ],
            [
              'Latency',
              `queue ${formatMs(promptDebug.observability.queue_wait_ms)} / runtime ${formatMs(promptDebug.observability.runtime_ms)} / wall ${formatMs(promptDebug.observability.wall_clock_ms)}`,
            ],
            [
              'Tokens',
              `in ${promptDebug.observability.input_tokens} / cached ${promptDebug.observability.cached_input_tokens} / out ${promptDebug.observability.output_tokens}`,
            ],
            [
              'Stdout / stderr bytes',
              `${promptDebug.observability.stdout_bytes} / ${promptDebug.observability.stderr_bytes}`,
            ],
          ],
          'metrics'
        ),
      ]
    ),
    h(
      'div',
      { className: 'artifact-grid', key: 'artifacts' },
      [
        renderArtifactPanel(
          'Prompt',
          promptDebug.prompt_text,
          'No prompt body available.',
          'prompt'
        ),
        renderArtifactPanel(
          'Final Response',
          promptDebug.response_text,
          'No final response available yet.',
          'response'
        ),
        renderArtifactPanel(
          'Structured Output',
          promptDebug.structured_output_text,
          'No structured output artifact is available.',
          'structured-output'
        ),
      ]
    ),
    h(
      'details',
      {
        className: 'prompt-debug',
        key: 'events',
      },
      [
        h('summary', { key: 'summary' }, 'Recent activity events'),
        renderEventsTable(detailState.payload.events),
      ]
    ),
  ]);
}

function renderRecoverySummary(repairContext, openPath) {
  return h(
    'section',
    {
      className: 'recovery-summary-card',
      key: 'recovery-summary',
    },
    [
      h(
        'div',
        {
          className: 'recovery-summary-head',
          key: 'head',
        },
        [
          h(
            'h4',
            { className: 'recovery-summary-title', key: 'title' },
            'Recovery'
          ),
          repairContext.recovery_action
            ? h(
                'span',
                {
                  className: 'recovery-summary-chip',
                  key: 'chip',
                },
                repairContext.recovery_action
              )
            : null,
        ]
      ),
      h(
        'div',
        {
          className: 'recovery-summary-grid',
          key: 'grid',
        },
        [
          h(
            'div',
            {
              className: 'recovery-summary-block',
              key: 'failure',
            },
            [
              h(
                'span',
                {
                  className: 'recovery-summary-label',
                  key: 'label',
                },
                'Failure'
              ),
              h(
                'p',
                {
                  className: 'recovery-summary-copy',
                  key: 'copy',
                },
                repairContext.failure_summary || 'Failure details are not available.'
              ),
              repairContext.failure_command
                ? h(
                    'p',
                    {
                      className: 'recovery-summary-command',
                      key: 'command',
                    },
                    `Failed command: ${repairContext.failure_command}`
                  )
                : null,
              renderFailureEvidenceDetails(
                repairContext.failure_excerpt,
                repairContext.failure_stdout_path,
                openPath,
                'recovery'
              ),
            ]
          ),
          h(
            'div',
            {
              className: 'recovery-summary-block',
              key: 'repair',
            },
            [
              h(
                'span',
                {
                  className: 'recovery-summary-label',
                  key: 'label',
                },
                'Repair'
              ),
              h(
                'p',
                {
                  className: 'recovery-summary-copy',
                  key: 'copy',
                },
                repairContext.repair_request_summary || 'Repair instructions are not available.'
              ),
            ]
          ),
        ]
      ),
    ]
  );
}

function renderStdoutFailureSummary(stdoutFailure, openPath) {
  return h(
    'section',
    {
      className: 'recovery-summary-card stdout-failure-card',
      key: 'stdout-failure',
    },
    [
      h(
        'div',
        {
          className: 'recovery-summary-head',
          key: 'head',
        },
        [h('h4', { className: 'recovery-summary-title', key: 'title' }, 'Observed failure')]
      ),
      h(
        'div',
        {
          className: 'recovery-summary-grid',
          key: 'grid',
        },
        [
          h(
            'div',
            {
              className: 'recovery-summary-block',
              key: 'failure',
            },
            [
              h(
                'span',
                {
                  className: 'recovery-summary-label',
                  key: 'label',
                },
                'Stdout'
              ),
              h(
                'p',
                {
                  className: 'recovery-summary-copy',
                  key: 'copy',
                },
                stdoutFailure.summary || 'The activity recorded a failed command.'
              ),
              stdoutFailure.command
                ? h(
                    'p',
                    {
                      className: 'recovery-summary-command',
                      key: 'command',
                    },
                    `Failed command: ${stdoutFailure.command}`
                  )
                : null,
              renderFailureEvidenceDetails(
                stdoutFailure.excerpt,
                stdoutFailure.stdout_path,
                openPath,
                'stdout'
              ),
            ]
          ),
        ]
      ),
    ]
  );
}

function renderFailureEvidenceDetails(excerpt, stdoutPath, openPath, keyPrefix) {
  if (!excerpt && !stdoutPath) {
    return null;
  }

  return h(
    'details',
    {
      className: 'recovery-summary-details',
      key: `${keyPrefix}:details`,
    },
    [
      h('summary', { key: 'summary' }, 'Stdout evidence'),
      h(
        'div',
        {
          className: 'recovery-summary-details-body',
          key: 'body',
        },
        [
          stdoutPath
            ? h(
                'div',
                {
                  className: 'recovery-summary-path',
                  key: 'path',
                },
                [
                  h(
                    'span',
                    {
                      className: 'recovery-summary-label',
                      key: 'label',
                    },
                    'Artifact'
                  ),
                  h(
                    'span',
                    {
                      className: 'recovery-summary-path-value',
                      key: 'value',
                    },
                    renderPathValue(stdoutPath, openPath)
                  ),
                ]
              )
            : null,
          excerpt
            ? h(
                'pre',
                {
                  className: 'recovery-summary-pre',
                  key: 'excerpt',
                },
                excerpt
              )
            : null,
        ]
      ),
    ]
  );
}

function renderInspectorChip(text, key) {
  return h(
    'span',
    {
      className: 'inspector-chip',
      key,
    },
    text || '-'
  );
}

function renderActivityMetaSection(title, items, key) {
  const normalizedItems = items.map((item) =>
    Array.isArray(item) ? { label: item[0], value: item[1] } : item
  );

  return h(
    'section',
    {
      className: 'activity-meta-section',
      key,
    },
    [
      h(
        'h4',
        {
          className: 'activity-meta-section-title',
          key: 'title',
        },
        title
      ),
      h(
        'div',
        {
          className: 'activity-meta-list',
          key: 'list',
        },
        normalizedItems.map((item, index) =>
          h(
            'div',
            {
              className: 'activity-meta-row',
              key: `${key}:${index}`,
            },
            [
              h(
                'span',
                {
                  className: 'activity-meta-label',
                  key: 'label',
                },
                item.label
              ),
              renderActivityMetaValue(item, `${key}:${index}:value`),
            ]
          )
        )
      ),
    ]
  );
}

function renderActivityMetaValue(item, key) {
  return h(
    'span',
    {
      className: 'activity-meta-value',
      key,
    },
    renderPathValue(item.value, item.openPath)
  );
}

function compactObjectiveLabel(label) {
  if (typeof label !== 'string') {
    return label || '-';
  }

  return label.replace(/^OBJ-[A-Z0-9]+\s*[·-]\s*/i, '');
}

function renderArtifactPanel(title, content, emptyMessage, key) {
  return h(
    'section',
    {
      className: 'artifact-panel',
      key,
    },
    [
      h('h4', { key: 'title' }, title),
      content
        ? h(
            'pre',
            {
              className: 'prompt-body',
              key: 'content',
            },
            content
          )
        : h('p', { key: 'empty' }, emptyMessage),
    ]
  );
}

function renderKv(label, value, key, options = {}) {
  return h(
    'div',
    {
      className: 'kv-item',
      key,
    },
    [
      h('span', { className: 'kv-label', key: 'label' }, label),
      h(
        'span',
        { className: 'kv-value', key: 'value' },
        renderPathValue(value, options.openPath)
      ),
    ]
  );
}

function renderPathValue(value, openPath) {
  const text = typeof value === 'string' ? value : String(value ?? '-');
  if (!canOpenProjectPath(text) || typeof openPath !== 'function') {
    return text;
  }

  return h(
    'button',
    {
      className: 'path-link-button',
      onClick: () => void openPath(text),
      title: text,
      type: 'button',
    },
    text
  );
}

function canOpenProjectPath(value) {
  if (typeof value !== 'string') {
    return false;
  }

  const trimmed = value.trim();
  if (!trimmed || trimmed === '-' || trimmed === 'none') {
    return false;
  }

  if (trimmed.startsWith('/') || trimmed.startsWith('~')) {
    return false;
  }

  return trimmed.includes('/');
}

function flattenActivityGroups(groups) {
  if (!groups) {
    return [];
  }

  return []
    .concat(groups.active_planning || [])
    .concat(groups.active_tasks || [])
    .concat(groups.queued_tasks || [])
    .concat(groups.blocked_tasks || [])
    .concat(groups.interrupted_or_recovered || []);
}

function isActivityStillSelectable(payload, activityId) {
  if (!payload || typeof activityId !== 'string' || activityId.length === 0) {
    return false;
  }

  if (
    flattenActivityGroups(payload.activities).some(
      (activity) => activity && activity.activity_id === activityId
    )
  ) {
    return true;
  }

  return Array.isArray(payload.history)
    ? payload.history.some((entry) => entry && entry.activity_id === activityId)
    : false;
}

function groupActivitiesByObjective(groups) {
  const grouped = {};
  const seenActivityIds = new Set();

  for (const activity of flattenActivityGroups(groups)) {
    if (!activity || seenActivityIds.has(activity.activity_id)) {
      continue;
    }
    seenActivityIds.add(activity.activity_id);
    if (!grouped[activity.objective_id]) {
      grouped[activity.objective_id] = [];
    }
    grouped[activity.objective_id].push(activity);
  }

  return grouped;
}

function parseObjectiveStatusSummary(summary) {
  if (typeof summary !== 'string' || summary.trim().length === 0) {
    return [];
  }

  return summary
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const separatorIndex = item.lastIndexOf(':');
      if (separatorIndex === -1) {
        return {
          count: '',
          status: item,
        };
      }
      return {
        count: item.slice(separatorIndex + 1).trim(),
        status: item.slice(0, separatorIndex).trim().replace(/_/g, ' '),
      };
    });
}

function formatCounts(payload) {
  const entries = Object.entries(payload || {});
  if (entries.length === 0) {
    return 'none';
  }
  return entries.map(([key, value]) => `${key}:${value}`).join(', ');
}

function formatMs(value) {
  const numericValue = Number(value) || 0;
  if (numericValue < 1000) {
    return `${numericValue}ms`;
  }
  return `${(numericValue / 1000).toFixed(1)}s`;
}

function slugify(value) {
  return String(value || 'unknown')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function clampDetailRailWidth(width, viewportWidth = Infinity) {
  const numericWidth = Number.isFinite(width) ? width : DEFAULT_DETAIL_RAIL_WIDTH;
  const viewportCap = Number.isFinite(viewportWidth)
    ? Math.max(
        MIN_DETAIL_RAIL_WIDTH,
        Math.min(MAX_DETAIL_RAIL_WIDTH, Math.floor(viewportWidth / 2) - 32)
      )
    : MAX_DETAIL_RAIL_WIDTH;
  return Math.min(Math.max(numericWidth, MIN_DETAIL_RAIL_WIDTH), viewportCap);
}

function toDisplayMessage(error, fallbackMessage) {
  if (error && typeof error.message === 'string' && error.message.length > 0) {
    return error.message;
  }
  return fallbackMessage;
}

function createMonitorAppRoot(container, options = {}) {
  if (!container || typeof container !== 'object') {
    throw new TypeError('A container element is required to mount the monitor app.');
  }

  const root = createRoot(container);
  root.render(
    h(MonitorApp, {
      client: options.client,
      initialRunId: options.initialRunId,
    })
  );
  return {
    root,
    unmount() {
      root.unmount();
    },
  };
}

function readInitialRunId(initialRunId) {
  if (typeof initialRunId === 'string' && initialRunId.trim().length > 0) {
    return initialRunId.trim();
  }

  return readRunIdFromUrl();
}

function readRunIdFromUrl() {
  if (!globalThis.window || !window.location) {
    return null;
  }

  const value = new URLSearchParams(window.location.search).get('run');
  if (typeof value !== 'string' || value.trim().length === 0) {
    return null;
  }

  return value.trim();
}

function syncRunQueryParam(runId) {
  if (!globalThis.window || !window.history || !window.location) {
    return;
  }

  const url = new URL(window.location.href);
  if (runId) {
    url.searchParams.set('run', runId);
  } else {
    url.searchParams.delete('run');
  }
  window.history.replaceState({}, '', url.toString());
}

module.exports = {
  clampDetailRailWidth,
  isActivityStillSelectable,
  MonitorApp,
  createMonitorAppRoot,
  groupHistoryAttempts,
  groupHistoryRows,
  groupHistoryRowsByPhase,
  historyVariantForAttempt,
  historyRowKey,
  paginateHistoryPhaseGroups,
};
