// Verify host-visible artifacts through the owning DSH filesystem, without presenting them.
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { problem } from './host-operations.mjs';

export const REPORT_FORMATS = ['excel', 'markdown', 'bugs'];
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

async function bytesAt(fs, path, cwd, signal) {
  const entry = await fs.lstat(path, { cwd }, signal);
  if (entry?.type !== 'file') throw problem('file_unavailable', '宿主无法读取这个普通文件：' + path);
  const target = await fs.resolve(path, { cwd, signal });
  const info = await fs.stat(target, signal);
  if (info?.type !== 'file') throw problem('file_unavailable', '宿主文件已不可用：' + path);
  const bytes = await fs.readBytes(target, signal, info.size);
  return { bytes, path: fs.processPath(target) };
}

export async function reportFiles(receipt, { fs, cwd, signal }) {
  if (receipt.status !== 'ready') return { status: 'unavailable', presented: false };
  if (REPORT_FORMATS.some((format) => !receipt[format])) return { status: 'unavailable', presented: false,
    reason: '报告回执缺少 Excel、简报或 Bug 清单；不能把旧的部分文件当成本轮完整交付。' };
  if (!fs?.processPathFromHostPath) return { status: 'unavailable', presented: false, reason: '宿主未提供文件映射；可使用同源下载入口' };
  try {
    const files = [];
    for (const format of REPORT_FORMATS) {
      const mapped = fs.processPathFromHostPath(receipt[format]);
      if (!mapped) throw problem('file_unavailable', '报告路径不在当前宿主文件系统中');
      const actual = await bytesAt(fs, mapped, cwd, signal);
      const prefix = format === 'excel' ? '' : format + '_';
      if (digest(actual.bytes) !== receipt[prefix + 'sha256']) throw problem('file_changed', '宿主读取的报告与本次报告校验值不符');
      files.push({ path: actual.path, description: { excel: '本次测试用例与真实结果', markdown: '本次测试简报', bugs: '本轮测试 Bug 清单' }[format] });
    }
    return { status: 'verified', presented: false, present_input: { files },
      note: '文件已核验；需由当前 Agent 在打开的 turn 中调用实际 present 工具，尚未声明交付。' };
  } catch (error) {
    if (signal?.aborted) throw error;
    return { status: 'unavailable', presented: false, reason: String(error.message ?? error) };
  }
}

export async function prepareSources(payload, { fs, cwd, folder, signal }) {
  const result = structuredClone(payload);
  for (const group of ['sources', 'artifacts']) {
    for (const item of result.spec?.[group] ?? []) {
      if (typeof item.file !== 'string') continue;
      if (!fs) throw problem('file_unavailable', '宿主未提供文件读取能力；可以使用来源 text 或原 CLI 文件输入');
      const original = item.file;
      const loaded = await bytesAt(fs, original, cwd, signal);
      const directory = join(folder, digest(loaded.bytes));
      await mkdir(directory, { recursive: true });
      const destination = join(directory, basename(loaded.path));
      await writeFile(destination, loaded.bytes);
      item.uri ??= original;
      item.file = destination;
    }
  }
  return { ...result, base_dir: cwd };
}
