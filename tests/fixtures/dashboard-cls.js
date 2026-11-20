// Document-start observer shared by Chromium automation and DevTools MCP navigations.
(() => {
  const entries = [];
  const deliveries = [];
  let pending = 0;
  let lastActivity = 0;
  function selector(node) {
    const parts = [];
    for (let el = node?.nodeType === 1 ? node : node?.parentElement; el; el = el.parentElement) {
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      let part = el.localName;
      if (el.classList.length) part += '.' + [...el.classList].slice(0, 2).map(name => CSS.escape(name)).join('.');
      if (el.parentElement?.querySelectorAll(':scope > ' + part).length > 1) {
        part += ':nth-child(' + ([...el.parentElement.children].indexOf(el) + 1) + ')';
      }
      parts.unshift(part);
    }
    return parts.join(' > ') || null;
  }
  function collect(shifts) {
    for (const shift of shifts) {
      lastActivity = Math.max(lastActivity, shift.startTime);
      entries.push({
        time_ms: shift.startTime, value: shift.value, had_recent_input: shift.hadRecentInput,
        sources: shift.sources.map(source => ({
          selector: selector(source.node), previous: source.previousRect.toJSON(), current: source.currentRect.toJSON(),
        })),
      });
    }
  }
  const observer = new PerformanceObserver(list => collect(list.getEntries()));
  observer.observe({ type: 'layout-shift', buffered: true });

  // Deliver real responses after fixed latency. Pages replaces fetch with an in-memory adapter.
  function delayed(fetcher) {
    return async function (input, options) {
      const path = new URL(input instanceof Request ? input.url : input, location.href).pathname;
      let delay = 0;
      if (path === '/api/status' || /^\/api\/projects\/[^/]+\/folders$/.test(path)) delay = 650;
      else if (/^\/api\/(dashboard|cases|load-tests\/[^/]+)$/.test(path) || path.endsWith('/fixtures.json')) delay = 300;
      if (!delay) return fetcher.call(this, input, options);
      pending++;
      try {
        const [response] = await Promise.all([
          fetcher.call(this, input, options), new Promise(resolve => setTimeout(resolve, delay)),
        ]);
        deliveries.push({ path, time_ms: performance.now() });
        return response;
      } finally {
        pending--;
        lastActivity = performance.now();
      }
    };
  }
  let fetcher = delayed(window.fetch);
  Object.defineProperty(window, 'fetch', {
    configurable: true, get: () => fetcher, set: adapter => { fetcher = delayed(adapter); },
  });
  window.dashboardCLS = async (readySelector) => {
    if (!PerformanceObserver.supportedEntryTypes.includes('layout-shift')) throw new Error('Layout Instability API unavailable');
    const deadline = performance.now() + 20000;
    function ready() {
      const rect = document.querySelector(readySelector)?.getBoundingClientRect();
      return rect?.width > 0 && rect.height > 0 && !document.querySelector('#view[aria-busy="true"]');
    }
    while (!ready() || pending || performance.now() - lastActivity < 1100) {
      if (performance.now() > deadline) throw new Error('Page did not settle: ' + readySelector);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    collect(observer.takeRecords());
    const marker = document.querySelector(readySelector);
    return {
      entries, deliveries, viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
      first_contentful_paint_ms: performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
      observed_ms: performance.now(), marker: { selector: readySelector, bounds: marker.getBoundingClientRect().toJSON() },
    };
  };
})();
