// Dashboard preferences belong to the active data root; browser storage is only a legacy import source.
import { api } from './core.js';

const LEGACY_CLAIM = 'autoskill.dashboard.legacy-data-id';
const LEGACY_KEYS = { theme: 'autoskill.theme', engine: 'autoskill.engine', headed: 'autoskill.headed' };
const listeners = new Set();
const valid = {
  theme: (value) => value === 'light' || value === 'dark',
  engine: (value) => value === 'playwright' || value === 'selenium',
  headed: (value) => typeof value === 'boolean',
};
const defaults = () => ({ theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light', engine: 'playwright', headed: true });
let current = { dataId: null, values: defaults() };
let loading = null;
let saving = Promise.resolve();

function stored(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function legacyValues() {
  const values = {};
  for (const [key, storageKey] of Object.entries(LEGACY_KEYS)) {
    const raw = stored(storageKey);
    const value = key === 'headed' ? (raw === '1' ? true : raw === '0' ? false : null) : raw;
    if (valid[key](value)) values[key] = value;
  }
  return values;
}

function publish(data) {
  if (typeof data?.data_id !== 'string' || !data.data_id) throw new Error('服务未返回当前数据集标识，偏好尚未保存');
  const values = defaults();
  for (const key of Object.keys(valid)) if (valid[key](data.dashboard?.[key])) values[key] = data.dashboard[key];
  const next = { dataId: data.data_id, values };
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  current = next;
  for (const listener of listeners) listener();
}

async function readPreferences() {
  let data = await api('/settings');
  if (typeof data?.data_id !== 'string' || !data.data_id) throw new Error('服务未返回当前数据集标识，偏好尚未加载');
  const claimed = stored(LEGACY_CLAIM);
  if (data.legacy_dashboard_import_eligible === true) {
    const legacy = !claimed || claimed === data.data_id ? legacyValues() : {};
    const missing = Object.fromEntries(Object.entries(legacy).filter(([key]) => data.dashboard?.[key] === undefined));
    data = await api('/settings', { method: 'PUT', body: { dashboard: missing, expected_data_id: data.data_id, legacy_dashboard_import: true } });
    if (!claimed || claimed === data.data_id) {
      try { localStorage.setItem(LEGACY_CLAIM, data.data_id); } catch { /* The server also consumes the import claim atomically. */ }
    }
  }
  publish(data);
  return current.values;
}

export function loadPreferences() {
  if (loading) return loading;
  loading = readPreferences().finally(() => { loading = null; });
  return loading;
}

export function onPreferences(listener) { listeners.add(listener); return () => listeners.delete(listener); }
export function preferenceDataId() { return current.dataId; }
export function preferredTheme() { return current.values.theme; }
export function preferredEngine() { return current.values.engine; }
export function preferredHeaded() { return current.values.headed; }

function savePreference(key, value) {
  if (!valid[key](value)) return Promise.reject(new Error('偏好值无效'));
  const intendedDataId = current.dataId;
  const action = saving.catch(() => undefined).then(async () => {
    if (!current.dataId) await loadPreferences();
    if (intendedDataId && intendedDataId !== current.dataId) throw new Error('数据目录已变化，请在当前数据目录重新选择偏好');
    const data = await api('/settings', { method: 'PUT', body: { dashboard: { [key]: value }, expected_data_id: current.dataId } });
    publish(data);
    return current.values;
  });
  saving = action;
  return action;
}

export const setTheme = (theme) => savePreference('theme', theme);
export const setEngine = (engine) => savePreference('engine', engine);
export const setHeaded = (headed) => savePreference('headed', headed);
