const RECORD_BUDGET = 50000;
const number = (value) => Number.isSafeInteger(value) && value > 0;
const seconds = (value) => Number.isFinite(value) && value > 0 && Number.isSafeInteger(Math.round(value * 1000)) && Math.round(value * 1000) > 0;

export function loadConfiguration(body) {
  if (!body.mode) {
    const { concurrency, iterations, ramp_up_seconds } = body;
    if (![concurrency, iterations].every(number) || !Number.isFinite(ramp_up_seconds) || ramp_up_seconds < 0) throw new Error('压测参数无效');
    return { concurrency, iterations, ramp_up_seconds };
  }
  if (body.mode === 'performance') {
    const { concurrency, duration_seconds, target_requests } = body;
    if (!number(concurrency)) throw new Error('并发数必须为正整数');
    if (duration_seconds == null && target_requests == null) throw new Error('请填写持续时长或目标请求数');
    if (duration_seconds != null && !seconds(duration_seconds)) throw new Error('持续时长必须大于零');
    if (target_requests != null && !number(target_requests)) throw new Error('目标请求数必须为正整数');
    return { mode: body.mode, driver: 'locust', concurrency, ...(duration_seconds != null ? { duration_seconds } : {}), ...(target_requests != null ? { target_requests } : {}) };
  }
  if (body.mode !== 'stress') throw new Error('不支持的负载测试模式');
  const { initial_concurrency, step_concurrency, max_concurrency, step_duration_seconds } = body;
  if (![initial_concurrency, step_concurrency, max_concurrency].every(number) || max_concurrency < initial_concurrency) {
    throw new Error('并发数和递增量必须为正整数，最大并发不能小于初始并发');
  }
  if (!seconds(step_duration_seconds)) throw new Error('每阶段持续时长必须大于零');
  return { mode: body.mode, driver: 'locust', initial_concurrency, step_concurrency, max_concurrency, step_duration_seconds };
}

function capacityError() {
  return new Error('浏览器模拟的完整报告最多可展示 ' + RECORD_BUDGET + ' 条样本，请缩短时长或减少请求数、并发数');
}

export function mockSchedule(load, plans) {
  const measured = plans.flatMap((plan, index) => plan.outcome === 'not_run' ? [] : [index]);
  let targetSlots = Infinity;
  if (load.target_requests != null && measured.length) {
    const beforeLast = load.target_requests - 1;
    targetSlots = Math.floor(beforeLast / measured.length) * plans.length + measured[beforeLast % measured.length] + 1;
  }
  const stress = load.mode === 'stress';
  const count = stress ? Math.ceil((load.max_concurrency - load.initial_concurrency) / load.step_concurrency) + 1 : 1;
  if (count > RECORD_BUDGET) throw capacityError();
  const stages = [];
  let total = 0;
  let end = 0;
  let stopReason = stress ? 'stages_completed' : 'duration';
  for (let i = 0; i < count; i++) {
    const concurrency = stress ? Math.min(load.max_concurrency, load.initial_concurrency + i * load.step_concurrency) : load.concurrency;
    const limit = stress ? load.step_duration_seconds : load.duration_seconds;
    const ms = limit == null ? Infinity : Math.round(limit * 1000);
    const slots = Math.ceil(ms / 1000) * concurrency;
    const requests = Number.isFinite(slots) || measured.length ? Math.min(slots, targetSlots) : plans.length;
    if (!Number.isSafeInteger(requests) || total + requests > RECORD_BUDGET) throw capacityError();
    const waves = Math.ceil(requests / concurrency);
    const duration = Math.min(waves * 1000, ms);
    if (!stress && load.target_requests != null && requests === targetSlots) stopReason = 'target_requests';
    if (!measured.length && !Number.isFinite(slots)) stopReason = 'no_samples';
    stages.push({ index: i + 1, concurrency, offset: end, end: end + duration, first: total, count: requests });
    end += duration;
    total += requests;
  }
  return { stages, total, duration: end, stop_reason: stopReason };
}

export function samplesDone(schedule, elapsed) {
  if (elapsed >= schedule.duration) return schedule.total;
  for (const stage of schedule.stages) {
    if (elapsed < stage.end) return Math.min(stage.first + stage.count,
      stage.first + Math.floor(Math.max(0, elapsed - stage.offset) / 1000) * stage.concurrency);
  }
  return 0;
}

export function sampleTiming(run, index) {
  const schedule = run.load_schedule;
  let left = 0;
  let right = schedule.stages.length - 1;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    const stage = schedule.stages[mid];
    if (index >= stage.first + stage.count) left = mid + 1;
    else right = mid;
  }
  const stage = schedule.stages[left];
  const local = index - stage.first;
  const wave = stage.offset + Math.floor(local / stage.concurrency) * 1000;
  const end = Math.min(wave + 1000, stage.end);
  const caseId = run.load_templates[index % run.load_templates.length].case_id;
  const latencyCheck = run.simulation_cases[caseId].assertions?.find((check) => check.scope === 'performance');
  const latency = Math.min(end - wave, latencyCheck?.actual ?? 94 + ((index * 37 + stage.index * 23) % 253) + Math.min(350, stage.concurrency * 3));
  return { stage: stage.index, user: local % stage.concurrency + 1, end, latency };
}

function sample(run, index, base) {
  const row = run.load_templates[index % run.load_templates.length];
  const plan = run.simulation_cases[row.case_id];
  if (plan.outcome === 'not_run') return null;
  const timing = sampleTiming(run, index);
  const cancelled = index === run.cancelled_index;
  const end = cancelled ? run.mock_elapsed_ms : timing.end;
  const start = cancelled ? Math.min(end, Math.max(0, timing.end - timing.latency)) : end - timing.latency;
  const result = { label: row.case_id, kind: row.kind, start_ms: base + start, elapsed_ms: end - start,
    success: !cancelled && plan.outcome === 'pass' };
  if (run.load.mode === 'stress') result.stage = timing.stage;
  if (!result.success) result.error = cancelled ? '模拟执行已取消' : plan.reason;
  if (!cancelled && plan.response) {
    result.code = String(plan.response.status);
    if (Number.isFinite(plan.response.bytes)) result.received_bytes = plan.response.bytes;
  }
  return result;
}

function virtualUsers(schedule, base, elapsed, finished) {
  const points = [];
  for (const stage of schedule.stages) {
    if (elapsed <= stage.offset) break;
    const count = Math.min(stage.concurrency, stage.count);
    points.push({ time_ms: base + stage.offset, count });
    const last = Math.min(elapsed, stage.end) - 1;
    if (last > stage.offset) points.push({ time_ms: base + last, count });
  }
  if (finished && points.length) points.push({ time_ms: base + elapsed, count: 0 });
  return points;
}

export function mockLoadReport(run) {
  const schedule = run.load_schedule;
  const base = Date.parse(run.started_at || run.created_at);
  const elapsed = run.mock_elapsed_ms || 0;
  const samples = [];
  for (let index = 0; index < run.done; index++) {
    const observation = sample(run, index, base);
    if (observation) samples.push(observation);
  }
  const stages = run.load.mode === 'stress' ? schedule.stages.filter((stage) => stage.offset < elapsed).map((stage) => ({
    index: stage.index, concurrency: stage.concurrency, start_ms: base + stage.offset, end_ms: base + Math.min(elapsed, stage.end),
  })) : [];
  return { simulated: true, granularity_ms: 1000, thread_events: true, clock: '浏览器加速模拟 · 每位虚拟用户每模拟秒调度一次',
    samples, threads: virtualUsers(schedule, base, elapsed, !['Queued', 'Running'].includes(run.status)), stages,
    stop_reason: run.status === 'Cancelled' ? 'cancelled' : run.done === run.total ? schedule.stop_reason : null };
}

export function mockLoadMetrics(run) {
  const { samples } = mockLoadReport(run);
  const times = samples.map((s) => s.elapsed_ms).sort((a, b) => a - b);
  const quantile = (p) => times[Math.ceil(times.length * p) - 1] ?? null;
  return { latency_samples: samples.length, request_count: samples.filter((s) => s.kind === 'api').length,
    throughput_per_second: run.mock_elapsed_ms ? samples.length * 1000 / run.mock_elapsed_ms : null,
    error_rate: samples.length ? samples.filter((s) => !s.success).length / samples.length : null,
    p50_ms: quantile(0.5), p95_ms: quantile(0.95), p99_ms: quantile(0.99) };
}

export function legacyLoadReport(run) {
  const samples = [];
  for (const row of run.results || []) {
    const start = Date.parse(row.started_at);
    const outcome = row.state?.outcome;
    if (row.prerequisite || !['pass', 'fail'].includes(outcome) || !Number.isFinite(start)
        || !Number.isFinite(row.duration_ms) || row.duration_ms < 0) continue;
    const sample = { label: row.case_id, kind: row.kind, start_ms: start, elapsed_ms: row.duration_ms,
      success: outcome === 'pass' };
    if (!sample.success && row.reason) sample.error = row.reason;
    const response = row.detail?.response;
    if (row.kind === 'api' && response) {
      if (response.status != null) sample.code = String(response.status);
      if (Number.isFinite(response.bytes)) sample.received_bytes = response.bytes;
    }
    samples.push(sample);
  }
  return { samples, threads: [], stages: [], simulated: Boolean(run.simulated) };
}
