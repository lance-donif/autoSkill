// Keep editable control identity while updating handlers to the current render.
const handlers = new WeakMap();

export function listen(node, type, callback) {
  node.addEventListener(type, callback);
  const list = handlers.get(node) || [];
  list.push([type, callback]);
  handlers.set(node, list);
}

export function adoptHandlers(current, fresh) {
  for (const [type, callback] of handlers.get(current) || []) current.removeEventListener(type, callback);
  const latest = handlers.get(fresh) || [];
  for (const [type, callback] of latest) current.addEventListener(type, callback);
  handlers.set(current, latest);
}
