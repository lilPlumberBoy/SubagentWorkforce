const DEFAULT_BASE_URL =
  (globalThis.process &&
    globalThis.process.env &&
    globalThis.process.env.MONITOR_API_BASE_URL) ||
  '';

function createMonitorApiClient(options = {}) {
  const baseUrl = normalizeBaseUrl(options.baseUrl || DEFAULT_BASE_URL);

  return {
    getActivityDetail(runId, activityId, options = {}) {
      return fetchJson(
        baseUrl,
        `/api/runs/${encodeURIComponent(runId)}/activities/${encodeURIComponent(
          activityId
        )}?limit=${options.limit || 20}`,
        options
      );
    },
    getPromptDebug(runId, activityId, options = {}) {
      const variantQuery =
        typeof options.variant === 'string' && options.variant.trim().length > 0
          ? `?variant=${encodeURIComponent(options.variant.trim())}`
          : '';
      return fetchJson(
        baseUrl,
        `/api/runs/${encodeURIComponent(runId)}/activities/${encodeURIComponent(
          activityId
        )}/prompt-debug${variantQuery}`,
        options
      );
    },
    getRunDashboard(runId, options = {}) {
      return fetchJson(
        baseUrl,
        `/api/runs/${encodeURIComponent(runId)}/dashboard?limit=${
          options.limit || 50
        }`,
        options
      );
    },
    openFile(path, options = {}) {
      return fetchJson(baseUrl, '/api/open-file', {
        ...options,
        body: { path },
        method: 'POST',
      });
    },
    listRuns(options = {}) {
      return fetchJson(baseUrl, '/api/runs', options);
    },
  };
}

async function fetchJson(baseUrl, path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    body:
      options.body && typeof options.body === 'object'
        ? JSON.stringify(options.body)
        : undefined,
    headers: {
      accept: 'application/json',
      ...(options.body ? { 'content-type': 'application/json' } : {}),
    },
    method: options.method || 'GET',
    signal: options.signal,
  });

  let payload = null;

  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    throw new Error(
      payload && typeof payload.error === 'string'
        ? payload.error
        : `Request failed with status ${response.status}.`
    );
  }

  return payload;
}

function normalizeBaseUrl(baseUrl) {
  if (typeof baseUrl !== 'string' || baseUrl.trim().length === 0) {
    return '';
  }

  return baseUrl.replace(/\/+$/, '');
}

module.exports = {
  createMonitorApiClient,
};
