// Download only verified HTTP responses and retain failures while the surrounding page refreshes.
import { h, icon, api, refresh } from '../core.js';

const feedback = new Map();
const receipts = new Map();
const pending = new Set();

function beginFeedback(key, text) {
  const base = key.slice(0, key.lastIndexOf(':') + 1);
  for (const previous of feedback.keys()) {
    if (previous.startsWith(base) && !pending.has(previous)) feedback.delete(previous);
  }
  feedback.set(key, { text });
}

export function setReportFeedback(run, kind, text, bad = false) {
  const key = (run.test_report?.report_id || run.id) + ':' + kind;
  beginFeedback(key, text);
  feedback.set(key, { text, bad });
  refresh();
}

async function checkedBlob(url, kind) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `文件请求失败（HTTP ${response.status}）`;
    try { const error = await response.json(); if (typeof error.detail === 'string') message = error.detail; } catch { /* Retain the HTTP failure. */ }
    throw new Error(message);
  }
  const blob = await response.blob();
  if (!blob.size) throw new Error('下载的文件为空');
  const contentType = response.headers.get('content-type') || '';
  if (kind === 'excel') {
    const signature = new Uint8Array(await blob.slice(0, 2).arrayBuffer());
    if (signature[0] !== 80 || signature[1] !== 75) throw new Error('服务端未返回有效的 Excel 文件');
  } else if (kind !== 'native' && /application\/json|text\/html/i.test(contentType)) throw new Error('服务端返回了错误页面，未开始下载报告');
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  let filename = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  if (encoded) { try { filename = decodeURIComponent(encoded); } catch { /* Use the fallback name. */ } }
  return { blob, filename };
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = h('a', { href: url, download: filename });
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function reloadReceipt(reportId) {
  if (!reportId) return;
  try { receipts.set(reportId, await api('/task-reports/' + encodeURIComponent(reportId))); }
  catch { /* Keep the specific download error and the previous recovery capability. */ }
}

export function reportDownloadLink({ key, url, label, kind = 'native', filename, reportId }) {
  if (!url) return null;
  return h('a', { class: 'btn', href: url, download: '', 'aria-disabled': pending.has(key) ? 'true' : null, onClick: async (event) => {
    event.preventDefault();
    if (pending.has(key)) return;
    beginFeedback(key, '正在读取文件…'); pending.add(key); refresh();
    try {
      const file = await checkedBlob(url, kind);
      saveBlob(file.blob, file.filename || filename || 'autoSkill-report');
      feedback.set(key, { text: '文件已取回，浏览器下载已开始；请在下载列表查看。' });
    } catch (error) {
      feedback.set(key, { text: `下载失败：${error.message} 可再次下载或按下方状态重试导出。`, bad: true });
      await reloadReceipt(reportId);
    } finally { pending.delete(key); refresh(); }
  } }, icon('table'), pending.has(key) ? '正在读取文件…' : label);
}

function retryButton(receipt) {
  const key = receipt.report_id + ':retry';
  return h('button', { class: 'btn', disabled: pending.has(key), onClick: async () => {
    if (pending.has(key)) return;
    beginFeedback(key, '正在重新导出本次报告，不会重新执行测试…'); pending.add(key); refresh();
    try {
      const next = await api('/task-reports/' + encodeURIComponent(receipt.report_id) + '/retry', { method: 'POST' });
      if (!next?.status) throw new Error('尚未收到完整的报告回执，请刷新状态后再核对');
      receipts.set(receipt.report_id, next);
      feedback.set(key, { text: next.status === 'ready' ? '本次报告已生成，可以下载。' : next.error || next.reason || '报告正在生成，请稍候。', bad: next.status === 'failed' });
    } catch (error) { feedback.set(key, { text: `导出未完成：${error.message}`, bad: true }); await reloadReceipt(receipt.report_id); }
    finally { pending.delete(key); refresh(); }
  } }, icon('reset'), '重试导出报告');
}

export function reportControls(run, extra = null) {
  const original = run.test_report;
  const cached = receipts.get(original?.report_id);
  const receipt = cached || original;
  const state = cached?.status || run.state?.report || receipt?.status || 'not_generated';
  const base = receipt?.report_id || run.id;
  const links = [
    ['excel', receipt?.download_url, '下载本次用例 Excel', 'xlsx'],
    ['markdown', receipt?.markdown_download_url, '下载 Markdown 汇总', 'md'],
    ['bugs', receipt?.bugs_download_url, '下载 Bug 清单', 'md'],
  ];
  const messages = [...feedback].filter(([key]) => key.startsWith(base + ':'));
  const explanation = state === 'ready' && !receipt?.download_url ? '报告尚无可用下载地址，可刷新状态查看。' : ({ pending: '本次报告待生成，执行完成后会更新状态。', generating: '本次报告正在生成，请稍候。',
    not_generated: '本次尚无报告回执；可刷新状态查看。', failed: receipt?.error || receipt?.reason || '本次报告生成失败，尚无可下载文件。' })[state];
  return h('section', { class: 'stack', 'aria-label': '本次报告' },
    h('h3', {}, '本次报告'), explanation && h('p', { class: state === 'failed' ? 'error' : 'sub', role: 'status' }, explanation),
    h('div', { class: 'row wrap' }, state === 'ready' && links.map(([kind, url, label, extension]) => reportDownloadLink({
      key: base + ':' + kind, url, label, kind, filename: `autoSkill-${run.id}.${extension}`, reportId: receipt?.report_id,
    })), extra, state === 'failed' && receipt?.report_id && receipt.retryable !== false && retryButton(receipt),
    (state !== 'ready' || !receipt?.download_url) && h('button', { class: 'btn', onClick: async () => { await reloadReceipt(receipt?.report_id); refresh(); } }, '刷新报告状态')),
    messages.map(([, value]) => h('p', { class: value.bad ? 'error' : 'sub', role: 'status' }, value.text)));
}
