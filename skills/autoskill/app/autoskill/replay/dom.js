(function (root = document) {
  // DOM helpers shared by the Selenium driver (element lookup) and the recorder
  // (turning the element an agent touched into stable locator candidates).
  // Lookup mirrors Playwright's getBy* locators closely enough that one recorded
  // step resolves to the same element in both engines.
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const FIELD = /^(INPUT|TEXTAREA|SELECT)$/;
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
      const text = norm(ids.split(/\s+/).map((id) => (root.querySelector('#' + CSS.escape(id)) || {}).textContent || '').join(' '));
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
    if (FIELD.test(tag)) return norm(el.getAttribute('placeholder') || el.getAttribute('title'));
    return norm(el.textContent) || norm(el.getAttribute('title'));
  }
  const rendered = (el) => el.tagName === 'OPTION' || el.getClientRects().length > 0;
  const exposed = (el) => rendered(el) && !el.closest('[aria-hidden="true"]');
  const all = () => Array.from(root.querySelectorAll('*'));

  function xpath(locator) {
    let anchor = root;
    if (locator.xpath_anchor) {
      const matches = root.querySelectorAll(locator.xpath_anchor);
      if (matches.length !== 1) throw new Error('XPath 元素锚点必须唯一匹配');
      anchor = matches[0];
      if (!locator.value.startsWith('.')) throw new Error('带元素锚点的 XPath 必须以 . 开始');
      const expression = locator.value.replace(/'[^']*'|"[^"]*"/g, "''");
      if (/(^|[^.])\.\.([^.]|$)|(?:parent|ancestor(?:-or-self)?|following(?:-sibling)?|preceding(?:-sibling)?)\s*::|[|(=\[,]\s*\//.test(expression)) {
        throw new Error('带元素锚点的 XPath 不能使用越界路径或祖先轴');
      }
    } else if (root.nodeType === 11) {
      throw new Error('Shadow DOM 内的 XPath 需要 xpath_anchor 元素锚点');
    }
    const doc = anchor.ownerDocument || anchor;
    const result = doc.evaluate(locator.value, anchor, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const out = [];
    for (let index = 0; index < result.snapshotLength; index++) {
      const node = result.snapshotItem(index);
      if (node.nodeType !== 1 || node.getRootNode() !== root) throw new Error('XPath 结果必须是当前上下文内的元素');
      out.push(node);
    }
    return out;
  }

  function find(locator) {
    const value = locator.value;
    switch (locator.by) {
      case 'css':
        return Array.from(root.querySelectorAll(value));
      case 'xpath':
        return xpath(locator);
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
  }

  // ---- recorder side: element -> locator candidates, best first ----
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^\w-]/g, '\\$&'));
  const stableId = (id) => id && !/\d{4,}|[:.]|^(ember|react|radix|headlessui|mui|el-id)/i.test(id);
  function cssPath(el) {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== root.body; node = node.parentElement) {
      if (stableId(node.id)) { parts.unshift('#' + cssEscape(node.id)); break; }
      const testid = node.getAttribute('data-testid');
      if (testid && node !== el) { parts.unshift('[data-testid="' + cssEscape(testid) + '"]'); break; }
      let part = node.tagName.toLowerCase();
      const classes = Array.from(node.classList).filter((c) => /^[a-z][\w-]*$/i.test(c) && !/\d{3,}/.test(c)).slice(0, 2);
      if (classes.length) part += '.' + classes.map(cssEscape).join('.');
      const same = node.parentNode ? Array.from(node.parentNode.children || []).filter((c) => c.tagName === node.tagName) : [];
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      parts.unshift(part);
      if (parts.length >= 5) break;
    }
    return parts.join(' > ');
  }
  function withIndex(locator, el) {
    const index = find(locator).indexOf(el);
    if (index < 0) return null;
    if (index > 0) locator.nth = index;
    return locator;
  }
  function xpathLiteral(value) {
    if (!value.includes("'")) return "'" + value + "'";
    if (!value.includes('"')) return '"' + value + '"';
    return 'concat(' + value.split("'").map((part) => "'" + part + "'").join(',"\'",') + ')';
  }
  function xpathCandidate(el) {
    const stable = (node) => {
      const testid = node.getAttribute('data-testid');
      if (testid) return '*[@data-testid=' + xpathLiteral(testid) + ']';
      if (stableId(node.id)) return '*[@id=' + xpathLiteral(node.id) + ']';
      return null;
    };
    let anchor = null;
    if (root.nodeType === 11) {
      anchor = el;
      while (anchor.parentElement) anchor = anchor.parentElement;
    }
    const parts = [];
    const candidates = [];
    for (let node = el; node && node.nodeType === 1 && node !== anchor; node = node.parentElement) {
      const hint = stable(node);
      if (hint) candidates.push('.//' + hint + (parts.length ? '/' + parts.join('/') : ''));
      const same = Array.from(node.parentNode.children || []).filter((item) => item.localName === node.localName);
      parts.unshift('*[local-name()=' + xpathLiteral(node.localName) + '][' + (same.indexOf(node) + 1) + ']');
    }
    candidates.push(anchor ? (parts.length ? './' + parts.join('/') : '.') : '/' + parts.join('/'));
    for (const value of candidates) {
      const locator = { by: 'xpath', value };
      if (anchor) locator.xpath_anchor = cssPath(anchor);
      const matches = find(locator);
      if (matches.length === 1 && matches[0] === el) {
        if (value.startsWith('/') || value.includes('local-name()')) locator.fragile = true;
        return locator;
      }
    }
    return null;
  }
  function describe(el) {
    const out = [];
    const push = (locator) => { const ok = locator && withIndex(locator, el); if (ok) out.push(ok); };
    const testid = el.getAttribute('data-testid');
    if (testid) push({ by: 'testid', value: testid });
    const r = role(el);
    const name = accessibleName(el);
    if (FIELD.test(el.tagName)) {
      const label = labelText(el);
      if (label && label.length <= 60) push({ by: 'label', value: label });
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) push({ by: 'placeholder', value: placeholder });
    }
    if (r && name && name.length <= 80) push({ by: 'role', role: r, name });
    if (!FIELD.test(el.tagName) && !r) {
      const text = norm(el.textContent);
      if (text && text.length <= 60) push({ by: 'text', value: text });
    }
    const css = cssPath(el);
    if (css) push({ by: 'css', value: css });
    const fallback = xpathCandidate(el);
    if (fallback) out.push(fallback);
    const type = (el.getAttribute('type') || '').toLowerCase();
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    return {
      locators: out, tag: el.tagName.toLowerCase(), role: r, name: name.slice(0, 80), type,
      editable: (el.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|file|image|hidden)$/.test(type)) || el.tagName === 'TEXTAREA' || el.isContentEditable,
      checkable: el.tagName === 'INPUT' && (type === 'checkbox' || type === 'radio') || /^(checkbox|radio|switch)$/.test(r || ''),
      secret: type === 'password' || /password|one-time-code|^cc-/.test(auto),
    };
  }
  // The element an agent-browser ref points at: same role and name, nearest to the ref's box.
  function fromRef(ref) {
    let pool = ref.role ? find({ by: 'role', role: ref.role, name: ref.name || null }) : [];
    if (!pool.length && ref.box) {
      const hit = root.elementFromPoint(ref.box.x + ref.box.width / 2, ref.box.y + ref.box.height / 2);
      if (hit) pool = [hit];
    }
    if (!pool.length) return null;
    if (!ref.box || pool.length === 1) return pool[0];
    const distance = (el) => { const b = el.getBoundingClientRect(); return Math.abs(b.x - ref.box.x) + Math.abs(b.y - ref.box.y); };
    return pool.slice().sort((a, b) => distance(a) - distance(b))[0];
  }
  function target(spec) {
    const el = spec.ref ? fromRef(spec.ref) : spec.locator ? find(spec.locator)[spec.locator.nth || 0] : root.activeElement;
    return el && el !== root.body ? describe(el) : null;
  }
  // One reading of an element property, for checking an assertion while recording.
  function read(locator, what, arg) {
    const found = find(locator);
    if (what === 'count') return found.length;
    const el = found[locator.nth || 0];
    if (!el) return null;
    const visible = rendered(el) && getComputedStyle(el).visibility !== 'hidden';
    if (what === 'visible') return visible;
    if (what === 'checked') return !!el.checked;
    if (what === 'value') return el.value == null ? null : String(el.value);
    if (what === 'attribute') return el.getAttribute(arg);
    return el.innerText == null ? el.textContent || '' : el.innerText;
  }
  return { find, target, read, describe };
})
