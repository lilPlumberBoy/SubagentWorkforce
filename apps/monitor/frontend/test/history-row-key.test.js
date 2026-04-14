const test = require('node:test');
const assert = require('node:assert/strict');

const {
  clampDetailRailWidth,
  groupHistoryAttempts,
  groupHistoryRows,
  groupHistoryRowsByPhase,
  historyVariantForAttempt,
  historyRowKey,
  isActivityStillSelectable,
  paginateHistoryPhaseGroups,
} = require('../src/monitor/app');

test('historyRowKey stays unique when activity history shares a timestamp', () => {
  const row = {
    activity_id: 'middleware-design-integration-boundary-reconciliation',
    attempt: 1,
    status: 'recovered',
    timestamp: '2026-04-13T17:31:49Z',
  };

  const firstKey = historyRowKey(row, 0);
  const secondKey = historyRowKey(row, 1);

  assert.notEqual(firstKey, secondKey);
  assert.match(firstKey, /middleware-design-integration-boundary-reconciliation/);
  assert.match(firstKey, /2026-04-13T17:31:49Z/);
});

test('groupHistoryRows keeps one parent activity with chronological child entries', () => {
  const groups = groupHistoryRows([
    {
      activity_id: 'TSK-1',
      attempt: 2,
      current_activity: 'Recovered execution finished.',
      label: 'TSK-1 · backend scope reconciliation [attempt 2]',
      objective_label: 'OBJ-123 · Backend objective',
      status: 'recovered',
      timestamp: '2026-04-13T19:28:07Z',
    },
    {
      activity_id: 'TSK-1',
      attempt: 1,
      current_activity: 'Initial execution blocked on dependency.',
      label: 'TSK-1 · backend scope reconciliation [attempt 1]',
      objective_label: 'OBJ-123 · Backend objective',
      status: 'blocked',
      timestamp: '2026-04-13T19:20:00Z',
    },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].label, 'TSK-1 · backend scope reconciliation');
  assert.equal(groups[0].entries.length, 2);
  assert.equal(groups[0].entries[0].status, 'blocked');
  assert.equal(groups[0].entries[1].status, 'recovered');
});

test('groupHistoryRowsByPhase groups activities under their phase', () => {
  const phaseGroups = groupHistoryRowsByPhase([
    {
      activity_id: 'TSK-2',
      attempt: 1,
      label: 'TSK-2 · polish refinement [attempt 1]',
      objective_label: 'OBJ-2 · Polish objective',
      phase: 'polish',
      status: 'completed',
      timestamp: '2026-04-13T20:10:00Z',
    },
    {
      activity_id: 'TSK-1',
      attempt: 1,
      label: 'TSK-1 · discovery scope [attempt 1]',
      objective_label: 'OBJ-1 · Discovery objective',
      phase: 'discovery',
      status: 'completed',
      timestamp: '2026-04-13T20:00:00Z',
    },
  ]);

  assert.equal(phaseGroups.length, 2);
  assert.equal(phaseGroups[0].phase, 'polish');
  assert.equal(phaseGroups[0].groups.length, 1);
  assert.equal(phaseGroups[1].phase, 'discovery');
});

test('groupHistoryAttempts synthesizes the missing initial attempt before a repair attempt', () => {
  const attemptGroups = groupHistoryAttempts([
    {
      activity_id: 'TSK-3',
      attempt: 2,
      recovery_action: 'recreated_workspace',
      status: 'recovered',
      timestamp: '2026-04-13T20:20:00Z',
    },
    {
      activity_id: 'TSK-3',
      attempt: 2,
      recovery_action: 'recreated_workspace',
      status: 'ready_for_bundle_review',
      timestamp: '2026-04-13T20:21:00Z',
    },
  ]);

  assert.equal(attemptGroups.length, 2);
  assert.equal(attemptGroups[0].attempt, 1);
  assert.equal(attemptGroups[0].entries[0].synthetic, true);
  assert.equal(attemptGroups[1].attempt, 2);
  assert.equal(attemptGroups[1].entries[0].status, 'recovered');
});

test('historyVariantForAttempt uses original for initial attempt when repairs exist', () => {
  const attemptGroups = [{ attempt: 1 }, { attempt: 2 }];

  assert.equal(historyVariantForAttempt(attemptGroups[0], attemptGroups), 'original');
  assert.equal(historyVariantForAttempt(attemptGroups[1], attemptGroups), 'current');
});

test('paginateHistoryPhaseGroups pages activity groups while preserving phase sections', () => {
  const pagination = paginateHistoryPhaseGroups(
    [
      {
        phase: 'mvp-build',
        groups: [{ activity_id: 'a' }, { activity_id: 'b' }],
      },
      {
        phase: 'design',
        groups: [{ activity_id: 'c' }, { activity_id: 'd' }],
      },
    ],
    2,
    2
  );

  assert.equal(pagination.page, 2);
  assert.equal(pagination.totalPages, 2);
  assert.equal(pagination.phaseGroups.length, 1);
  assert.equal(pagination.phaseGroups[0].phase, 'design');
  assert.equal(pagination.phaseGroups[0].groups.length, 2);
});

test('clampDetailRailWidth keeps the detail rail within the supported desktop range', () => {
  assert.equal(clampDetailRailWidth(120, 1600), 320);
  assert.equal(clampDetailRailWidth(880, 1600), 760);
  assert.equal(clampDetailRailWidth(700, 900), 418);
});

test('isActivityStillSelectable keeps history-selected tasks active across dashboard refreshes', () => {
  const payload = {
    activities: {
      active_tasks: [{ activity_id: 'TSK-A' }],
    },
    history: [{ activity_id: 'TSK-HISTORY' }],
  };

  assert.equal(isActivityStillSelectable(payload, 'TSK-A'), true);
  assert.equal(isActivityStillSelectable(payload, 'TSK-HISTORY'), true);
  assert.equal(isActivityStillSelectable(payload, 'TSK-MISSING'), false);
});
