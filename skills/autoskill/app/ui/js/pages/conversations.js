// Preserve captured requests, explicitly map dialogue turns, and launch the native conversation engine.
import { h, icon, api, replace, route, scope, go, href, fmtTime, toast, refresh, attempt, live } from '../core.js';
import { drawer, closeOverlay, empty, errorState } from '../ui.js';

const path = (projectId, captureId = '') => '/conversations/' + encodeURIComponent(projectId) + (captureId ? '/' + encodeURIComponent(captureId) : '');
const json = (value) => JSON.stringify(value, null, 2);

export async function render(view, { id }) {
  let projects;
  try { projects = (await api('/projects')).filter((item) => !item.broken); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const projectId = route().params.get('project') || scope().projects[0] || projects[0]?.id;
  const project = projects.find((item) => item.id === projectId);
  if (!project) {
    replace(view, empty({ glyph: 'api', title: '先创建一个项目', text: '将捕获的业务对话归属到项目后，可以建立多轮验证资产。',
      actions: h('a', { class: 'btn primary', href: href('cases', { new: 1 }) }, '新建项目') }));
    return;
  }
  if (id) await capturePage(view, project, id);
  else await overview(view, projects, project);
}

async function overview(view, projects, project) {
  let captures, assets;
  const offset = Math.max(0, Number(route().params.get('offset') || 0));
  try { [captures, assets] = await Promise.all([api(path(project.id) + `?limit=50&offset=${offset}`), api('/assets/' + encodeURIComponent(project.id) + '?all=1')]); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  const picker = h('select', { class: 'select', 'aria-label': '对话所属项目', onChange: (event) => go('conversations', { project: event.target.value }) },
    projects.map((item) => h('option', { value: item.id, selected: item.id === project.id }, item.name)));
  const existing = assets.assets.filter((item) => item.engine === 'ai-eval');
  replace(view, h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, '多轮对话'),
    h('p', { class: 'sub' }, '从真实页面捕获或 cURL / HAR 来源建立对话，独立验证答案后再增加并发。')),
  h('button', { class: 'btn primary', onClick: () => importCapture(project) }, icon('plus'), '导入对话来源')),
  h('div', { class: 'toolbar' }, h('span', { class: 'sub' }, '项目'), picker),
  h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, '捕获来源')),
    captures.items.length ? captureTable(project, captures.items) : empty({ glyph: 'api', title: '还没有对话来源',
      text: '导入浏览器复制的 cURL 或 HAR 文件，页面录制捕获的对话也会出现在这里。',
      actions: h('button', { class: 'btn', onClick: () => importCapture(project) }, '导入对话来源') }),
    captures.total > 50 && h('div', { class: 'card-body row', style: 'justify-content:flex-end' },
      h('span', { class: 'sub num' }, `${offset + 1}–${Math.min(offset + 50, captures.total)} / ${captures.total}`),
      h('button', { class: 'btn sm', disabled: offset === 0, onClick: () => go('conversations', { project: project.id, offset: Math.max(0, offset - 50) || null }) }, '上一页'),
      h('button', { class: 'btn sm', disabled: offset + 50 >= captures.total, onClick: () => go('conversations', { project: project.id, offset: offset + 50 }) }, '下一页'))),
  dialogueAssets(project, existing));
}

function captureTable(project, captures) {
  return h('div', { class: 'load-table-wrap' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, ['来源', '请求数', '候选轮次', '捕获时间', ''].map((text) => h('th', {}, text)))),
    h('tbody', {}, captures.map((item) => h('tr', {}, h('td', {}, h('div', { class: 'title' }, item.origin?.name || item.name || item.id), h('div', { class: 'sub mono' }, item.id)),
      h('td', { class: 'num' }, typeof item.requests === 'number' ? item.requests : item.request_count),
      h('td', { class: 'sub num' }, (item.conversation_candidates || []).join('、') || '需指定映射'),
      h('td', { class: 'sub num' }, fmtTime(item.created_at)),
      h('td', { class: 'actions' }, h('a', { class: 'btn sm', href: href('conversations/' + item.id, { project: project.id }) }, '查看与构建')))))));
}

function dialogueAssets(project, assets) {
  return h('section', { class: 'card', style: 'margin-top:16px' }, h('div', { class: 'card-head' }, h('div', {}, h('h2', {}, '多轮验证资产'),
    h('p', { class: 'sub' }, '一次用例包含完整多轮对话；运行结果分别记录执行、答案质量与性能。'))),
  assets.length ? h('div', { class: 'list' }, assets.map((asset) => h('div', { class: 'list-row row' },
    h('div', { class: 'grow' }, h('div', { class: 'title' }, asset.name), h('div', { class: 'sub mono' }, asset.id)),
    h('a', { class: 'btn sm', href: href('native-assets', { project: project.id, asset: asset.id }) }, '查看资产'),
    h('button', { class: 'btn sm', onClick: () => runDialogue(project, asset) }, icon('play'), '运行与压测'))))
    : empty({ glyph: 'cases', title: '还没有多轮资产', text: '打开一个捕获来源，指定问题、回答和会话字段，再提供独立的预期。' }));
}

function importCapture(project) {
  const name = h('input', { class: 'input', id: 'cv-import-name', placeholder: '例如：销售额问数' });
  const format = h('select', { class: 'select', id: 'cv-format' }, h('option', { value: 'curl' }, 'cURL'), h('option', { value: 'har' }, 'HAR'));
  const content = h('textarea', { class: 'input mono', id: 'cv-content', rows: 10, required: true, placeholder: '粘贴浏览器复制的 cURL 或完整 HAR JSON' });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const file = h('input', { class: 'input', type: 'file', accept: '.curl,.txt,.har,.json', 'aria-label': '选择 cURL 或 HAR 文件', onChange: async () => {
    if (!file.files[0]) return;
    try { content.value = await file.files[0].text(); name.value ||= file.files[0].name; format.value = /\.(har|json)$/i.test(file.files[0].name) ? 'har' : 'curl'; }
    catch (problem) { error.textContent = problem.message; error.hidden = false; }
  } });
  const save = h('button', { class: 'btn primary', type: 'submit', form: 'conversation-import-form' }, '保存来源');
  const form = h('form', { id: 'conversation-import-form', class: 'stack', onSubmit: async (event) => {
    event.preventDefault(); error.hidden = true; save.disabled = true;
    try {
      const captured = await api(path(project.id) + '/import-content', { method: 'POST', body: { name: name.value, format: format.value, content: content.value } });
      closeOverlay(); go('conversations/' + captured.id, { project: project.id });
    } catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { save.disabled = false; }
  } }, h('div', { class: 'field' }, h('label', { for: 'cv-import-name' }, '来源名称'), name),
  h('div', { class: 'field' }, h('label', { for: 'cv-format' }, '来源格式'), format),
  h('div', { class: 'field' }, h('span', { class: 'label' }, '选择本地文件'), file),
  h('div', { class: 'field' }, h('label', { for: 'cv-content' }, '来源内容'), content,
    h('span', { class: 'hint' }, '保存真实请求来源；cURL 作为文本解析，不会作为命令执行。')), error);
  drawer({ title: '导入对话来源', subtitle: project.name, body: form, wide: true,
    foot: [h('button', { class: 'btn', onClick: () => closeOverlay() }, '取消'), save] });
}

async function capturePage(view, project, captureId) {
  let capture;
  try { capture = await api(path(project.id, captureId)); }
  catch (error) { replace(view, h('section', { class: 'card' }, errorState(error, refresh))); return; }
  replace(view, h('a', { class: 'back', href: href('conversations', { project: project.id }) }, icon('right'), '多轮对话'),
    h('div', { class: 'page-head' }, h('div', { class: 'grow' }, h('h1', {}, capture.origin?.name || '对话来源'),
      h('p', { class: 'sub' }, `${project.name} · ${capture.requests.length} 个业务请求 · ${fmtTime(capture.created_at)}`))),
    h('section', { class: 'card' }, h('div', { class: 'card-body stack' }, h('h2', {}, '真实请求与响应'),
      capture.requests.map((request) => h('details', { class: 'more' }, h('summary', {}, `请求 ${request.number} · ${request.request.method} ${request.request.url}`),
        h('p', { class: 'sub' }, `${request.protocol} · 响应状态 ${request.transport_status ?? '未捕获'} · 来源 ${request.capture_source}`),
        h('pre', { class: 'code' }, json({ request: request.request, body_json: request.body_json, response: request.response, response_json: request.response_json,
          body_complete: request.body_complete, capture_errors: request.capture_errors })))))),
    h('section', { class: 'card', style: 'margin-top:16px' }, h('div', { class: 'card-body' }, mappingForm(project, capture))));
}

function mappingField(id, label, placeholder) {
  const input = h('input', { class: 'input mono', id, placeholder });
  return { input, field: h('div', { class: 'field' }, h('label', { for: id }, label), input) };
}

function mappingForm(project, capture) {
  const name = h('input', { class: 'input', id: 'cv-name', value: capture.origin?.name || '多轮对话验证', required: true });
  const turns = h('input', { class: 'input mono', id: 'cv-turns', required: true, value: capture.conversation_candidates.join(', '), placeholder: '例如：1, 2；也可 1, 1 重用模板' });
  const protocol = h('select', { class: 'select', id: 'cv-protocol' }, ['http', 'sse', 'websocket'].map((value) => h('option', { value }, value.toUpperCase())));
  protocol.value = capture.requests.find((item) => item.number === capture.conversation_candidates[0])?.protocol || 'http';
  const question = mappingField('cv-question', '问题字段', '例如 /question，留空按来源识别');
  const answer = mappingField('cv-answer', '回答字段', '例如 /answer，留空按来源识别');
  const conversation = mappingField('cv-conversation', '请求中的会话字段', '例如 /conversation_id');
  const responseConversation = mappingField('cv-response-conversation', '响应中的会话字段', '例如 /conversation_id');
  const extra = h('textarea', { class: 'input mono', id: 'cv-extra', rows: 6, value: '{}', 'aria-label': '高级映射 JSON' });
  const errors = h('p', { class: 'error', role: 'alert', hidden: true });
  const roundBox = h('div', { class: 'stack' });
  let rounds = [];
  const refreshRounds = () => { rounds = buildRounds(capture, turns.value, rounds); replace(roundBox, rounds.map((item) => item.node)); };
  turns.addEventListener('change', refreshRounds);
  refreshRounds();
  const save = h('button', { class: 'btn primary', type: 'submit' }, icon('plus'), '构建多轮资产');
  const form = h('form', { class: 'stack', onSubmit: async (event) => {
    event.preventDefault(); errors.hidden = true; save.disabled = true;
    try {
      refreshRounds();
      const advanced = JSON.parse(extra.value || '{}');
      if (!advanced || typeof advanced !== 'object' || Array.isArray(advanced)) throw new Error('高级映射需要 JSON 对象');
      const mapping = { turns: turns.value.split(/[,，\s]+/).filter(Boolean).map(Number), protocol: protocol.value,
        turn_overrides: rounds.map((item) => roundOverride(item)), ...advanced };
      for (const [key, input] of [['question_pointer', question.input], ['answer_pointer', answer.input], ['conversation_pointer', conversation.input], ['conversation_response_pointer', responseConversation.input]]) {
        if (input.value.trim()) mapping[key] = input.value.trim();
      }
      const asset = await api(path(project.id, capture.id) + '/build', { method: 'POST', body: { name: name.value, mapping } });
      toast('多轮资产已构建，运行后查看真实验证结果');
      runDialogue(project, asset);
    } catch (problem) { errors.textContent = problem.message; errors.hidden = false; }
    finally { save.disabled = false; }
  } }, h('h2', {}, '建立多轮验证'),
  h('p', { class: 'sub' }, '选择真实请求并指定字段路径。独立预期来自需求或固定资料；来源响应保留为证据，不会自动作为正确答案。'),
  h('div', { class: 'field' }, h('label', { for: 'cv-name' }, '资产名称'), name),
  h('div', { class: 'load-fields' }, h('div', { class: 'field' }, h('label', { for: 'cv-turns' }, '轮次请求序号'), turns),
    h('div', { class: 'field' }, h('label', { for: 'cv-protocol' }, '响应协议'), protocol)),
  h('div', { class: 'load-fields' }, question.field, answer.field), h('div', { class: 'load-fields' }, conversation.field, responseConversation.field),
  roundBox, h('details', { class: 'more' }, h('summary', {}, '参考数据、流事件与高级映射'),
    h('p', { class: 'hint' }, '可提供 dataset、performance、endpoint 或完整 turn_overrides；此处字段覆盖上面的同名设置。'), extra),
  errors, h('div', { class: 'row' }, save));
  return form;
}

function buildRounds(capture, text, previous) {
  return text.split(/[,，\s]+/).filter(Boolean).map((value, index) => {
    if (previous[index]?.number === value) return previous[index];
    const request = capture.requests.find((item) => item.number === Number(value));
    const body = request?.body_json || {};
    const question = h('textarea', { class: 'input', rows: 2, value: body.question || body.prompt || body.query || '', 'aria-label': `第 ${index + 1} 轮问题` });
    const expected = h('input', { class: 'input', placeholder: '留空时尚未验证业务正确性', 'aria-label': `第 ${index + 1} 轮独立预期` });
    const type = h('select', { class: 'select', 'aria-label': `第 ${index + 1} 轮预期类型` }, h('option', { value: 'text' }, '文本'), h('option', { value: 'json' }, 'JSON（数字 / 对象等）'));
    return { number: value, question, expected, type, node: h('div', { class: 'stack' }, h('h3', {}, `第 ${index + 1} 轮 · 请求 ${value}`),
      !request && h('p', { class: 'error' }, '来源中不存在这个请求序号'), h('div', { class: 'field' }, h('span', { class: 'label' }, '问题'), question),
      h('div', { class: 'field' }, h('span', { class: 'label' }, '独立预期'), h('div', { class: 'row' }, expected, type))) };
  });
}

function roundOverride(round) {
  const value = {};
  if (round.question.value) value.question = round.question.value;
  if (round.expected.value !== '') value.expected = round.type.value === 'json' ? JSON.parse(round.expected.value) : round.expected.value;
  return value;
}

function runDialogue(project, asset) {
  const concurrency = h('input', { class: 'input', type: 'number', id: 'cv-run-concurrency', min: 1, step: 1, value: 1, required: true });
  const iterations = h('input', { class: 'input', type: 'number', id: 'cv-run-iterations', min: 1, step: 1, value: 1, required: true });
  const ramp = h('input', { class: 'input', type: 'number', id: 'cv-run-ramp', min: 0, step: 'any', value: 0, required: true });
  const error = h('p', { class: 'error', role: 'alert', hidden: true });
  const start = h('button', { class: 'btn primary', type: 'submit', form: 'conversation-run-form' }, icon('play'), '开始运行');
  const form = h('form', { id: 'conversation-run-form', class: 'stack', onSubmit: async (event) => {
    event.preventDefault(); error.hidden = true;
    const run = await attempt(() => api('/native-runs', { method: 'POST', body: { project_id: project.id, asset_id: asset.id,
      parameters: { load: { concurrency: Number(concurrency.value), iterations: Number(iterations.value), ramp_up_seconds: Number(ramp.value) } } } }), start);
    if (run) { closeOverlay(); live.until = Date.now() + 10000; go('native-runs/' + run.id); }
  } }, h('p', { class: 'sub' }, '先使用 1 并发、1 次迭代核对独立预期，再按需增加并发。需要宿主语义判定的资产继续由宿主处理。'),
  h('div', { class: 'field' }, h('label', { for: 'cv-run-concurrency' }, '并发会话数'), concurrency),
  h('div', { class: 'field' }, h('label', { for: 'cv-run-iterations' }, '每条对话迭代次数'), iterations),
  h('div', { class: 'field' }, h('label', { for: 'cv-run-ramp' }, '渐增时间（秒）'), ramp), error);
  drawer({ title: '运行多轮对话', subtitle: `${project.name} · ${asset.name}`, body: form,
    foot: [h('button', { class: 'btn', onClick: () => { closeOverlay(); refresh(); } }, '关闭'), start] });
}
