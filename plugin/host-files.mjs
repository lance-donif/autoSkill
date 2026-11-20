// Verify host-visible artifacts through the owning DSH filesystem, without presenting them.
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, open, rename, rm } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { problem } from './host-operations.mjs';

export const REPORT_FORMATS = ['excel', 'markdown', 'bugs'];
const descriptions = { initial_excel: '本任务首次交付的 14 列用例设计', excel: '本次测试用例与真实结果', markdown: '本次测试简报', bugs: '本轮测试 Bug 清单' };
const READ_WINDOW = 1024 * 1024;

async function* chunksAt(fs, target, size, signal) {
  if (typeof fs.readByteRange !== 'function') {
    yield await fs.readBytes(target, signal, size);
    return;
  }
  for (let offset = 0; ; offset += READ_WINDOW) {
    signal?.throwIfAborted();
    const length = size === undefined ? READ_WINDOW : Math.min(READ_WINDOW, size - offset + 1);
    const bytes = await fs.readByteRange(target, { offset, length }, signal);
    if (bytes.byteLength > length) throw problem('file_changed', '宿主返回的文件片段超过请求范围');
    yield bytes;
    if (bytes.byteLength < length) return;
  }
}

async function hashHostFile(path, { fs, cwd, signal }, output) {
  signal?.throwIfAborted();
  const entry = await fs.lstat(path, { cwd }, signal);
  if (entry?.type !== 'file') throw problem('file_unavailable', '宿主无法读取这个普通文件：' + path);
  const target = await fs.resolve(path, { cwd, signal });
  const info = await fs.stat(target, signal);
  if (info?.type !== 'file') throw problem('file_unavailable', '宿主文件已不可用：' + path);
  const digest = createHash('sha256');
  let size = 0;
  for await (const bytes of chunksAt(fs, target, info.size, signal)) {
    signal?.throwIfAborted();
    size += bytes.byteLength;
    if (info.size !== undefined && size > info.size) throw problem('file_changed', '读取期间宿主文件已增长：' + path);
    digest.update(bytes);
    if (output) await output.writeFile(bytes);
  }
  const after = await fs.stat(target, signal);
  signal?.throwIfAborted();
  if (after?.type !== 'file' || after.version !== info.version || (info.size !== undefined && size !== info.size) || (after.size !== undefined && size !== after.size)) {
    throw problem('file_changed', '读取期间宿主文件已变化：' + path);
  }
  return { sha256: digest.digest('hex'), path: fs.processPath(target) };
}

async function verifiedReportFile(receipt, format, context) {
  const mapped = context.fs.processPathFromHostPath(receipt[format]);
  if (!mapped) throw problem('file_unavailable', '报告路径不在当前宿主文件系统中');
  const actual = await hashHostFile(mapped, context);
  const checksum = format === 'initial_excel' || format === 'excel' ? 'sha256' : format + '_sha256';
  if (actual.sha256 !== receipt[checksum]) throw problem('file_changed', '宿主读取的报告与本次报告校验值不符');
  return { path: actual.path, description: descriptions[format] };
}

export async function reportFiles(receipt, context, { delivery = 'final', task_id: taskId } = {}) {
  if (delivery !== 'initial' && delivery !== 'final') throw problem('validation_error', '报告交付阶段需要是 initial 或 final');
  if (taskId !== undefined && receipt.task_id !== taskId) throw problem('validation_error', '报告与指定任务编号不匹配');
  if (receipt.status !== 'ready') return { status: 'unavailable', presented: false };
  const initial = delivery === 'initial';
  if (initial && (receipt.kind !== 'design' || !receipt.task_id || !receipt.initial_excel || receipt.initial_excel === receipt.delivery_excel)) return { status: 'unavailable', presented: false,
    reason: '首版交付需要 plan write 返回的初始设计报告编号和冻结的 initial_excel。' };
  const formats = initial ? ['initial_excel'] : REPORT_FORMATS;
  if (formats.some((format) => !receipt[format])) return { status: 'unavailable', presented: false,
    reason: '报告回执缺少 Excel、简报或 Bug 清单；不能把旧的部分文件当成本轮完整交付。' };
  if (!context.fs?.processPathFromHostPath) return { status: 'unavailable', presented: false, reason: '宿主未提供文件映射；可使用同源下载入口' };
  try {
    const files = [];
    for (const format of formats) files.push(await verifiedReportFile(receipt, format, context));
    return { status: 'verified', presented: false, present_input: { files },
      note: '文件已核验；需由当前 Agent 在打开的 turn 中调用实际 present 工具，尚未声明交付。' };
  } catch (error) {
    if (context.signal?.aborted) throw error;
    return { status: 'unavailable', presented: false, reason: String(error.message ?? error) };
  }
}

async function stageSource(path, context) {
  const { folder, signal } = context;
  await mkdir(folder, { recursive: true });
  const temporary = join(folder, randomUUID() + '.tmp');
  const output = await open(temporary, 'wx');
  try {
    let loaded;
    try { loaded = await hashHostFile(path, context, output); }
    finally { await output.close(); }
    const directory = join(folder, loaded.sha256);
    const destination = join(directory, basename(loaded.path));
    await mkdir(directory, { recursive: true });
    signal?.throwIfAborted();
    await rename(temporary, destination);
    return destination;
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
}

export async function prepareSources(payload, context) {
  const result = structuredClone(payload);
  for (const group of ['sources', 'artifacts']) {
    for (const item of result.spec?.[group] ?? []) {
      if (typeof item.file !== 'string') continue;
      if (!context.fs) throw problem('file_unavailable', '宿主未提供文件读取能力；可以使用来源 text 或原 CLI 文件输入');
      const original = item.file;
      item.file = await stageSource(original, context);
      item.uri ??= original;
    }
  }
  return { ...result, base_dir: context.cwd };
}
