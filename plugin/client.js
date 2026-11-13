// DeepSeek Harness client entry (loaded as shipped; no build step).
// Two panels, both showing the local dashboard: overview and schedules.
window.__ModuleLoader__.load({ id: 'dsh-plugin-autoskill', factory: (require) => {
  const module = { exports: {} };
  const exports = module.exports;
  const React = require('react');
  let activationCount = 0;
  exports.inject = ['slots', 'layout'];
  exports.apply = (ctx, config = {}) => {
    const port = config.port ?? 8787;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid autoSkill port');
    const origin = 'http://127.0.0.1:' + port;
    activationCount += 1;
    // This public cache key belongs to the activation, not to React render or user storage.
    const loadId = Date.now().toString(36) + '-' + activationCount.toString(36) + '-' + Math.random().toString(36).slice(2);
    const panels = [['autoskill-dashboard', 'autoSkill 仪表盘', 'dashboard'], ['autoskill-schedules', 'autoSkill 定时任务', 'schedules']];
    for (const [key, label, page] of panels) {
      const Panel = () => React.createElement('iframe', { src: origin + '/?embed=1&activation=' + loadId + '#/' + page, title: label,
        style: { width: '100%', height: '100%', border: 0, display: 'block' } });
      const Icon = () => React.createElement('span', { 'aria-hidden': true, style: { fontSize: 15 } }, page === 'dashboard' ? '▦' : '◷');
      ctx.effect(() => ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key }, Panel)), 'autoskill: ' + key);
      ctx.effect(() => ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: key, order: 30, label: () => label }, Icon)), 'autoskill: nav ' + key);
    }
  };
  return module.exports;
} });
