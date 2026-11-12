(function (locator) {
  // Element lookup for the Selenium driver, mirroring Playwright's getBy* locators
  // closely enough that one recorded step resolves to the same element in both engines.
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox',
    radio: 'radio', range: 'slider', number: 'spinbutton', search: 'searchbox', hidden: null,
  };
  const TAG_ROLES = {
    BUTTON: 'button', TEXTAREA: 'textbox', H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading',
    H5: 'heading', H6: 'heading', LI: 'listitem', UL: 'list', OL: 'list', NAV: 'navigation', MAIN: 'main',
    TABLE: 'table', TR: 'row', TD: 'cell', TH: 'columnheader', OPTION: 'option', IMG: 'img',
    DIALOG: 'dialog', PROGRESS: 'progressbar', FORM: 'form', ARTICLE: 'article',
  };
  function role(el) {
    const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (explicit) return explicit;
    if (el.tagName === 'A') return el.hasAttribute('href') ? 'link' : null;
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type in INPUT_ROLES) return INPUT_ROLES[type];
      return el.hasAttribute('list') ? 'combobox' : 'textbox';
    }
    if (el.tagName === 'SELECT') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    return TAG_ROLES[el.tagName] || null;
  }
  function labelText(el) {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const text = norm(ids.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || '').join(' '));
      if (text) return text;
    }
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.labels && el.labels.length) return norm(Array.from(el.labels).map((l) => l.textContent).join(' '));
    return '';
  }
  function accessibleName(el) {
    const fromLabel = labelText(el);
    if (fromLabel) return fromLabel;
    const tag = el.tagName;
    if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase())) return norm(el.value);
    if (tag === 'IMG') return norm(el.getAttribute('alt'));
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      return norm(el.getAttribute('placeholder') || el.getAttribute('title'));
    }
    return norm(el.textContent) || norm(el.getAttribute('title'));
  }
  const rendered = (el) => el.tagName === 'OPTION' || el.getClientRects().length > 0;
  const exposed = (el) => rendered(el) && !el.closest('[aria-hidden="true"]');
  const all = () => Array.from(document.querySelectorAll('body *'));
  const value = locator.value;
  switch (locator.by) {
    case 'css':
      return Array.from(document.querySelectorAll(value));
    case 'testid':
      return all().filter((el) => el.getAttribute('data-testid') === value);
    case 'placeholder':
      return all().filter((el) => el.getAttribute('placeholder') === value);
    case 'label':
      return all().filter((el) => labelText(el) === norm(value));
    case 'text': {
      const wanted = norm(value);
      const own = (el) => !/^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName) && norm(el.textContent) === wanted;
      return all().filter((el) => own(el) && rendered(el) && !Array.from(el.children).some(own));
    }
    case 'role': {
      const name = locator.name == null ? null : norm(locator.name);
      return all().filter((el) => role(el) === locator.role && exposed(el) && (name === null || accessibleName(el) === name));
    }
    default:
      return [];
  }
})
