// Only presentation controls are interactive in the static Pages preview.
const side = document.getElementById('side');
const toggle = side.querySelector('.nav-toggle');
const themeButton = side.querySelector('.theme-toggle');

function menu(open) {
  side.dataset.navOpen = String(open);
  toggle.setAttribute('aria-expanded', String(open));
}
function theme(value) {
  document.documentElement.dataset.theme = value;
  const label = value === 'dark' ? '浅色' : '深色';
  themeButton.querySelector('.text').textContent = label;
  themeButton.setAttribute('aria-label', label);
}
let preferred = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
try { preferred = sessionStorage.getItem('preview-theme') || preferred; } catch { /* Storage is optional. */ }
theme(preferred);
themeButton.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  theme(next);
  try { sessionStorage.setItem('preview-theme', next); } catch { /* Storage is optional. */ }
});
toggle.addEventListener('click', () => menu(side.dataset.navOpen !== 'true'));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && side.dataset.navOpen === 'true') { menu(false); toggle.focus(); }
});
document.addEventListener('pointerdown', (event) => { if (!side.contains(event.target)) menu(false); });
for (const button of document.querySelectorAll('[data-preview-href]')) {
  button.addEventListener('click', () => location.assign(button.dataset.previewHref));
}
