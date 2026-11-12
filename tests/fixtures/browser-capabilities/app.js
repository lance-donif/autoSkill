const at = id => document.getElementById(id);
at('note').addEventListener('focus', () => { at('focus-result').textContent = 'note focused'; });
at('note').addEventListener('keydown', event => { at('key-result').textContent = event.key + ':' + event.shiftKey; });
at('hover-target').addEventListener('mouseenter', () => { at('hover-result').textContent = 'hovered'; });
at('reveal').onclick = () => setTimeout(() => { at('delayed').style.display = 'inline'; }, 80);
at('remove-dynamic').onclick = () => at('dynamic').remove();
at('drag-source').addEventListener('dragstart', event => { event.dataTransfer.setData('text/plain', 'capability-drag'); });
at('drop-target').addEventListener('dragover', event => event.preventDefault());
at('drop-target').addEventListener('drop', event => {
  event.preventDefault();
  at('drop-result').textContent = event.dataTransfer.getData('text/plain') === 'capability-drag' ? 'dropped' : 'invalid drop';
});
at('mouse-target').addEventListener('mousedown', event => { at('mouse-result').textContent = 'down:' + event.button; at('mouse-modifier').textContent = String(event.shiftKey); });
at('mouse-target').addEventListener('mouseup', event => { at('mouse-result').textContent = 'up:' + event.button; at('mouse-modifier').textContent = String(event.shiftKey); });
at('scroll-box').addEventListener('scroll', () => { at('scroll-result').textContent = at('scroll-box').scrollLeft + ',' + at('scroll-box').scrollTop; });
at('confirm').onclick = () => { at('dialog-result').textContent = confirm('Choose yes or no') ? 'accepted' : 'dismissed'; };
at('prompt').onclick = () => { const value = prompt('Enter a value', 'default'); at('dialog-result').textContent = value === null ? 'prompt dismissed' : value; };
at('alert').onclick = () => { alert('Capability alert'); at('dialog-result').textContent = 'alert accepted'; };
at('upload').addEventListener('change', async () => {
  const files = Array.from(at('upload').files);
  const contents = await Promise.all(files.map(async file => file.name + ':' + await file.text()));
  at('upload-result').textContent = contents.join('|');
});
at('start-stream').onclick = () => {
  at('stream-output').textContent = 'waiting';
  at('stream-first').textContent = '';
  at('stream-done').hidden = true;
  setTimeout(() => { at('stream-output').textContent = 'first token'; at('stream-first').textContent = 'first token'; }, 80);
  setTimeout(() => { at('stream-output').textContent = 'partial response'; }, 900);
  setTimeout(() => { at('stream-done').hidden = false; }, 6000);
  setTimeout(() => { at('stream-done').hidden = true; }, 6100);
  setTimeout(() => { at('stream-output').textContent = 'answer complete'; at('stream-done').hidden = false; }, 6300);
};
let selected = null;
async function request(method, path, body) {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'request failed');
  return result;
}
async function refresh() {
  const result = await request('GET', '/api/items');
  at('items').replaceChildren(...result.items.map(item => {
    const row = document.createElement('tr');
    for (const value of [item.title, item.status]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
    return row;
  }));
  at('crud-count').textContent = String(result.total);
}
async function act(work) {
  try { await work(); } catch (error) { at('crud-result').textContent = 'ERROR ' + error.message; }
}
at('create-item').onclick = () => act(async () => {
  selected = await request('POST', '/api/items', {title: at('item-title').value});
  at('crud-result').textContent = 'created:' + selected.title; await refresh();
});
at('read-item').onclick = () => act(async () => {
  selected = await request('GET', '/api/items/' + selected.id);
  at('crud-result').textContent = 'read:' + selected.title;
});
at('update-item').onclick = () => act(async () => {
  selected = await request('PATCH', '/api/items/' + selected.id, {title: at('item-title').value, status: 'updated'});
  at('crud-result').textContent = 'updated:' + selected.title; await refresh();
});
at('delete-item').onclick = () => act(async () => {
  if (!confirm('Delete selected item?')) { at('crud-result').textContent = 'delete cancelled'; return; }
  await request('DELETE', '/api/items/' + selected.id); selected = null;
  at('crud-result').textContent = 'deleted'; await refresh();
});
refresh();
