// Ticket Desk front end. The session token is kept in localStorage and sent as a
// Bearer header, so a saved login state has to carry both cookies and storage.
(function () {
  const TOKEN_KEY = 'td_token';
  const $ = (id) => document.getElementById(id);
  const labels = {
    priority: { low: '低', medium: '中', high: '高' },
    status: { open: '待处理', in_progress: '处理中', resolved: '已解决' },
  };
  let user = null;
  let searchTimer = null;

  async function api(method, path, body) {
    const headers = {};
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = 'Bearer ' + token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== '/api/login') {
      localStorage.removeItem(TOKEN_KEY);
      showLogin();
    }
    return { ok: response.ok, status: response.status, data };
  }

  function toast(message) {
    const node = $('toast');
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3000);
  }

  function showLogin() {
    user = null;
    $('app-view').hidden = true;
    $('login-view').hidden = false;
  }

  function showApp() {
    $('login-view').hidden = true;
    $('app-view').hidden = false;
    $('current-user').textContent = user.name;
    $('new-ticket').hidden = user.role !== 'admin';
    loadTickets();
  }

  function cell(text) {
    const td = document.createElement('td');
    td.textContent = text;
    return td;
  }

  function row(ticket) {
    const tr = document.createElement('tr');
    tr.dataset.testid = 'ticket-row';
    tr.dataset.ticketId = ticket.id;
    tr.append(cell(ticket.id), cell(ticket.title), cell(labels.priority[ticket.priority]));
    const status = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge ' + ticket.status;
    badge.textContent = labels.status[ticket.status];
    status.append(badge);
    const actions = document.createElement('td');
    if (user.role === 'admin' && ticket.status !== 'resolved') {
      const button = document.createElement('button');
      button.className = 'link';
      button.textContent = '标记已解决';
      button.setAttribute('aria-label', '标记已解决 ' + ticket.id);
      button.addEventListener('click', () => resolve(ticket.id));
      actions.append(button);
    }
    tr.append(status, actions);
    return tr;
  }

  async function loadTickets() {
    const query = new URLSearchParams();
    if ($('status-filter').value) query.set('status', $('status-filter').value);
    if ($('search').value.trim()) query.set('q', $('search').value.trim());
    const result = await api('GET', '/api/tickets' + (query.size ? '?' + query : ''));
    if (!result.ok) return;
    $('ticket-rows').replaceChildren(...result.data.items.map(row));
    $('ticket-total').textContent = '共 ' + result.data.total + ' 条';
    $('empty').hidden = result.data.total > 0;
  }

  async function resolve(id) {
    const result = await api('PATCH', '/api/tickets/' + id, { status: 'resolved' });
    if (result.ok) {
      toast('工单 ' + id + ' 已解决');
      loadTickets();
    }
  }

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('login-error').hidden = true;
    const result = await api('POST', '/api/login', { username: $('username').value, password: $('password').value });
    if (!result.ok) {
      $('login-error').textContent = result.data.message || '登录失败';
      $('login-error').hidden = false;
      return;
    }
    localStorage.setItem(TOKEN_KEY, result.data.token);
    user = result.data.user;
    $('password').value = '';
    showApp();
  });

  $('logout').addEventListener('click', async () => {
    await api('POST', '/api/logout');
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
  });

  $('new-ticket').addEventListener('click', () => {
    $('ticket-form').reset();
    $('title-error').hidden = true;
    $('ticket-dialog').showModal();
  });
  $('cancel-ticket').addEventListener('click', () => $('ticket-dialog').close());

  $('ticket-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = { title: $('title').value, priority: $('priority').value, description: $('description').value };
    const result = await api('POST', '/api/tickets', body);
    if (result.status === 422) {
      const reason = (result.data.fields || {}).title;
      $('title-error').textContent = reason === 'too_long' ? '标题不能超过 80 个字' : '请填写标题';
      $('title-error').hidden = false;
      return;
    }
    if (!result.ok) return;
    $('ticket-dialog').close();
    toast('工单 ' + result.data.id + ' 已创建');
    loadTickets();
  });

  $('status-filter').addEventListener('change', loadTickets);
  $('search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadTickets, 250);
  });

  (async function start() {
    if (!localStorage.getItem(TOKEN_KEY)) return showLogin();
    const me = await api('GET', '/api/me');
    if (!me.ok) return showLogin();
    user = me.data;
    showApp();
  })();
})();
