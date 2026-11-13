// This cache records transport observations; it never schedules or owns business work.
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

function ordered(value) {
  if (Array.isArray(value)) return value.map(ordered);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])]));
}

export const hash = (value) => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(ordered(value))).digest('hex');

export class HostJournal {
  constructor(workspace) {
    this.folder = join(workspace, '.local', 'host-operations', 'dsh');
    this.pending = new Map();
  }

  path(hostSessionId, requestId) {
    return join(this.folder, hash([hostSessionId, requestId]) + '.json');
  }

  async get(hostSessionId, requestId) {
    try { return JSON.parse(await readFile(this.path(hostSessionId, requestId), 'utf8')); }
    catch (error) {
      if (error.code === 'ENOENT') return null;
      throw error;
    }
  }

  async save(hostSessionId, requestId, value) {
    await mkdir(this.folder, { recursive: true });
    const file = this.path(hostSessionId, requestId);
    const temporary = file + '.' + randomUUID() + '.tmp';
    await writeFile(temporary, JSON.stringify(value), { encoding: 'utf8', flag: 'wx' });
    await rename(temporary, file);
    return value;
  }

  async serial(hostSessionId, requestId, action) {
    const key = hash([hostSessionId, requestId]);
    const before = this.pending.get(key);
    const promise = (before ? before.catch(() => undefined) : Promise.resolve()).then(action);
    this.pending.set(key, promise);
    try { return await promise; }
    finally { if (this.pending.get(key) === promise) this.pending.delete(key); }
  }
}
