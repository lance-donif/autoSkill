// Source: the gridlines skill's probe.js, extended with autoSkill dashboard containers.
// Injected into a page. Measures the ink edges of every text run, icon and
// boxed control inside each container, groups them into rows, and draws the
// dominant edges as dashed guides. Red: lead line and label line (solid), box
// line (faint), icon-column centre (fainter). Blue: trailing line. Amber: the
// trailing column's centre line.
//
//   window.__gridlines({ draw: true, containers: 'css, selectors' })
//
// Returns one entry per container: its dominant lines, every row's edges and
// vertical-centre spread, and the outliers that miss a line by 2px or more.
const GRIDLINE_CONTAINERS = [
  "aside",
  "nav",
  "header",
  // A case card splits into a folder tree and a table; measure its parts instead.
  "section:not(:has(.project-folders))",
  "article",
  '[role="dialog"]',
  '[role="menu"]',
  '[role="listbox"]',
  '[data-slot="dialog-content"]',
  '[data-slot="sheet-content"]',
  '[data-slot="popover-content"]',
  '[data-slot="dropdown-menu-content"]',
  '[data-slot="card"]',
  // autoSkill dashboard: the shell, page headers, cards and overlays in #layer.
  ".side",
  // The phone navigation panel hangs below the compact bar while it is open.
  '.side[data-nav-open="true"] .side-panel',
  ".scope",
  "#view .page-head",
  "#view .card:not(:has(.project-folders))",
  "#view .kpi",
  "#view .card:has(.project-folders) > .card-head",
  "#view .folder-content",
  "#layer .drawer",
  "#layer .dialog",
  ".menu",
];

window.__gridlines = function (opts = {}) {
  const draw = opts.draw !== false;
  const TOL = 1;
  document.querySelectorAll("[data-gridline]").forEach((n) => n.remove());

  const containerSelector = opts.containers || GRIDLINE_CONTAINERS.join(", ");

  const clear = (c) => !c || /rgba\(0, 0, 0, 0\)|transparent/.test(c);
  // Opacity does not inherit, so a hover-only row action hides its icon through an ancestor.
  const shown = (el) => {
    if (getComputedStyle(el).visibility === "hidden") return false;
    for (let a = el; a; a = a.parentElement) if (parseFloat(getComputedStyle(a).opacity) === 0) return false;
    return true;
  };
  const skip = (el) =>
    el.closest('[aria-hidden="true"], .sr-only, [data-gridline], canvas');
  // Decorative icons carry aria-hidden themselves and are still ink; only an aria-hidden
  // ancestor (an off-screen or inert subtree) removes an icon from the measurement.
  const skipIcon = (el) =>
    el.parentElement?.closest('[aria-hidden="true"]') || el.closest(".sr-only, [data-gridline], canvas");
  // Heuristic: a CSS-drawn marker (a ::before chevron or swatch with no text) is ink. When
  // it is the first item of a centred flex row, it sits at the row's content-box start and
  // on the row's centre, so it is measured as an icon there.
  const markerOf = (el) => {
    const cs = getComputedStyle(el);
    const before = getComputedStyle(el, "::before");
    if (before.content !== '""' || before.position === "absolute" || !/flex/.test(cs.display) || cs.alignItems !== "center")
      return null;
    const w = parseFloat(before.width);
    const h = parseFloat(before.height);
    if (!(w > 0 && h > 0)) return null;
    const painted = !clear(before.backgroundColor) || parseFloat(before.borderRightWidth) > 0 || parseFloat(before.borderBottomWidth) > 0;
    if (!painted) return null;
    const r = el.getBoundingClientRect();
    // A square drawn with a 45-degree rotation covers its diagonal.
    const ink = before.transform !== "none" ? Math.hypot(w, h) : w;
    const left = r.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft) + parseFloat(before.marginLeft) + (w - ink) / 2;
    const cy = r.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop) + (r.height - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - parseFloat(cs.borderTopWidth) - parseFloat(cs.borderBottomWidth)) / 2;
    return new DOMRect(left, cy - ink / 2, ink, ink);
  };

  // A box is anything a reader sees as a shape: fill, full border or shadow.
  // Tabs and their indicator are text, not boxes, even when they carry a fill.
  const isBox = (el) => {
    if (el.matches('[role="tab"], [role="tablist"], [data-slot="tabs-indicator"]')) return false;
    if (/^(TR|TD|TH|TBODY|THEAD|TABLE)$/.test(el.tagName)) return false;
    const cs = getComputedStyle(el);
    const fill = !clear(cs.backgroundColor);
    const border = ["Left", "Right", "Top", "Bottom"].every(
      (s) => parseFloat(cs[`border${s}Width`]) > 0 && !clear(cs[`border${s}Color`]),
    );
    const shadow = cs.boxShadow && cs.boxShadow !== "none";
    return fill || border || shadow;
  };

  // Outermost small box between an element and its container.
  const boxOf = (el, container, cr) => {
    let box = null;
    for (let a = el; a && a !== container; a = a.parentElement) {
      const r = a.getBoundingClientRect();
      if (r.width < cr.width - 4 && r.height <= 64 && isBox(a)) box = a;
    }
    return box;
  };

  const containers = [...new Set(document.querySelectorAll(containerSelector))].filter((c) => {
    const r = c.getBoundingClientRect();
    return r.width > 120 && r.height > 24 && shown(c);
  });

  // Heuristic: centred content is not edge-positioned. An item counts as centred when a
  // block-level ancestor that declares centring (text-align: center, a column flex box with
  // align-items: center, a row flex box with justify-content: center, or a grid with centred
  // items) is clearly wider than the item and the item's centre sits on that ancestor's
  // centre. Inline controls (buttons, links, pills) shrink-wrap their label, so their own
  // text-align does not count. Centred rows take no part in leading or trailing lines and
  // never miss them; their vertical centre is still checked.
  const centring = (cs) =>
    cs.textAlign === "center" ||
    (/flex/.test(cs.display) &&
      (cs.flexDirection.startsWith("column") ? cs.alignItems === "center" : cs.justifyContent === "center")) ||
    (/grid/.test(cs.display) && (cs.justifyItems === "center" || cs.justifyContent === "center"));
  const centredIn = (el, r, container) => {
    for (let a = el; a && a !== container; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.display.startsWith("inline") || cs.display === "contents") continue;
      const ar = a.getBoundingClientRect();
      const offCentre = Math.abs((ar.left + ar.right) / 2 - (r.left + r.right) / 2);
      if (ar.width - r.width > 24 && offCentre <= 1.5 && centring(cs)) return true;
    }
    return false;
  };
  // Heuristic: chart internals are drawn, not laid out. Text inside an <svg> (axis ticks,
  // axis titles, data labels) follows the plot's scale, so it is skipped. Icons stay
  // measured: they carry no text and pass the size filter below.
  const inChart = (el) => !!el.closest("svg");
  // Heuristic: an item is a trailing item only when layout pushes it to the end: at every
  // block-level ancestor up to the container it ends on that ancestor's content edge
  // (right-aligned text, the last item of a space-between row, a shrink-wrapped control).
  // Ragged-right text and wrapped paragraph lines end wherever the words run out, so they
  // take no part in the trailing line and never miss it.
  const flushEnd = (el, right, container) => {
    let x = right;
    for (let a = el; a && a !== container; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.display === "inline" || cs.display === "contents") continue;
      const r = a.getBoundingClientRect();
      if (x < r.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth) - 2) return false;
      x = r.right + Math.max(0, parseFloat(cs.marginRight));
    }
    return true;
  };

  const out = [];
  for (const c of containers) {
    const cr = c.getBoundingClientRect();
    const items = [];
    const seenBoxes = new Set();
    // Heuristic: a table body is a grid of columns, not a stack of rows. Cells with two-line
    // content (a title over a subtitle) put their lines between the centres of single-line
    // neighbours, so grouping a table by row centre invents leading and vertical-centre
    // misses. Every item inside a <td>/<th> remembers its cell and is measured per column
    // in measureTables().
    const cellOf = (el) => {
      const cell = el.closest("td, th");
      return cell && c.contains(cell) ? cell : null;
    };
    // Heuristic: panels laid side by side (KPI tiles, a two-column card grid) each own their
    // lines and are measured as their own containers. A nested container narrower than 60%
    // of this one is such a panel, so its items are left out here. Full-width nested
    // containers (stacked rows) stay in, so stacked panels still share this container's edges.
    const panels = containers.filter((o) => o !== c && c.contains(o) && o.getBoundingClientRect().width < cr.width * 0.6);
    const push = (el, r, kind, t, wrapped = false) => {
      if (panels.some((o) => o.contains(el))) return;
      items.push({ r, kind, t, cell: cellOf(el), centred: centredIn(el, r, c), flush: !wrapped && flushEnd(el, r.right, c) });
    };

    const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const text = n.textContent.trim();
      if (!text) continue;
      const el = n.parentElement;
      if (!el || skip(el) || !shown(el) || inChart(el)) continue;
      const box = boxOf(el, c, cr);
      if (box) {
        if (!seenBoxes.has(box)) {
          seenBoxes.add(box);
          push(box, box.getBoundingClientRect(), "box", `[${text.slice(0, 18)}]`);
        }
        continue;
      }
      const range = document.createRange();
      range.selectNodeContents(n);
      const rects = [...range.getClientRects()].filter((r) => r.width > 1);
      for (const r of rects) push(el, r, "text", text.slice(0, 18), rects.length > 1);
    }

    c.querySelectorAll("svg, img, input, [role='checkbox'], [role='switch']").forEach((el) => {
      if (skipIcon(el) || !shown(el) || el.parentElement?.closest("svg")) return;
      const r = el.getBoundingClientRect();
      if (r.width < 6 || r.width > 64 || r.height > 64) return;
      const box = boxOf(el.parentElement, c, cr);
      if (box) {
        if (!seenBoxes.has(box)) {
          seenBoxes.add(box);
          push(box, box.getBoundingClientRect(), "box", "[icon]");
        }
        return;
      }
      push(el, r, "icon", `<${el.tagName.toLowerCase()}>`);
    });

    c.querySelectorAll("summary, li, span, div, button, a").forEach((el) => {
      if (skip(el) || !shown(el)) return;
      const r = markerOf(el);
      if (r && !boxOf(el, c, cr)) push(el, r, "icon", "<::before>");
    });

    // Heuristic: inside table cells, empty filled shapes (progress segments, swatches, dots)
    // are ink too. Without them a cell holding a bar over a caption is measured by its
    // caption alone and its centre looks lower than its neighbours'.
    c.querySelectorAll("td *, th *").forEach((el) => {
      if (el.children.length || el.textContent.trim() || skip(el) || !shown(el) || el.closest("svg")) return;
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 2 || r.height > 24 || clear(getComputedStyle(el).backgroundColor)) return;
      push(el, r, "graphic", `<${el.tagName.toLowerCase()}>`);
    });

    const inside = items.filter(
      (i) =>
        i.r.height > 0 &&
        i.r.left >= cr.left - 1 &&
        i.r.right <= cr.right + 1 &&
        i.r.top >= cr.top - 1 &&
        i.r.bottom <= cr.bottom + 1,
    );

    const rows = [];
    const flowItems = inside.filter((i) => !i.cell);
    for (const i of flowItems.sort((a, b) => a.r.top + a.r.bottom - (b.r.top + b.r.bottom))) {
      const cy = (i.r.top + i.r.bottom) / 2;
      let row = rows.find((r) => Math.abs(r.cy - cy) < 6);
      if (!row) rows.push((row = { cy, items: [] }));
      row.items.push(i);
    }

    const rowEdges = rows.map((row) => {
      const centres = row.items.map((i) => (i.r.top + i.r.bottom) / 2);
      const base = {
        y: Math.round(row.cy - cr.top),
        centreSpread: +(Math.max(...centres) - Math.min(...centres)).toFixed(1),
        spreadText: `${row.items[0].t} … ${row.items[row.items.length - 1].t}`,
      };
      const edged = row.items.filter((i) => !i.centred);
      if (!edged.length) return { ...base, centred: true };
      const L = edged.reduce((a, b) => (a.r.left <= b.r.left ? a : b));
      const R = edged.reduce((a, b) => (a.r.right >= b.r.right ? a : b));
      const label = L.kind === "icon" ? edged.filter((i) => i.kind === "text" && i.r.left > L.r.right).sort((a, b) => a.r.left - b.r.left)[0] : undefined;
      return {
        ...base,
        left: Math.round(L.r.left - cr.left),
        leftKind: L.kind,
        lt: L.t,
        iconCentre: L.kind === "icon" ? Math.round((L.r.left + L.r.right) / 2 - cr.left) : undefined,
        label: label ? Math.round(label.r.left - cr.left) : undefined,
        labelText: label?.t,
        right: Math.round(cr.right - R.r.right),
        rightKind: R.kind,
        rt: R.t,
        trailCentre: Math.round(cr.right - (R.r.left + R.r.right) / 2),
        flush: R.flush,
      };
    });

    const dominant = (key, filter = () => true) => {
      const h = {};
      rowEdges.filter((e) => e[key] !== undefined && filter(e)).forEach((e) => (h[e[key]] = (h[e[key]] || 0) + 1));
      return Object.entries(h)
        .sort((a, b) => b[1] - a[1])
        .map(([v, count]) => ({ px: +v, count }));
    };

    // Only a row whose last item is pushed to the end (see flushEnd) and ends in the
    // trailing quarter of the container takes part in the trailing line.
    const reachesTrailing = (e) => e.right !== undefined && e.flush && e.right < cr.width / 4;
    const lines = {
      lead: dominant("left", (e) => e.leftKind !== "box")[0]?.px,
      iconCentre: dominant("iconCentre")[0]?.px,
      label: dominant("label")[0]?.px,
      box: dominant("left", (e) => e.leftKind === "box")[0]?.px,
      trailing: dominant("right", reachesTrailing)[0]?.px,
      trailCentre: dominant("trailCentre", reachesTrailing)[0]?.px,
    };

    const near = (v, line) => line !== undefined && Math.abs(v - line) <= TOL;
    const outliers = rowEdges.flatMap((e) => {
      const miss = [];
      if (e.centreSpread > 1)
        miss.push({ y: e.y, edge: "vertical-centre", px: e.centreSpread, line: 0, item: e.spreadText });
      if (e.centred) return miss;
      const leadLine = e.leftKind === "box" ? lines.box : lines.lead;
      // Heuristic: a row whose first ink starts past the container's middle holds only
      // trailing items (a "全部" link on its own line, a right-aligned action). It is
      // edge-positioned on the trailing line, so the leading line does not apply to it.
      const trailingOnly = e.left > cr.width / 2;
      const onALine = [lines.lead, lines.box, lines.label].some((l) => near(e.left, l));
      if (leadLine !== undefined && !onALine && !trailingOnly)
        miss.push({ y: e.y, edge: "leading", px: e.left, line: leadLine, item: e.lt });
      if (e.label !== undefined && !near(e.label, lines.label))
        miss.push({ y: e.y, edge: "label", px: e.label, line: lines.label, item: e.labelText });
      if (e.iconCentre !== undefined && !near(e.iconCentre, lines.iconCentre))
        miss.push({ y: e.y, edge: "icon-column", px: e.iconCentre, line: lines.iconCentre, item: e.lt });
      if (reachesTrailing(e) && !near(e.right, lines.trailing) && !near(e.trailCentre, lines.trailCentre))
        miss.push({ y: e.y, edge: "trailing", px: e.right, line: lines.trailing, item: e.rt });
      return miss;
    });
    const tables = measureTables(inside.filter((i) => i.cell), cr, lines, near);
    outliers.push(...tables.outliers);

    const name =
      c.getAttribute("aria-label") ||
      c.querySelector("h1, h2, h3, [role='heading']")?.textContent.trim().slice(0, 30) ||
      c.dataset.slot ||
      c.tagName.toLowerCase();
    out.push({ container: name, x: Math.round(cr.left), w: Math.round(cr.width), lines, outliers, rows: rowEdges, tables: tables.summary });

    if (draw) {
      const guide = (x, colour) => {
        const d = document.createElement("div");
        d.dataset.gridline = "";
        Object.assign(d.style, {
          position: "fixed",
          left: `${x}px`,
          top: `${cr.top}px`,
          height: `${cr.height}px`,
          width: "0",
          borderLeft: `1px dashed ${colour}`,
          zIndex: 2147483646,
          pointerEvents: "none",
        });
        document.body.appendChild(d);
      };
      if (lines.lead !== undefined) guide(cr.left + lines.lead, "rgba(255,90,90,0.95)");
      if (lines.box !== undefined && lines.box !== lines.lead) guide(cr.left + lines.box, "rgba(255,90,90,0.5)");
      if (lines.iconCentre !== undefined) guide(cr.left + lines.iconCentre, "rgba(255,90,90,0.35)");
      if (lines.label !== undefined) guide(cr.left + lines.label, "rgba(255,90,90,0.95)");
      if (lines.trailing !== undefined) guide(cr.right - lines.trailing, "rgba(90,200,255,0.95)");
      if (lines.trailCentre !== undefined) guide(cr.right - lines.trailCentre, "rgba(255,190,60,0.6)");
    }
  }
  return out;
};
// Per-column table measurement. A cell's ink is the union of its items, so a two-line cell
// is one block, not two rows. Within a column, left-aligned cells share a text line (boxed
// cells such as status pills may sit on a box line instead), right-aligned cells share a
// trailing edge, and centred columns are skipped. Within a body row, cells with
// vertical-align: middle share an ink centre; other cells compare their first line. The
// table then joins its container: the first column's line must sit on the container's text
// or box line, and a right-aligned last column must end on the container's trailing line.
// A misplaced column is reported once per cell that misses, never once per text line.
function measureTables(cellItems, cr, lines, near) {
  const outliers = [];
  const summary = [];
  const mode = (values) => {
    const h = {};
    for (const v of values) h[v] = (h[v] || 0) + 1;
    const top = Object.entries(h).sort((a, b) => b[1] - a[1])[0];
    return top === undefined ? undefined : top[0];
  };
  const group = (list, key) => {
    const map = new Map();
    for (const x of list) {
      const k = key(x);
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(x);
    }
    return map;
  };
  const cells = [...group(cellItems, (i) => i.cell)].map(([cell, its]) => {
    const cs = getComputedStyle(cell);
    const edged = its.filter((i) => !i.centred);
    const top = Math.min(...its.map((i) => i.r.top));
    const firstLine = its.filter((i) => i.r.top < top + 4);
    const L = edged.length ? edged.reduce((a, b) => (a.r.left <= b.r.left ? a : b)) : null;
    let col = 0;
    for (let s = cell.previousElementSibling; s; s = s.previousElementSibling) col += s.colSpan || 1;
    const align = { end: "right", start: "left", "-webkit-right": "right", "-webkit-center": "center" }[cs.textAlign] || cs.textAlign;
    return {
      table: cell.closest("table"),
      row: cell.parentElement,
      col,
      head: !!cell.closest("thead"),
      align: L ? align : "center",
      middle: cs.verticalAlign === "middle",
      left: L ? Math.round(L.r.left - cr.left) : undefined,
      leftKind: L?.kind,
      right: edged.length ? Math.round(cr.right - Math.max(...edged.map((i) => i.r.right))) : undefined,
      centre: (top + Math.max(...its.map((i) => i.r.bottom))) / 2,
      firstCentre: firstLine.reduce((a, i) => a + (i.r.top + i.r.bottom) / 2, 0) / firstLine.length,
      t: (L || its[0]).t,
      y: Math.round(top - cr.top),
    };
  });
  for (const list of group(cells, (c) => c.table).values()) {
    const colLines = [];
    const columns = [...group(list, (c) => c.col)].sort((a, b) => a[0] - b[0]);
    for (const [col, members] of columns) {
      const align = mode(members.map((c) => c.align));
      if (align === "center") continue;
      if (align === "right") {
        const line = +mode(members.map((c) => c.right));
        colLines.push({ col, align, line });
        for (const c of members)
          if (c.align === "right" && !near(c.right, line))
            outliers.push({ y: c.y, edge: "column-trailing", px: c.right, line, item: c.t });
        continue;
      }
      const text = members.filter((c) => c.align === "left" && c.leftKind !== "box");
      const boxes = members.filter((c) => c.align === "left" && c.leftKind === "box");
      const textLine = text.length ? +mode(text.map((c) => c.left)) : undefined;
      const boxLine = boxes.length ? +mode(boxes.map((c) => c.left)) : undefined;
      colLines.push({ col, align: "left", line: textLine ?? boxLine, text: textLine, box: boxLine });
      for (const c of [...text, ...boxes])
        if (!near(c.left, textLine) && !near(c.left, boxLine))
          outliers.push({ y: c.y, edge: "column-leading", px: c.left, line: c.leftKind === "box" ? boxLine : textLine, item: c.t });
    }
    for (const row of group(list.filter((c) => !c.head), (c) => c.row).values()) {
      const centres = row.map((c) => (c.middle ? c.centre : c.firstCentre));
      const spread = +(Math.max(...centres) - Math.min(...centres)).toFixed(1);
      if (spread > 1)
        outliers.push({ y: row[0].y, edge: "vertical-centre", px: spread, line: 0, item: `${row[0].t} … ${row[row.length - 1].t}` });
    }
    const first = colLines[0];
    if (first?.col === 0 && first.align === "left" && (lines.lead !== undefined || lines.box !== undefined)) {
      const sits = [first.text, first.box].some((v) => v !== undefined && (near(v, lines.lead) || near(v, lines.box)));
      if (!sits) outliers.push({ y: 0, edge: "table-leading", px: first.line, line: lines.lead ?? lines.box, item: `表格首列：${list[0].t}` });
    }
    const last = colLines[colLines.length - 1];
    if (last?.align === "right" && lines.trailing !== undefined && last.line < cr.width / 4 && !near(last.line, lines.trailing))
      outliers.push({ y: 0, edge: "table-trailing", px: last.line, line: lines.trailing, item: `表格末列：${list[list.length - 1].t}` });
    summary.push({ columns: colLines });
  }
  return { outliers, summary };
}

window.__gridlines.containers = GRIDLINE_CONTAINERS;
