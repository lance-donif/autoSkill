export const verdictStatus = Object.freeze({ pass: 'Pass', fail: 'Fail', not_run: 'NotRun' });
const records = (items) => Array.isArray(items) ? items.filter((item) => item && typeof item === 'object') : [];
const failed = (item) => ['business_status', 'performance_status', 'quality_status'].some((key) => item[key] === 'Fail');

function legacyOutcome(status, detail, state, duration) {
  const checks = [...records(detail.assertions), ...records(detail.performance_assertions),
    ...records(detail.turns).flatMap((turn) => records(turn.performance_assertions))];
  if (failed(detail) || records(detail.attempts).some(failed) || checks.some((check) => check.passed === false)
      || Object.values(state?.checks || {}).includes('fail')) return 'fail';
  const raw = String(status || '').toLowerCase();
  if (['notrun', 'not_run', 'pending', 'queued'].includes(raw)) return 'not_run';
  if (['blocked', 'cancelled', 'canceled', 'skipped'].includes(raw)) {
    const setup = records(detail.setup_cases).length;
    const started = detail.target_started ?? (records(detail.steps).length > setup || !!detail.request || !!detail.response
      || records(detail.turns).length > 0 || records(detail.assertions).length > 0 || (!setup && (detail.duration_ms || duration) > 0));
    return started ? 'fail' : 'not_run';
  }
  return raw === 'pass' && state?.evidence === 'valid'
    && (detail.business_status === 'Pass' || detail.execution_only_pass === true) ? 'pass' : 'fail';
}

export function resultOutcome(row) {
  if (row.state?.outcome === 'pass' && !row.simulated && row.state.evidence !== 'valid') return 'fail';
  if (Object.hasOwn(verdictStatus, row.state?.outcome)) return row.state.outcome;
  return legacyOutcome(row.status, row.detail || {}, row.state, row.duration_ms);
}

export function restoreResult(row) {
  const outcome = resultOutcome(row);
  const changed = outcome !== row.state?.outcome;
  if (changed) row.state = { ...row.state, outcome };
  if (changed || row.history_status != null) row.history_status = verdictStatus[outcome];
  for (const attempt of records(row.detail?.attempts)) {
    if (!Object.hasOwn(verdictStatus, attempt.outcome)) {
      const evidence = attempt.path && attempt.sha256 ? row.state.evidence : null;
      attempt.outcome = legacyOutcome(attempt.status, attempt, { evidence }, attempt.duration_ms);
    }
  }
  return changed;
}
