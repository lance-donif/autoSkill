// Export the same rendered report and loaded styles as a self-contained document.
import { h, icon } from '../core.js';

function stylesheetText(sheet) {
  const rules = [];
  for (const rule of sheet.cssRules) {
    if (rule.type === CSSRule.IMPORT_RULE) {
      const imported = stylesheetText(rule.styleSheet);
      rules.push(rule.media.mediaText ? `@media ${rule.media.mediaText} { ${imported} }` : imported);
    } else rules.push(rule.cssText);
  }
  const text = rules.join('\n');
  return sheet.media.mediaText ? `@media ${sheet.media.mediaText} { ${text} }` : text;
}

function captureStyles() {
  try {
    const sheets = [...document.styleSheets].filter((sheet) => !sheet.disabled);
    if (!sheets.length) throw new Error('报告样式尚未加载');
    return sheets.map(stylesheetText).join('\n');
  } catch (error) {
    throw new Error(`HTML 报告未生成：无法读取当前页面样式（${error.message}）`);
  }
}

function dataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('无法读取截图内容'));
    reader.readAsDataURL(blob);
  });
}

async function loadImage(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  if (!blob.type.startsWith('image/')) throw new Error('附件不是图片');
  return dataUrl(blob);
}

async function embedScreenshot(shot, cache) {
  const image = shot.querySelector('img');
  const url = image.src;
  const filename = decodeURIComponent(new URL(url).pathname.split('/').at(-1));
  try {
    if (!cache.has(url)) cache.set(url, loadImage(url));
    const embedded = await cache.get(url);
    image.loading = 'eager';
    image.src = embedded;
    await image.decode();
    shot.href = embedded;
    shot.title = '查看内嵌原图';
    shot.dataset.filename = filename;
    shot.removeAttribute('target');
    return null;
  } catch (error) {
    const warning = `截图 ${filename} 未能嵌入：${error.message}`;
    shot.replaceWith(h('div', { class: 'banner', role: 'status' }, icon('warn'), warning));
    return warning;
  }
}

async function embedScreenshots(content) {
  const cache = new Map();
  const outcomes = await Promise.all([...content.querySelectorAll('a.shot')].map((shot) => embedScreenshot(shot, cache)));
  return outcomes.filter(Boolean);
}

function toolbar(run, exportedAt, title, load) {
  return h('header', { class: 'scope report-toolbar' },
    h('div', { class: 'grow scope-text' }, h('b', {}, 'autoSkill ' + title),
      h('div', {}, `${run.id} · 导出于 ${exportedAt.toLocaleString('zh-CN', { hour12: false })}`),
      ['Queued', 'Running'].includes(run.status) && h('div', {}, '导出时任务尚未完成，结果以本文件中的执行进度为准。')),
    !load && h('button', { class: 'btn sm', dataset: { reportAction: 'expand' } }, '全部展开'),
    !load && h('button', { class: 'btn sm', dataset: { reportAction: 'collapse' } }, '全部收起'));
}

function imageViewer() {
  return h('dialog', { class: 'report-image', 'aria-label': '执行截图原图' },
    h('div', { class: 'row' }, h('b', { class: 'grow' }, '执行截图原图'),
      h('a', { class: 'btn sm', download: '' }, icon('image'), '下载原图'),
      h('button', { class: 'btn sm', dataset: { reportAction: 'close-image' } }, icon('x'), '关闭')),
    h('img', { alt: '执行截图原图' }));
}

// This function is serialized without any application imports or runtime dependencies.
function installReportInteractions() {
  function setOpen(row, open) {
    row.classList.toggle('open', open);
    row.querySelector('.result-head').setAttribute('aria-expanded', String(open));
    row.querySelector('.result-body').hidden = !open;
  }
  const viewer = document.querySelector('.report-image');
  document.addEventListener('click', (event) => {
    const head = event.target.closest('.result-head');
    if (head) setOpen(head.closest('.result'), head.getAttribute('aria-expanded') !== 'true');
    const action = event.target.closest('[data-report-action]')?.dataset.reportAction;
    if (action === 'expand' || action === 'collapse') {
      const open = action === 'expand';
      for (const row of document.querySelectorAll('.result')) setOpen(row, open);
      for (const detail of document.querySelectorAll('.result-body details')) detail.open = open;
    }
    if (action === 'close-image') viewer.close();
    const shot = event.target.closest('a.shot');
    if (shot) {
      event.preventDefault();
      viewer.querySelector('img').src = shot.querySelector('img').src;
      const download = viewer.querySelector('a');
      download.href = shot.href;
      download.download = shot.dataset.filename;
      viewer.showModal();
    }
  });
  viewer.addEventListener('click', (event) => { if (event.target === viewer) viewer.close(); });
}

function installLoadInteractions(render) {
  const run = JSON.parse(document.getElementById('load-report-data').textContent);
  const target = document.querySelector('[data-load-dashboard]');
  const select = document.querySelector('[data-load-stage]');
  const update = () => target.replaceChildren(render(run, { stage: select?.value || null }));
  select?.addEventListener('change', update);
  update();
}

export async function downloadRunHtml(run, content, { loadDashboard } = {}) {
  const styles = captureStyles();
  const warnings = await embedScreenshots(content);
  const title = loadDashboard ? (run.load?.mode === 'stress' ? '压力测试报告' : '性能测试报告') : '执行报告';
  const output = document.implementation.createHTMLDocument(`${run.project_name || run.project_id} · autoSkill ${title}`);
  output.documentElement.lang = 'zh-CN';
  output.documentElement.dataset.theme = document.documentElement.dataset.theme || 'light';
  output.head.prepend(h('meta', { charset: 'utf-8' }));
  output.head.append(h('meta', { name: 'viewport', content: 'width=device-width,initial-scale=1' }), h('style', {}, styles));
  output.body.append(toolbar(run, new Date(), title, !!loadDashboard), content);
  if (warnings.length) content.prepend(h('div', { class: 'banner', role: 'status' }, icon('warn'),
    `有 ${warnings.length} 张截图未能嵌入，原因已标在对应明细中；其他结果与明细已保存。`));
  if (loadDashboard) {
    const data = JSON.stringify(loadDashboard.data).replaceAll('<', '\\u003c');
    output.body.append(h('script', { type: 'application/json', id: 'load-report-data' }, data),
      h('script', {}, loadDashboard.script + `\n(${installLoadInteractions.toString()})(renderLoadDashboard);`));
  } else {
    output.body.append(imageViewer(), h('script', {}, `(${installReportInteractions.toString()})();`));
  }
  const html = '<!doctype html>\n' + output.documentElement.outerHTML;
  const filename = `autoSkill-${String(run.id).replace(/[\\/:*?"<>|]/g, '-')}.html`;
  const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
  const link = h('a', { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return { filename, warnings };
}
