// Each document gets a fresh sample workspace. The hash router shares it across views.
import { initCases, restoreCaseStates } from './cases-api.js';
import { restoreRunVerdicts, seedLoadRuns } from './runs-api.js';
import { initSchedules, validateTasks } from './schedules-api.js';

let data;
let revision = 0;
const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export function startSession(fixtures) {
  const settings = structuredClone(fixtures['/settings']);
  settings.legacy_dashboard_import_eligible = false;
  const runs = fixtures['/runs'].items.map((entry) => structuredClone(fixtures['/runs/' + entry.id] || entry));
  data = { settings, runs };
  initCases(data, fixtures);
  seedLoadRuns(data, fixtures);
  initSchedules(data, fixtures);
  revision = 1;
}

export const currentData = () => data;
export const getData = () => structuredClone(data);
export const getRevision = () => String(revision);

export function commitData(next) {
  data = next;
  revision++;
}

function safeFields(value) {
  if (value && typeof value === 'object') {
    for (const [key, field] of Object.entries(value)) {
      if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('备份中含有无效字段');
      safeFields(field);
    }
  }
}

export function restoreData(saved) {
  safeFields(saved);
  const library = saved?.caseLibrary;
  if (!record(saved) || !record(library) || !Array.isArray(library.projects) || !Array.isArray(library.items)
      || !record(library.details) || !record(library.definitions) || !record(library.folders) || !Array.isArray(library.secrets)
      || !Array.isArray(saved.runs) || !record(saved.settings?.browser) || !record(saved.settings.dashboard)) {
    throw new Error('备份缺少项目、用例、运行或设置信息');
  }
  const ids = new Set(library.projects.map((item) => item.id));
  if (ids.has(undefined) || ids.size !== library.projects.length || library.projects.some((item) => !Array.isArray(library.folders[item.id]?.folders))
      || library.items.some((item) => !ids.has(item.project_id) || !library.details[item.project_id + '/' + item.case_id]
        || !library.definitions[item.project_id + '/' + item.case_id]?.case)
      || saved.runs.some((run) => !run.id || !Array.isArray(run.results))) throw new Error('备份中的用例或运行数据不完整');
  validateTasks(saved.tasks, ids);
  const next = structuredClone(saved);
  const changedProjects = new Set();
  for (const run of next.runs) {
    if (restoreRunVerdicts(run)) changedProjects.add(run.project_id);
  }
  restoreCaseStates(next, changedProjects);
  commitData(next);
}
