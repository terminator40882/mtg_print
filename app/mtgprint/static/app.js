/* MTG Print Studio – Web-Oberfläche (ohne Build-Schritt, ohne externe Abhängigkeiten) */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n, d = 1) => Number(n).toLocaleString('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d });

const S = {
  info: null, settings: null,
  cards: [], cardMap: {},
  projects: [], project: null, geo: null,
  sel: null,            // {p, s}
  clipboard: null,
  history: [], future: [],
  view: 'editor',
  filter: '',
  libSel: new Set(),
  showCuts: true, showBleed: true, zoom: 1,
  printer: null, calib: null,
};

// ------------------------------------------------------------------ API
async function api(method, url, body, isForm = false) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(url, opt);
  let data = null;
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('json')) data = await r.json();
  if (!r.ok) {
    let msg = data && data.detail ? data.detail : `${r.status} ${r.statusText}`;
    if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join(', ');
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = '', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function guard(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg, 'ok');
    return r;
  } catch (e) {
    console.error(e);
    toast(e.message || String(e), 'bad', 7000);
    return undefined;
  }
}

// ------------------------------------------------------------------ Modal
function modal({ title, body, buttons = [], wide = false, onOpen }) {
  return new Promise((resolve) => {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal ${wide ? 'wide' : ''}">
      <header><h2>${esc(title)}</h2><button class="x" data-x>×</button></header>
      <div class="body">${body}</div>
      <footer></footer></div>`;
    const footer = $('footer', bg);
    const close = (v) => { bg.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    buttons.forEach((b) => {
      const btn = document.createElement('button');
      btn.className = `btn ${b.cls || ''}`;
      btn.textContent = b.label;
      btn.onclick = async () => {
        if (b.onClick) {
          btn.disabled = true;
          const res = await b.onClick(bg);
          btn.disabled = false;
          if (res === false) return;
          close(res === undefined ? b.value : res);
        } else close(b.value);
      };
      footer.appendChild(btn);
    });
    $('[data-x]', bg).onclick = () => close(null);
    bg.addEventListener('mousedown', (e) => { if (e.target === bg) close(null); });
    const onKey = (e) => {
      if (e.key === 'Escape') close(null);
      if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'file') {
        const primary = $('footer .primary', bg);
        if (primary) { e.preventDefault(); primary.click(); }
      }
    };
    document.addEventListener('keydown', onKey);
    $('#modal-root').appendChild(bg);
    if (onOpen) onOpen(bg, close);
    const first = $('[autofocus]', bg);
    if (first) setTimeout(() => { first.focus(); first.select && first.select(); }, 30);
  });
}

async function prompt(title, label, value = '') {
  let v = null;
  const r = await modal({
    title,
    body: `<div class="form"><label>${esc(label)}</label><input type="text" id="prompt-in" value="${esc(value)}" autofocus></div>`,
    buttons: [{ label: 'Abbrechen' }, { label: 'OK', cls: 'primary', onClick: (bg) => { v = $('#prompt-in', bg).value.trim(); return v; } }],
  });
  return r === null ? null : v;
}

async function confirmBox(title, text, okLabel = 'OK', danger = false) {
  const r = await modal({ title, body: `<p>${text}</p>`, buttons: [{ label: 'Abbrechen', value: false }, { label: okLabel, cls: danger ? 'danger' : 'primary', value: true }] });
  return r === true;
}

// ------------------------------------------------------------------ Karten-Thumbnails
function thumbHTML(card, extra = '') {
  if (!card) return `<div class="cardthumb ${extra}"></div>`;
  const fx = card.fx || 0, fy = card.fy || 0;
  const w = 100 / (1 - 2 * fx), h = 100 / (1 - 2 * fy);
  const l = -fx * w, t = -fy * h;
  return `<div class="cardthumb ${extra}"><img loading="lazy" src="${card.thumb}" style="width:${w}%;height:${h}%;left:${l}%;top:${t}%"></div>`;
}

function cardWarnings(c) {
  const w = [];
  if (c.dpi < 250) w.push({ cls: 'bad', text: `${c.dpi} DPI – unscharf` });
  else if (c.dpi < 300) w.push({ cls: 'warn', text: `${c.dpi} DPI` });
  const hasPad = !['none'].includes(c.bleed_mode);
  if (c.suggested_mode === 'none' && hasPad && c.bleed_mode !== 'custom') w.push({ cls: 'warn', text: 'Sieht aus wie ohne Padding' });
  if (c.suggested_mode === 'mpc' && c.bleed_mode === 'none') w.push({ cls: 'warn', text: 'Sieht aus wie mit Padding' });
  const presetMm = { mpc: 3.048, eighth: 3.175 }[c.bleed_mode];
  if (presetMm && c.detected_bleed_mm > 0.5 && Math.abs(c.detected_bleed_mm - presetMm) > 0.4) {
    w.push({ cls: 'warn', text: `Bild hat eher ${fmt(c.detected_bleed_mm, 2)} mm Bleed` });
  }
  return w;
}

// ------------------------------------------------------------------ Daten laden
async function loadCards() {
  S.cards = await api('GET', '/api/cards');
  S.cardMap = Object.fromEntries(S.cards.map((c) => [c.id, c]));
}

async function loadProjects() {
  S.projects = await api('GET', '/api/projects');
}

async function openProject(id, keepHistory = false) {
  const p = await api('GET', `/api/projects/${id}`);
  setProject(p);
  if (!keepHistory) { S.history = []; S.future = []; }
  if (!S.sel || S.sel.p >= p.pages.length) S.sel = { p: 0, s: 0 };
  renderEditor();
}

function setProject(p) {
  S.project = p;
  S.geo = p.geometry;
}

// ------------------------------------------------------------------ Projekt-Mutationen (mit Undo)
let saveTimer = null;
function snapshot() { return JSON.stringify(S.project.pages); }
function mutate(fn, { render = true } = {}) {
  S.history.push(snapshot());
  if (S.history.length > 80) S.history.shift();
  S.future = [];
  fn(S.project.pages);
  normalizePages();
  scheduleSave();
  if (render) renderEditor();
}
function normalizePages() {
  const per = S.geo.slots.length;
  const pages = S.project.pages;
  if (!pages.length) pages.push(Array(per).fill(null));
  pages.forEach((p) => { while (p.length < per) p.push(null); p.length = per; });
}
function undo() {
  if (!S.history.length) return toast('Nichts zum Rückgängigmachen');
  S.future.push(snapshot());
  S.project.pages = JSON.parse(S.history.pop());
  scheduleSave(); renderEditor();
}
function redo() {
  if (!S.future.length) return;
  S.history.push(snapshot());
  S.project.pages = JSON.parse(S.future.pop());
  scheduleSave(); renderEditor();
}
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 400);
}
async function saveNow() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!S.project) return;
  await guard(async () => {
    const p = await api('PUT', `/api/projects/${S.project.id}`, { pages: S.project.pages });
    S.project.updated = p.updated;
  });
}
async function flushSave() { if (saveTimer) await saveNow(); }

const flat = () => S.project.pages.flat();
function fromFlat(list) {
  const per = S.geo.slots.length;
  while (list.length && list[list.length - 1] == null) list.pop();
  const pages = [];
  for (let i = 0; i < list.length; i += per) pages.push(list.slice(i, i + per));
  if (!pages.length) pages.push([]);
  pages.forEach((p) => { while (p.length < per) p.push(null); });
  return pages;
}
function appendCards(ids) {
  mutate((pages) => {
    const list = pages.flat();
    while (list.length && list[list.length - 1] == null) list.pop();
    const per = S.geo.slots.length;
    const startIdx = list.length;
    list.push(...ids);
    S.project.pages = fromFlat(list);
    S.sel = { p: Math.floor((startIdx + ids.length) / per), s: (startIdx + ids.length) % per };
    if (S.sel.p >= S.project.pages.length) S.sel = { p: S.project.pages.length - 1, s: per - 1 };
  });
}
function placeInSelected(cardId) {
  if (!S.sel) S.sel = { p: 0, s: 0 };
  const per = S.geo.slots.length;
  mutate((pages) => {
    pages[S.sel.p][S.sel.s] = cardId;
    // Auswahl weiterschieben -> schnelles Befüllen per Klick
    let { p, s } = S.sel;
    s++;
    if (s >= per) { s = 0; p++; if (p >= pages.length) pages.push(Array(per).fill(null)); }
    S.sel = { p, s };
  });
}
function nextFree(p, s) {
  const per = S.geo.slots.length;
  const list = flat();
  for (let i = p * per + s + 1; i < list.length; i++) if (!list[i]) return { p: Math.floor(i / per), s: i % per };
  return null;
}
function duplicateSlot(p, s) {
  const id = S.project.pages[p][s];
  if (!id) return;
  const nf = nextFree(p, s);
  mutate((pages) => {
    if (nf) pages[nf.p][nf.s] = id;
    else { const per = S.geo.slots.length; const np = Array(per).fill(null); np[0] = id; pages.push(np); }
  });
}

// ------------------------------------------------------------------ Rendering: Editor
function pxPerMm() { return (420 * S.zoom) / S.geo.page_w; }

function renderEditor() {
  if (!S.project) return;
  renderProjectSelect();
  renderSideLib();
  renderPages();
  renderSummary();
  renderSlotPanel();
  renderProjectLayout();
}

function renderProjectSelect() {
  const sel = $('#project-select');
  sel.innerHTML = S.projects.map((p) => `<option value="${p.id}" ${p.id === S.project.id ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
  if (!S.projects.find((p) => p.id === S.project.id)) sel.insertAdjacentHTML('afterbegin', `<option value="${S.project.id}" selected>${esc(S.project.name)}</option>`);
}

function usageCounts() {
  const counts = {};
  flat().forEach((id) => { if (id) counts[id] = (counts[id] || 0) + 1; });
  return counts;
}

function filteredCards() {
  const f = S.filter.toLowerCase();
  return S.cards.filter((c) => !f || c.name.toLowerCase().includes(f) || (c.original_name || '').toLowerCase().includes(f));
}

function renderSideLib() {
  const counts = usageCounts();
  const list = filteredCards();
  const box = $('#side-lib');
  if (!S.cards.length) {
    box.innerHTML = `<div class="empty-state" style="padding:30px 6px">Noch keine Karten.<br><br><button class="btn primary small" data-action="open-upload">Bilder hochladen</button></div>`;
    return;
  }
  box.innerHTML = list.map((c) => {
    const warn = cardWarnings(c).length ? '<span class="warn-dot">!</span>' : '';
    return `<div class="lib-item" draggable="true" data-card="${c.id}" title="${esc(c.name)}">
      ${thumbHTML(c)}
      ${counts[c.id] ? `<span class="count">${counts[c.id]}×</span>` : ''}
      <button class="add" data-add="${c.id}" title="Ans Ende anhängen (Shift: 4×)">+</button>${warn}
      <div class="name">${esc(c.name)}</div></div>`;
  }).join('') || '<div class="empty-state">Keine Treffer</div>';
}

function visRect(slot, card) {
  const fx = card ? card.fx || 0 : 0, fy = card ? card.fy || 0 : 0;
  const imgW = slot.w / (1 - 2 * fx), imgH = slot.h / (1 - 2 * fy);
  const ax = fx * imgW, ay = fy * imgH;
  const b = S.showBleed ? slot.bleed : { l: 0, r: 0, t: 0, b: 0 };
  const l = Math.min(b.l, ax), r = Math.min(b.r, ax), t = Math.min(b.t, ay), bt = Math.min(b.b, ay);
  return { x: slot.x - l, y: slot.y - t, w: slot.w + l + r, h: slot.h + t + bt, ix: l - ax, iy: t - ay, iw: imgW, ih: imgH };
}

function renderPages() {
  const k = pxPerMm();
  const g = S.geo;
  const lay = S.project.layout;
  const W = g.page_w * k, H = g.page_h * k;
  const html = S.project.pages.map((page, pi) => {
    const filled = page.filter(Boolean).length;
    let inner = '';
    g.slots.forEach((slot, si) => {
      const id = page[si];
      const card = id ? S.cardMap[id] : null;
      if (card) {
        const v = visRect(slot, card);
        inner += `<div class="slot-img" style="left:${v.x * k}px;top:${v.y * k}px;width:${v.w * k}px;height:${v.h * k}px">
          <img src="${card.thumb}" draggable="false" style="left:${v.ix * k}px;top:${v.iy * k}px;width:${v.iw * k}px;height:${v.ih * k}px"></div>`;
      }
      const sel = S.sel && S.sel.p === pi && S.sel.s === si ? 'sel' : '';
      const low = card && card.dpi < 300 ? `<span class="lowdpi" title="Effektive Auflösung">${card.dpi} DPI</span>` : '';
      inner += `<div class="slot-hit ${card ? '' : 'empty'} ${sel}" data-p="${pi}" data-s="${si}" draggable="${card ? 'true' : 'false'}"
        style="left:${slot.x * k}px;top:${slot.y * k}px;width:${slot.w * k}px;height:${slot.h * k}px">
        ${card ? `<div class="lbl">${si + 1} · ${esc(card.name)}</div>${low}` : (id ? '<small>fehlt</small>' : si + 1)}</div>`;
    });
    // Schnittmarken (wie im PDF) und optionale Schnittlinien
    if (lay.cut_marks) {
      const off = g.outer_bleed + lay.cut_mark_gap, len = lay.cut_mark_len;
      const top = g.grid.y - off, bot = g.grid.y + g.grid.h + off, left = g.grid.x - off, right = g.grid.x + g.grid.w + off;
      const lw = Math.max(1, lay.cut_mark_width * k);
      g.cuts_x.forEach((x) => {
        const y0 = len ? Math.max(0, top - len) : 0, y1 = len ? Math.min(g.page_h, bot + len) : g.page_h;
        inner += `<div class="cutmark" style="left:${x * k - lw / 2}px;top:${y0 * k}px;width:${lw}px;height:${(top - y0) * k}px"></div>`;
        inner += `<div class="cutmark" style="left:${x * k - lw / 2}px;top:${bot * k}px;width:${lw}px;height:${(y1 - bot) * k}px"></div>`;
      });
      g.cuts_y.forEach((y) => {
        const x0 = len ? Math.max(0, left - len) : 0, x1 = len ? Math.min(g.page_w, right + len) : g.page_w;
        inner += `<div class="cutmark" style="top:${y * k - lw / 2}px;left:${x0 * k}px;height:${lw}px;width:${(left - x0) * k}px"></div>`;
        inner += `<div class="cutmark" style="top:${y * k - lw / 2}px;left:${right * k}px;height:${lw}px;width:${(x1 - right) * k}px"></div>`;
      });
    }
    if (S.showCuts) {
      g.cuts_x.forEach((x) => { inner += `<div class="cutline v" style="left:${x * k}px;top:0;height:${H}px"></div>`; });
      g.cuts_y.forEach((y) => { inner += `<div class="cutline h" style="top:${y * k}px;left:0;width:${W}px"></div>`; });
    }
    return `<div class="page-wrap">
      <div class="page-head"><b>Seite ${pi + 1}</b><span class="muted">${filled}/${page.length}</span>
        <button class="btn" data-page-act="up" data-p="${pi}" title="Nach vorne">↑</button>
        <button class="btn" data-page-act="down" data-p="${pi}" title="Nach hinten">↓</button>
        <button class="btn" data-page-act="dup" data-p="${pi}" title="Seite duplizieren">⧉</button>
        <button class="btn" data-page-act="clear" data-p="${pi}" title="Seite leeren">⌫</button>
        <button class="btn" data-page-act="del" data-p="${pi}" title="Seite löschen">🗑</button></div>
      <div class="page ${S.sel && S.sel.p === pi ? 'current' : ''}" style="width:${W}px;height:${H}px" data-page="${pi}">${inner}</div></div>`;
  }).join('');
  $('#pages').innerHTML = html + `<div class="page-wrap"><div class="page-head"><b>&nbsp;</b></div>
    <button class="btn" data-action="add-page" style="width:${W}px;height:${H}px;border-style:dashed;font-size:16px;color:var(--muted)">+ Seite hinzufügen</button></div>`;
}

function renderSummary() {
  const n = flat().filter(Boolean).length;
  const g = S.geo;
  const lay = S.project.layout;
  const warn = g.fits ? '' : '<div class="alert warn wide">Raster + Bleed passt nicht aufs Papier!</div>';
  $('#project-summary').innerHTML = `
    <div class="k"><b>${n}</b><span>Karten</span></div>
    <div class="k"><b>${S.project.pages.length}</b><span>Seiten</span></div>
    <div class="k"><b>${g.cols}×${g.rows}</b><span>Raster</span></div>
    <div class="k"><b>${g.num_cuts}</b><span>Schnitte / Bogen</span></div>
    <div class="k wide"><span>Kartengröße</span> <b style="font-size:14px">${fmt(lay.card_w)} × ${fmt(lay.card_h)} mm</b></div>${warn}`;
}

function renderSlotPanel() {
  const box = $('#slot-panel');
  if (!S.sel || !S.project.pages[S.sel.p]) { box.innerHTML = '<div class="muted">Kein Slot gewählt</div>'; return; }
  const id = S.project.pages[S.sel.p][S.sel.s];
  const card = id ? S.cardMap[id] : null;
  const head = `<div class="muted" style="margin-bottom:6px">Seite ${S.sel.p + 1} · Slot ${S.sel.s + 1}</div>`;
  if (!card) {
    box.innerHTML = head + '<div class="hint">Leerer Slot. Karte links anklicken, um sie hier einzusetzen (danach springt die Auswahl automatisch weiter).</div>';
    return;
  }
  const warns = cardWarnings(card).map((w) => `<span class="badge ${w.cls}">${esc(w.text)}</span>`).join(' ');
  box.innerHTML = head + `<div class="slotinfo">${thumbHTML(card)}<div class="meta"><b>${esc(card.name)}</b>
      ${card.w}×${card.h}px · ${card.dpi} DPI<br>Padding: ${esc(S.info.bleed_modes[card.bleed_mode] || card.bleed_mode)}<br>${warns}</div></div>
    <div class="btn-col" style="margin-top:8px">
      <div class="btn-row"><button class="btn small" data-slot-act="dup">Duplizieren</button><button class="btn small" data-slot-act="fill">Seite füllen</button></div>
      <div class="btn-row"><button class="btn small" data-slot-act="edit">Bild-Einstellungen</button><button class="btn small danger-ghost" data-slot-act="clear">Leeren</button></div>
    </div>`;
}

// ------------------------------------------------------------------ Layout-Formular (Projekt & Standard)
function layoutFormHTML(l, prefix) {
  const num = (key, label, step = 0.1, unit = 'mm', note = '') => `
    <label for="${prefix}-${key}">${label}</label>
    <div class="unit"><input type="number" id="${prefix}-${key}" data-key="${key}" step="${step}" value="${l[key]}"> ${unit}</div>
    ${note ? `<div class="note">${note}</div>` : ''}`;
  const chk = (key, label) => `<label class="chk full"><input type="checkbox" data-key="${key}" ${l[key] ? 'checked' : ''}> ${label}</label>`;
  return `<div class="form" data-layout-form="${prefix}">
    <h4>Papier & Karten</h4>
    <label>Papier</label><select data-key="paper">${S.info.papers.map((p) => `<option ${p === l.paper ? 'selected' : ''}>${p}</option>`).join('')}</select>
    ${num('card_w', 'Kartenbreite')}
    ${num('card_h', 'Kartenhöhe', 0.1, 'mm', 'Original Magic: 63 × 88 mm')}
    ${num('cols', 'Spalten', 1, '', '')}
    ${num('rows', 'Zeilen', 1, '', '0 = automatisch (A4 → 3 × 3)')}
    ${num('gap', 'Abstand', 0.1, 'mm', '0 = Karten stoßen aneinander (8 Schnitte). >0 = Puffer mit Bleed gefüllt, doppelte Schnitte.')}
    <h4>Bleed & Position</h4>
    ${num('outer_bleed', 'Außen-Bleed', 0.1, 'mm', 'Bild ragt an den Außenkanten so weit über die Schnittlinie.')}
    ${num('synthetic_bleed', 'Künstl. Bleed', 0.1, 'mm', 'Für Bilder ohne Padding (Randpixel werden verlängert).')}
    ${num('offset_x', 'Raster-Versatz X')}
    ${num('offset_y', 'Raster-Versatz Y', 0.1, 'mm', 'Bewusste Verschiebung. Druckerfehler bitte über „Kalibrierung“ ausgleichen.')}
    <h4>Schnittmarken & Ausgabe</h4>
    ${chk('cut_marks', 'Schnittmarken im Rand drucken')}
    ${num('cut_mark_len', 'Markenlänge', 0.5, 'mm', '0 = bis zum Blattrand (ideal für Schneidemaschinen)')}
    ${num('cut_mark_width', 'Linienstärke', 0.05)}
    ${num('cut_mark_gap', 'Abstand z. Bild', 0.1)}
    ${chk('page_label', 'Projektname/Seitenzahl in den Rand drucken')}
    <label>Bildqualität</label><select data-key="quality">
      <option value="high" ${l.quality === 'high' ? 'selected' : ''}>Hoch (max. 1200 DPI, JPEG 95)</option>
      <option value="original" ${l.quality === 'original' ? 'selected' : ''}>Original (verlustfrei, große PDFs)</option>
      <option value="standard" ${l.quality === 'standard' ? 'selected' : ''}>Standard (max. 600 DPI, kleiner)</option>
    </select></div>`;
}

function readLayoutForm(root) {
  const out = {};
  $$('[data-key]', root).forEach((el) => {
    const k = el.dataset.key;
    if (el.type === 'checkbox') out[k] = el.checked;
    else if (el.type === 'number') out[k] = el.value === '' ? 0 : Number(el.value);
    else out[k] = el.value;
  });
  return out;
}

function renderProjectLayout() {
  const box = $('#project-layout');
  if (box.contains(document.activeElement)) return; // nicht während der Eingabe neu zeichnen
  box.innerHTML = layoutFormHTML(S.project.layout, 'pl');
}

let layoutTimer = null;
function onProjectLayoutChange() {
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(async () => {
    const layout = readLayoutForm($('#project-layout'));
    await flushSave();
    const p = await guard(() => api('PUT', `/api/projects/${S.project.id}`, { layout }));
    if (p) {
      setProject(p);
      if (S.sel && S.sel.s >= S.geo.slots.length) S.sel = { p: 0, s: 0 };
      renderPages(); renderSummary(); renderSlotPanel();
    }
  }, 350);
}

// ------------------------------------------------------------------ Editor-Events
function slotFromEl(el) {
  const hit = el.closest('.slot-hit');
  return hit ? { p: +hit.dataset.p, s: +hit.dataset.s } : null;
}

function setupEditorEvents() {
  const pages = $('#pages');
  pages.addEventListener('click', (e) => {
    const pa = e.target.closest('[data-page-act]');
    if (pa) return pageAction(pa.dataset.pageAct, +pa.dataset.p);
    const s = slotFromEl(e.target);
    if (s) { S.sel = s; renderPages(); renderSlotPanel(); }
  });
  pages.addEventListener('dblclick', (e) => {
    const s = slotFromEl(e.target);
    if (s) { const id = S.project.pages[s.p][s.s]; if (id) openCardDetail(id); }
  });
  pages.addEventListener('dragstart', (e) => {
    const s = slotFromEl(e.target);
    if (!s) return;
    e.dataTransfer.setData('text/x-slot', `${s.p}:${s.s}`);
    e.dataTransfer.effectAllowed = 'copyMove';
  });
  pages.addEventListener('dragover', (e) => {
    const types = e.dataTransfer.types;
    if (!types.includes('text/x-slot') && !types.includes('text/x-card')) return;
    const hit = e.target.closest('.slot-hit');
    if (!hit) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = e.altKey || types.includes('text/x-card') ? 'copy' : 'move';
    $$('.slot-hit.over').forEach((x) => x !== hit && x.classList.remove('over'));
    hit.classList.add('over');
  });
  pages.addEventListener('dragleave', (e) => { const hit = e.target.closest('.slot-hit'); if (hit) hit.classList.remove('over'); });
  pages.addEventListener('drop', (e) => {
    const to = slotFromEl(e.target);
    $$('.slot-hit.over').forEach((x) => x.classList.remove('over'));
    if (!to) return;
    const cardId = e.dataTransfer.getData('text/x-card');
    const from = e.dataTransfer.getData('text/x-slot');
    if (!cardId && !from) return;
    e.preventDefault();
    e.stopPropagation();
    if (cardId) {
      mutate((pg) => { pg[to.p][to.s] = cardId; });
    } else {
      const [fp, fs] = from.split(':').map(Number);
      if (fp === to.p && fs === to.s) return;
      mutate((pg) => {
        const a = pg[fp][fs];
        if (e.altKey || e.ctrlKey) pg[to.p][to.s] = a;
        else { pg[fp][fs] = pg[to.p][to.s]; pg[to.p][to.s] = a; }
      });
    }
    S.sel = to;
    renderPages(); renderSlotPanel();
  });

  const side = $('#side-lib');
  side.addEventListener('click', (e) => {
    const add = e.target.closest('[data-add]');
    if (add) { appendCards(Array(e.shiftKey ? 4 : 1).fill(add.dataset.add)); return; }
    const item = e.target.closest('[data-card]');
    if (item) placeInSelected(item.dataset.card);
  });
  side.addEventListener('dblclick', (e) => { const item = e.target.closest('[data-card]'); if (item) openCardDetail(item.dataset.card); });
  side.addEventListener('dragstart', (e) => {
    const item = e.target.closest('[data-card]');
    if (!item) return;
    e.dataTransfer.setData('text/x-card', item.dataset.card);
    e.dataTransfer.effectAllowed = 'copy';
  });
  // Slot auf die Bibliothek ziehen = entfernen
  side.addEventListener('dragover', (e) => {
    if (e.dataTransfer.types.includes('text/x-slot')) { e.preventDefault(); side.classList.add('drop-remove'); }
  });
  side.addEventListener('dragleave', () => side.classList.remove('drop-remove'));
  side.addEventListener('drop', (e) => {
    side.classList.remove('drop-remove');
    const from = e.dataTransfer.getData('text/x-slot');
    if (!from) return;
    e.preventDefault();
    const [fp, fs] = from.split(':').map(Number);
    mutate((pg) => { pg[fp][fs] = null; });
  });

  $('#slot-panel').addEventListener('click', (e) => {
    const b = e.target.closest('[data-slot-act]');
    if (!b || !S.sel) return;
    const { p, s } = S.sel;
    const id = S.project.pages[p][s];
    if (b.dataset.slotAct === 'clear') mutate((pg) => { pg[p][s] = null; });
    if (b.dataset.slotAct === 'dup') duplicateSlot(p, s);
    if (b.dataset.slotAct === 'fill') mutate((pg) => { pg[p] = pg[p].map(() => id); });
    if (b.dataset.slotAct === 'edit') openCardDetail(id);
  });

  $('#project-layout').addEventListener('input', onProjectLayoutChange);
  $('#project-layout').addEventListener('change', onProjectLayoutChange);
  $('#project-select').addEventListener('change', async (e) => { await flushSave(); openProject(e.target.value); });
  $('#lib-search').addEventListener('input', (e) => { S.filter = e.target.value; renderSideLib(); });
  $('#toggle-cuts').addEventListener('change', (e) => { S.showCuts = e.target.checked; renderPages(); });
  $('#toggle-bleed').addEventListener('change', (e) => { S.showBleed = e.target.checked; renderPages(); });
  $('#zoom').addEventListener('input', (e) => { S.zoom = +e.target.value; try { localStorage.setItem('zoom', S.zoom); } catch (_) {} renderPages(); });

  document.addEventListener('keydown', (e) => {
    if (S.view !== 'editor' || $('.modal-bg')) return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && e.key.toLowerCase() === 'z' && !e.shiftKey) { e.preventDefault(); return undo(); }
    if ((ctrl && e.key.toLowerCase() === 'y') || (ctrl && e.shiftKey && e.key.toLowerCase() === 'z')) { e.preventDefault(); return redo(); }
    if (!S.sel) return;
    const per = S.geo.slots.length, cols = S.geo.cols;
    let idx = S.sel.p * per + S.sel.s;
    const total = S.project.pages.length * per;
    const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
    if (move) {
      e.preventDefault();
      idx = Math.min(total - 1, Math.max(0, idx + move));
      S.sel = { p: Math.floor(idx / per), s: idx % per };
      renderPages(); renderSlotPanel();
      const el = $(`.slot-hit[data-p="${S.sel.p}"][data-s="${S.sel.s}"]`);
      if (el) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      return;
    }
    const { p, s } = S.sel;
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); mutate((pg) => { pg[p][s] = null; }); }
    if (ctrl && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSlot(p, s); }
    if (ctrl && e.key.toLowerCase() === 'c') { S.clipboard = S.project.pages[p][s]; if (S.clipboard) toast('Karte kopiert'); }
    if (ctrl && e.key.toLowerCase() === 'v' && S.clipboard) { e.preventDefault(); placeInSelected(S.clipboard); }
  });
}

function pageAction(act, pi) {
  const per = S.geo.slots.length;
  if (act === 'up' && pi > 0) mutate((pg) => { [pg[pi - 1], pg[pi]] = [pg[pi], pg[pi - 1]]; });
  if (act === 'down' && pi < S.project.pages.length - 1) mutate((pg) => { [pg[pi + 1], pg[pi]] = [pg[pi], pg[pi + 1]]; });
  if (act === 'dup') mutate((pg) => { pg.splice(pi + 1, 0, [...pg[pi]]); });
  if (act === 'clear') mutate((pg) => { pg[pi] = Array(per).fill(null); });
  if (act === 'del') {
    mutate((pg) => { pg.splice(pi, 1); });
    if (S.sel && S.sel.p >= S.project.pages.length) S.sel = { p: S.project.pages.length - 1, s: 0 };
    toast('Seite gelöscht – Strg+Z macht es rückgängig');
  }
}

// ------------------------------------------------------------------ Bibliothek-Ansicht
function renderLibrary() {
  const list = filteredCards();
  const counts = S.project ? usageCounts() : {};
  $('#lib-selcount').textContent = S.libSel.size ? `${S.libSel.size} ausgewählt` : `${S.cards.length} Karten`;
  const grid = $('#lib-grid');
  if (!S.cards.length) {
    grid.innerHTML = `<div class="empty-state"><h2>Noch keine Karten</h2><p>Ziehe MPC-Fill-Bilder (oder einen ganzen Ordner / eine ZIP) einfach ins Fenster.</p>
      <button class="btn primary" data-action="open-upload">Bilder hochladen</button></div>`;
    return;
  }
  grid.innerHTML = list.map((c) => {
    const badges = [
      `<span class="badge">${c.bleed_mode === 'none' ? 'ohne Padding' : c.bleed_mode === 'custom' ? `Padding ${fmt(c.bleed_mm)} mm` : c.bleed_mode === 'auto' ? 'Padding auto' : c.bleed_mode === 'eighth' ? 'Padding 1/8"' : 'MPC-Padding'}</span>`,
      `<span class="badge ${c.dpi < 250 ? 'bad' : c.dpi < 300 ? 'warn' : 'ok'}">${c.dpi} DPI</span>`,
      c.rotation ? `<span class="badge">↻ ${c.rotation}°</span>` : '',
      counts[c.id] ? `<span class="badge">${counts[c.id]}× im Projekt</span>` : '',
      ...cardWarnings(c).filter((w) => !w.text.includes('DPI')).map((w) => `<span class="badge ${w.cls}">${esc(w.text)}</span>`),
    ].join('');
    return `<div class="lib-card ${S.libSel.has(c.id) ? 'sel' : ''}" data-id="${c.id}">
      <span class="check"></span>${thumbHTML(c)}
      <div class="title" title="${esc(c.original_name)}">${esc(c.name)}</div><div class="sub">${badges}</div></div>`;
  }).join('') || '<div class="empty-state">Keine Treffer</div>';
}

function setupLibraryEvents() {
  let lastClicked = null;
  $('#lib-grid').addEventListener('click', (e) => {
    const cardEl = e.target.closest('.lib-card');
    if (!cardEl) return;
    const id = cardEl.dataset.id;
    if (e.target.closest('.check') || e.ctrlKey || e.metaKey) {
      S.libSel.has(id) ? S.libSel.delete(id) : S.libSel.add(id);
    } else if (e.shiftKey && lastClicked) {
      const ids = filteredCards().map((c) => c.id);
      const [a, b] = [ids.indexOf(lastClicked), ids.indexOf(id)].sort((x, y) => x - y);
      ids.slice(a, b + 1).forEach((x) => S.libSel.add(x));
    } else {
      return openCardDetail(id);
    }
    lastClicked = id;
    renderLibrary();
  });
  $('#lib-search2').addEventListener('input', (e) => { S.filter = e.target.value; $('#lib-search').value = S.filter; renderLibrary(); });
  $('#bulk-bleed').innerHTML = Object.entries(S.info.bleed_modes).filter(([k]) => k !== 'custom').map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join('');
}

async function bulkUpdate(patchFn) {
  const ids = [...S.libSel];
  if (!ids.length) return toast('Bitte zuerst Karten auswählen (Häkchen oder Strg+Klick)');
  for (const id of ids) {
    await api('PATCH', `/api/cards/${id}`, patchFn(S.cardMap[id]));
  }
  await loadCards();
  renderLibrary();
  toast(`${ids.length} Karte(n) aktualisiert`, 'ok');
}

// ------------------------------------------------------------------ Karten-Detail
function openCardDetail(id) {
  const c = S.cardMap[id];
  if (!c) return;
  const modes = Object.entries(S.info.bleed_modes).map(([k, v]) => `<option value="${k}" ${k === c.bleed_mode ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const body = `<div class="detail">
    <div class="preview" id="cd-prev"><img src="${c.thumb}"><div class="cut" id="cd-cut"></div></div>
    <div class="form" id="cd-form">
      <label>Name</label><input type="text" id="cd-name" value="${esc(c.name)}" autofocus>
      <label>Padding</label><select id="cd-mode">${modes}</select>
      <label>Eigene Breite</label><div class="unit"><input type="number" id="cd-mm" step="0.05" value="${c.bleed_mm ?? 3}"> mm pro Seite</div>
      <label>Drehung</label><select id="cd-rot">${[0, 90, 180, 270].map((r) => `<option value="${r}" ${r === c.rotation ? 'selected' : ''}>${r}°</option>`).join('')}</select>
      <label class="chk full"><input type="checkbox" id="cd-corners" ${c.fill_corners ? 'checked' : ''}> Transparente (runde) Ecken mit Randfarbe füllen</label>
      <label>Künstl. Bleed</label><select id="cd-fill"><option value="edge" ${c.bleed_fill !== 'mirror' ? 'selected' : ''}>Randpixel verlängern</option><option value="mirror" ${c.bleed_fill === 'mirror' ? 'selected' : ''}>Spiegeln</option></select>
      <div class="note">Gilt nur für Bilder ohne Padding.</div>
      <div class="full muted" style="font-size:12px;line-height:1.6">
        Datei: ${esc(c.original_name)}<br>${c.w} × ${c.h} px · effektiv <b>${c.dpi} DPI</b><br>
        Seitenverhältnis spricht für: <b>${c.suggested_mode === 'none' ? 'ohne Padding' : 'mit Padding (MPC)'}</b><br>
        Bleed laut Seitenverhältnis: <b>${fmt(c.detected_bleed_mm ?? 0, 2)} mm</b> pro Seite (MPC-Vorgabe 3,05 mm, 1/8″ = 3,18 mm)<br>
        Die gestrichelte Linie zeigt die spätere Schnittkante.</div>
    </div></div>`;
  modal({
    title: 'Bild-Einstellungen', body, wide: true,
    onOpen: (bg) => {
      const cut = $('#cd-cut', bg);
      const fx = c.fx || 0, fy = c.fy || 0;
      cut.style.left = `${fx * 100}%`; cut.style.right = `${fx * 100}%`;
      cut.style.top = `${fy * 100}%`; cut.style.bottom = `${fy * 100}%`;
      const toggleMm = () => { $('#cd-mm', bg).disabled = $('#cd-mode', bg).value !== 'custom'; };
      $('#cd-mode', bg).addEventListener('change', toggleMm); toggleMm();
    },
    buttons: [
      { label: 'Löschen', cls: 'danger-ghost', onClick: async () => { if (await deleteCards([id])) return true; return false; } },
      { label: 'Original öffnen', onClick: () => { window.open(`/api/cards/${id}/original`, '_blank'); return false; } },
      { label: 'Abbrechen' },
      {
        label: 'Speichern', cls: 'primary', onClick: async (bg) => {
          const patch = {
            name: $('#cd-name', bg).value.trim() || c.name, bleed_mode: $('#cd-mode', bg).value, bleed_mm: Number($('#cd-mm', bg).value),
            rotation: Number($('#cd-rot', bg).value), fill_corners: $('#cd-corners', bg).checked, bleed_fill: $('#cd-fill', bg).value,
          };
          const r = await guard(() => api('PATCH', `/api/cards/${id}`, patch));
          if (!r) return false;
          await loadCards(); refreshView();
          toast('Gespeichert', 'ok');
          return true;
        },
      },
    ],
  });
}

async function deleteCards(ids) {
  const used = ids.filter((id) => flat().includes(id)).length;
  const ok = await confirmBox('Karten löschen', `${ids.length} Karte(n) endgültig löschen?${used ? ' Sie werden auch aus allen Projekten entfernt.' : ''}`, 'Löschen', true);
  if (!ok) return false;
  const r = await guard(() => api('POST', '/api/cards/delete', { ids }));
  if (!r) return false;
  ids.forEach((id) => S.libSel.delete(id));
  await loadCards();
  if (S.project) await openProject(S.project.id, true);
  refreshView();
  toast(`${ids.length} Karte(n) gelöscht`, 'ok');
  return true;
}

// ------------------------------------------------------------------ Upload
function bleedRadioHTML(selected) {
  const hints = { mpc: 'Standard für MPC-Fill', eighth: 'manche Vorlagen', auto: 'pro Bild erkannt', custom: '', none: 'z.B. Scryfall-Bilder' };
  return `<div class="radio-list">${Object.entries(S.info.bleed_modes).map(([k, v]) => `
    <label><input type="radio" name="bleed" value="${k}" ${k === selected ? 'checked' : ''}> ${esc(v)}
    ${k === 'custom' ? '<input type="number" id="up-mm" step="0.05" style="width:80px" value="3"> mm' : ''}<small>${hints[k]}</small></label>`).join('')}</div>`;
}

function openUpload(preFiles = null) {
  const d = S.settings.upload_defaults;
  let files = preFiles ? [...preFiles] : [];
  const body = `
    <div class="dropzone" id="up-drop"><b>Dateien hierher ziehen</b> oder klicken<br><small>PNG, JPG, WEBP … oder ZIP-Archiv · auch ganze Ordner</small></div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn small" id="up-pick">Dateien wählen</button><button class="btn small" id="up-pick-dir">Ordner wählen</button>
      <span id="up-count" class="muted" style="margin-left:auto"></span></div>
    <input type="file" id="up-files" multiple accept="image/*,.zip" hidden>
    <input type="file" id="up-dir" webkitdirectory multiple hidden>
    <h4 style="margin:16px 0 4px">Haben die Bilder ein Padding (Bleed)?</h4>
    ${bleedRadioHTML(d.bleed_mode)}
    <div class="form" style="margin-top:12px">
      <label class="chk full"><input type="checkbox" id="up-corners" ${d.fill_corners ? 'checked' : ''}> Runde/transparente Ecken mit Randfarbe füllen</label>
      <label class="chk full"><input type="checkbox" id="up-rotate" ${d.auto_rotate ? 'checked' : ''}> Querformat-Bilder automatisch hochkant drehen</label>
      <label class="chk full"><input type="checkbox" id="up-place" ${S.project ? 'checked' : ''}> Direkt ins Projekt „${esc(S.project ? S.project.name : '')}“ einfügen</label>
      <label>Anzahl je Karte</label><input type="number" id="up-qty" min="1" max="99" value="1">
    </div>
    <div class="progress" hidden id="up-prog"><div></div></div>`;
  modal({
    title: 'Karten hochladen', body,
    onOpen: (bg) => {
      const count = () => { $('#up-count', bg).textContent = files.length ? `${files.length} Datei(en) bereit` : ''; };
      count();
      const add = (list) => { files.push(...[...list].filter((f) => /\.(png|jpe?g|webp|bmp|tiff?|gif|zip)$/i.test(f.name))); count(); };
      const drop = $('#up-drop', bg);
      drop.onclick = () => $('#up-files', bg).click();
      $('#up-pick', bg).onclick = () => $('#up-files', bg).click();
      $('#up-pick-dir', bg).onclick = () => $('#up-dir', bg).click();
      $('#up-files', bg).onchange = (e) => add(e.target.files);
      $('#up-mm', bg).addEventListener('focus', () => { $('input[name=bleed][value=custom]', bg).checked = true; });
      $('#up-dir', bg).onchange = (e) => add(e.target.files);
      drop.addEventListener('dragover', (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add('over'); });
      drop.addEventListener('dragleave', () => drop.classList.remove('over'));
      drop.addEventListener('drop', async (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove('over'); add(await filesFromDrop(e.dataTransfer)); });
    },
    buttons: [
      { label: 'Abbrechen' },
      {
        label: 'Hochladen', cls: 'primary', onClick: async (bg) => {
          if (!files.length) { toast('Keine Dateien ausgewählt'); return false; }
          const opts = {
            bleed_mode: $('input[name=bleed]:checked', bg).value,
            bleed_mm: $('#up-mm', bg).value,
            fill_corners: $('#up-corners', bg).checked,
            auto_rotate: $('#up-rotate', bg).checked,
            project_id: $('#up-place', bg).checked && S.project ? S.project.id : '',
            quantity: $('#up-qty', bg).value || 1,
          };
          await uploadFiles(files, opts, $('#up-prog', bg));
          return true;
        },
      },
    ],
  });
}

async function filesFromDrop(dt) {
  // Unterstützt auch gezogene Ordner (webkitGetAsEntry)
  const items = [...(dt.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!items.length) return [...dt.files];
  const out = [];
  const walk = async (entry) => {
    if (entry.isFile) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res) => reader.readEntries(res));
        for (const e of batch) await walk(e);
      } while (batch.length);
    }
  };
  for (const e of items) await walk(e);
  out.sort((a, b) => a.name.localeCompare(b.name, 'de', { numeric: true }));
  return out;
}

async function uploadFiles(files, opts, progEl) {
  await flushSave();
  const chunks = [];
  let cur = [], size = 0;
  for (const f of files) {
    if (cur.length && (cur.length >= 12 || size + f.size > 80e6)) { chunks.push(cur); cur = []; size = 0; }
    cur.push(f); size += f.size;
  }
  if (cur.length) chunks.push(cur);
  if (progEl) progEl.hidden = false;
  let added = 0, existing = 0, placed = 0;
  const errors = [];
  for (let i = 0; i < chunks.length; i++) {
    const fd = new FormData();
    chunks[i].forEach((f) => fd.append('files', f, f.webkitRelativePath || f.name));
    Object.entries(opts).forEach(([k, v]) => fd.append(k, v));
    try {
      const r = await api('POST', '/api/cards/upload', fd, true);
      added += r.added.length; existing += r.existing.length; placed += r.placed; errors.push(...r.errors);
    } catch (e) { errors.push(e.message); }
    if (progEl) $('div', progEl).style.width = `${((i + 1) / chunks.length) * 100}%`;
  }
  await loadCards();
  if (S.project) await openProject(S.project.id, true);
  refreshView();
  toast(`${added} neu, ${existing} bereits vorhanden${placed ? `, ${placed} ins Projekt eingefügt` : ''}`, errors.length ? '' : 'ok', 6000);
  if (errors.length) toast(`Fehler: ${errors.slice(0, 3).join(' | ')}${errors.length > 3 ? ' …' : ''}`, 'bad', 10000);
}

// ------------------------------------------------------------------ MPC-Fill-Import
function openMpcImport() {
  modal({
    title: 'MPC-Fill-Bestellung importieren',
    body: `<p class="hint">Die XML-Datei aus MPC-Fill enthält Reihenfolge und Anzahl jeder Karte. Bilder werden über ihre Drive-ID im Dateinamen oder über den Namen zugeordnet – lade die Bilder also am besten vorher hoch.</p>
      <div class="form">
        <label>XML-Datei</label><input type="file" id="mx-file" accept=".xml,text/xml">
        <label class="chk full"><input type="checkbox" id="mx-replace" checked> Projektinhalt ersetzen (sonst anhängen)</label>
        <label class="chk full"><input type="checkbox" id="mx-dl"> Fehlende Bilder von Google Drive laden (benötigt Internet)</label>
        <label class="chk full"><input type="checkbox" id="mx-backs"> Individuelle Rückseiten (doppelseitige Karten) als eigene Karten anhängen</label>
      </div>`,
    buttons: [{ label: 'Abbrechen' }, {
      label: 'Importieren', cls: 'primary', onClick: async (bg) => {
        const f = $('#mx-file', bg).files[0];
        if (!f) { toast('Bitte XML-Datei wählen'); return false; }
        await flushSave();
        const fd = new FormData();
        fd.append('file', f);
        fd.append('replace', $('#mx-replace', bg).checked);
        fd.append('download_missing', $('#mx-dl', bg).checked);
        fd.append('include_backs', $('#mx-backs', bg).checked);
        const r = await guard(() => api('POST', `/api/projects/${S.project.id}/import-mpcfill`, fd, true));
        if (!r) return false;
        await loadCards();
        setProject(r.project);
        S.history = []; S.future = [];
        renderEditor();
        toast(`${r.placed} Karten platziert${r.downloaded.length ? `, ${r.downloaded.length} heruntergeladen` : ''}`, 'ok');
        if (r.missing.length) {
          modal({ title: 'Nicht gefundene Karten', body: `<p>Diese Karten aus der XML konnten keinem Bild zugeordnet werden:</p><ul>${r.missing.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>`, buttons: [{ label: 'OK', cls: 'primary' }] });
        }
        return true;
      },
    }],
  });
}

// ------------------------------------------------------------------ PDF & Druck
function profileOptions(selected) {
  const profs = S.calib ? S.calib.profiles : [];
  return profs.map((p) => `<option value="${p.id}" ${p.id === selected ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
}

async function exportPdf(download) {
  await flushSave();
  const r = await guard(() => api('POST', `/api/projects/${S.project.id}/pdf`, { calibrated: S.settings.print.use_calibration }));
  if (!r) return;
  if (r.missing.length) toast(`${r.missing.length} Bild(er) fehlen`, 'bad');
  if (download) {
    const a = document.createElement('a');
    a.href = `${r.url}?download=1`; a.download = r.file; document.body.appendChild(a); a.click(); a.remove();
  } else window.open(r.url, '_blank');
  toast(`PDF erstellt: ${r.pages} Seite(n), ${r.cards} Karten${S.settings.print.use_calibration ? ' (kalibriert)' : ''}`, 'ok');
}

async function openPrintDialog() {
  await flushSave();
  await loadCalibration();
  const pr = await guard(() => api('GET', '/api/printer'));
  if (!pr) return;
  S.printer = pr;
  const curPage = (S.sel ? S.sel.p : 0) + 1;
  const ready = pr.configured && pr.queue_ready;
  let warn = '';
  if (!pr.configured) warn = '<div class="alert bad">Kein Drucker konfiguriert. Trage <code>PRINTER_IP</code> in die <code>.env</code> ein und starte den Container neu. Du kannst das PDF trotzdem erzeugen.</div>';
  else if (!pr.queue_ready) warn = `<div class="alert bad">Drucker-Queue nicht bereit: ${esc(pr.error || 'wird eingerichtet…')}</div>`;
  const optSel = (key) => {
    const o = pr.options.find((x) => x.key === key);
    if (!o) return '';
    return `<label>${esc(o.label)}</label><select data-opt="${key}">${o.choices.map((c) => `<option value="${esc(c.value)}" ${pr.effective[key] === c.value ? 'selected' : ''}>${esc(c.label)}${pr.recommended[key] === c.value ? ' ★' : ''}</option>`).join('')}</select>`;
  };
  const profActive = S.calib ? S.calib.active : 'default';
  const body = `${warn}
    <div class="form">
      <label>Seiten</label>
      <div class="radio-list" style="margin:0">
        <label><input type="radio" name="pr-pages" value="" checked> Alle (${S.project.pages.length})</label>
        <label><input type="radio" name="pr-pages" value="${curPage}"> Aktuelle Seite (${curPage})</label>
        <label><input type="radio" name="pr-pages" value="custom"> Auswahl <input type="text" id="pr-custom" placeholder="z.B. 1-3,5" style="width:120px"></label>
      </div>
      <label>Kopien</label><input type="number" id="pr-copies" min="1" max="99" value="1">
      <label>Kalibrierung</label><select id="pr-profile">${profileOptions(profActive)}</select>
      ${optSel('MediaType')}${optSel('cupsPrintQuality')}${optSel('InputSlot')}${optSel('ColorModel')}${optSel('PageSize')}
      <div class="note">★ = empfohlen (Glossy II, hohe Qualität, A4 <b>nicht randlos</b> – randlos würde vergrößern!). Gedruckt wird immer 1:1 ohne Skalierung.</div>
      <label class="chk full"><input type="checkbox" id="pr-remember"> Papier-/Qualitätsoptionen als Standard merken</label>
    </div>`;
  modal({
    title: `Drucken: ${S.project.name}`, body,
    buttons: [
      { label: 'Abbrechen' },
      { label: 'Nur PDF erzeugen', onClick: async (bg) => { const p = collect(bg); const r = await guard(() => api('POST', `/api/projects/${S.project.id}/pdf`, p)); if (r) window.open(r.url, '_blank'); return !!r; } },
      {
        label: '🖨 Drucken', cls: 'primary', onClick: async (bg) => {
          if (!ready) { toast('Drucker nicht bereit', 'bad'); return false; }
          const p = collect(bg);
          if ($('#pr-remember', bg).checked) await api('POST', '/api/printer/options', { options: p.options });
          const r = await guard(() => api('POST', `/api/projects/${S.project.id}/print`, p));
          if (!r) return false;
          toast(`Druckauftrag ${r.job} gesendet (${r.pages} Seite(n), ${r.cards} Karten)`, 'ok', 7000);
          refreshPrinterPill();
          return true;
        },
      },
    ],
  });
  function collect(bg) {
    let pages = $('input[name=pr-pages]:checked', bg).value;
    if (pages === 'custom') pages = $('#pr-custom', bg).value;
    const options = {};
    $$('[data-opt]', bg).forEach((s) => { options[s.dataset.opt] = s.value; });
    const prof = $('#pr-profile', bg).value;
    return { pages, copies: Number($('#pr-copies', bg).value) || 1, profile_id: prof, calibrated: true, options };
  }
}

// ------------------------------------------------------------------ Drucker-Ansicht
async function renderPrinter() {
  const pr = await guard(() => api('GET', '/api/printer'));
  if (!pr) return;
  S.printer = pr;
  updatePrinterPill(pr);
  const a = pr.attributes;
  let alerts = '';
  if (!pr.configured) alerts += `<div class="alert bad">Kein Drucker konfiguriert. In der Datei <code>.env</code> neben der <code>docker-compose.yml</code> z.B. <code>PRINTER_IP=192.168.178.50</code> setzen und <code>docker compose up -d</code> ausführen.</div>`;
  else if (!pr.cups) alerts += '<div class="alert warn">CUPS ist nicht verfügbar (App läuft außerhalb des Containers?). Drucken ist deaktiviert, PDFs funktionieren.</div>';
  if (pr.error) alerts += `<div class="alert bad">${esc(pr.error)}</div>`;
  if (pr.attributes_error) alerts += `<div class="alert warn">${esc(pr.attributes_error)}</div>`;
  if (a && a.state_reasons.length) alerts += `<div class="alert warn">Drucker meldet: ${a.state_reasons.map(esc).join(', ')}${a.state_message ? ` – ${esc(a.state_message)}` : ''}</div>`;
  const stateTxt = { idle: 'Bereit', processing: 'Druckt', stopped: 'Gestoppt', unknown: 'Unbekannt' };
  $('#printer-status').innerHTML = `<h2>Drucker</h2>${alerts}
    <div class="kv">
      <div>Modell</div><div>${esc(a ? a.make_and_model : '–')}</div>
      <div>Adresse</div><div><code>${esc(pr.uri || '–')}</code></div>
      <div>Zustand</div><div>${a ? stateTxt[a.state] || a.state : '–'}</div>
      <div>CUPS-Queue</div><div>${pr.queue_ready ? `<span class="badge ok">bereit</span> <code>${esc(pr.queue)}</code>` : pr.setting_up ? 'wird eingerichtet…' : '<span class="badge bad">nicht bereit</span>'}</div>
      <div>Papier eingelegt</div><div>${a && a.media_ready.length ? esc(a.media_ready.join(', ')) : '–'}</div>
      <div>Formate</div><div class="muted" style="font-size:12px">${a ? esc(a.formats.join(', ')) : '–'}</div>
      ${a && a.firmware ? `<div>Firmware</div><div>${esc(a.firmware)}</div>` : ''}
    </div>
    <div class="btn-row">
      <button class="btn" data-action="printer-refresh">Aktualisieren</button>
      <button class="btn" data-action="printer-setup" ${pr.configured ? '' : 'disabled'}>Queue neu einrichten</button>
    </div>`;
  const markers = a ? a.markers.filter((m) => m.type !== 'waste-ink' || true) : [];
  $('#printer-ink').innerHTML = `<h2>Tinte</h2>${markers.length ? markers.map((m) => {
    const lvl = m.level < 0 ? null : m.level;
    const col = (m.color || '#888').split('#').filter(Boolean)[0];
    return `<div class="ink"><span>${esc(m.name)}</span><div class="bar"><div style="width:${lvl ?? 0}%;background:#${esc(col)}"></div></div><span>${lvl == null ? '?' : `${lvl}%`}</span></div>`;
  }).join('') : '<div class="muted">Keine Daten (Drucker nicht erreichbar?)</div>'}
    <div class="hint">Bei MegaTank-Druckern schätzt der Drucker den Füllstand – nachfüllen nach Sichtprüfung.</div>`;

  if (!pr.options.length) {
    $('#printer-options').innerHTML = '<h2>Druckoptionen</h2><div class="muted">Verfügbar, sobald die Queue eingerichtet ist.</div>';
  } else {
    $('#printer-options').innerHTML = `<h2>Druckoptionen (Standard)</h2>
      <div class="form">${pr.options.map((o) => `<label>${esc(o.label)}</label><select data-opt="${o.key}">
        ${o.choices.map((c) => `<option value="${esc(c.value)}" ${pr.effective[o.key] === c.value || (!(o.key in pr.effective) && o.default === c.value) ? 'selected' : ''}>${esc(c.label)}${pr.recommended[o.key] === c.value ? ' ★' : ''}</option>`).join('')}</select>`).join('')}
        <div class="note">★ = automatisch empfohlen. Papiertyp passend zu „Photo Paper Plus Glossy II“, Qualität „Hoch“. Skalierung ist immer aus (1:1).</div>
        <label class="chk full"><input type="checkbox" id="use-calib" ${S.settings.print.use_calibration ? 'checked' : ''}> PDF-Downloads ebenfalls mit Kalibrierung erzeugen</label>
      </div>
      <div class="btn-row"><button class="btn" data-action="printer-options-reset">Auf Empfehlung zurücksetzen</button><button class="btn primary" data-action="printer-options-save">Speichern</button></div>
      <div class="hint" style="margin-top:10px">Vom Drucker gemeldete Papiertypen (IPP): ${a ? esc(a.media_types.join(', ')) : '–'}</div>`;
  }
  renderJobs();
}

async function renderJobs() {
  const jobs = await api('GET', '/api/printer/jobs').catch(() => []);
  S.jobs = jobs;
  const badge = (j) => {
    if (j.active) return `<span class="badge warn">${esc(j.state || 'aktiv')}</span>`;
    if (j.ok) return '<span class="badge ok">fertig</span>';
    return `<span class="badge bad">${esc(j.state || '?')}</span>`;
  };
  const reasons = (j) => (j.reasons || []).filter((r) => !['none', 'job-completed-successfully', 'processing-to-stop-point', 'job-printing'].includes(r));
  $('#printer-jobs').innerHTML = `<h2>Druckaufträge</h2>
    ${jobs.length ? `<table class="jobs"><tr><th>Job</th><th>Name</th><th>Status</th><th></th></tr>
      ${jobs.map((j) => `<tr><td>${esc(j.id)}</td><td>${esc(j.name || '')}<div class="muted" style="font-size:11px">${esc(j.time)}</div></td>
      <td>${badge(j)} <span class="muted" style="font-size:11px">${esc([...reasons(j), j.printer_message].filter(Boolean).join(' · '))}</span></td>
      <td style="white-space:nowrap"><button class="btn small" data-job-detail="${esc(j.id)}">Details</button>
      ${j.active ? `<button class="btn small danger-ghost" data-cancel="${esc(j.id)}">Abbrechen</button>` : ''}</td></tr>`).join('')}</table>
      <div style="margin-top:8px"><button class="btn small danger-ghost" data-action="cancel-all">Alle aktiven abbrechen</button></div>` : '<div class="muted">Keine Aufträge</div>'}
    <div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
      <b>Fehlersuche</b><div class="hint">Wird eine Seite nur teilweise oder gar nicht gedruckt: Diagnose öffnen, kopieren und mitschicken.</div>
      <button class="btn small" data-action="printer-diagnostics">Diagnose-Bericht anzeigen</button></div>`;
}

function showJobDetail(id) {
  const j = (S.jobs || []).find((x) => x.id === id);
  if (!j) return;
  const rows = [
    ['Status', `${j.state} (${j.state_code ?? '?'})`], ['Gründe', (j.reasons || []).join(', ')],
    ['Meldung Drucker', j.printer_message], ['Drucker-Gründe', (j.printer_reasons || []).join(', ')],
    ['Seiten gedruckt', j.impressions ?? '–'], ['Blätter', j.sheets ?? '–'], ['Größe', `${j.k_octets ?? '?'} kB`],
    ['Format', j.format], ['Zeit', j.time],
  ];
  modal({ title: `Auftrag ${j.id}`, body: `<div class="kv">${rows.map(([k, v]) => `<div>${k}</div><div>${esc(v || '–')}</div>`).join('')}</div>`, buttons: [{ label: 'OK', cls: 'primary' }] });
}

async function showDiagnostics() {
  const d = await guard(() => api('GET', '/api/printer/diagnostics'));
  if (!d) return;
  const text = [
    '== Status ==', JSON.stringify(d.status, null, 1),
    '== Drucker (IPP) ==', d.attributes_error || JSON.stringify(d.attributes, null, 1),
    '== Queue ==', d.queue_state, '== Optionen ==', JSON.stringify(d.effective_options),
    '== Jobs ==', JSON.stringify(d.jobs, null, 1), '== CUPS-Log ==', d.cups_log,
  ].join('\n');
  modal({
    title: 'Diagnose-Bericht', wide: true,
    body: `<textarea id="diag-text" readonly style="width:100%;height:60vh;font-family:ui-monospace,monospace;font-size:11.5px">${esc(text)}</textarea>`,
    buttons: [{ label: 'Kopieren', onClick: async (bg) => { const t = $('#diag-text', bg); t.select(); try { await navigator.clipboard.writeText(t.value); } catch (_) { document.execCommand('copy'); } toast('Kopiert', 'ok'); return false; } },
      { label: 'Schließen', cls: 'primary' }],
  });
}

function updatePrinterPill(pr) {
  const el = $('#pill-printer');
  if (!pr.configured) { el.className = 'pill warn'; el.textContent = 'Kein Drucker'; return; }
  if (!pr.queue_ready) { el.className = 'pill bad'; el.textContent = 'Drucker: nicht bereit'; return; }
  const a = pr.attributes;
  if (!a) { el.className = 'pill bad'; el.textContent = 'Drucker offline'; return; }
  const low = a.markers.some((m) => m.level >= 0 && m.level < 15);
  el.className = `pill ${a.state === 'stopped' || a.state_reasons.some((r) => r.endsWith('-error')) ? 'bad' : low ? 'warn' : 'ok'}`;
  el.textContent = `${a.make_and_model ? a.make_and_model.replace(/^Canon\s*/i, '') : 'Drucker'}: ${{ idle: 'bereit', processing: 'druckt', stopped: 'gestoppt' }[a.state] || a.state}${low ? ' · Tinte niedrig' : ''}`;
}

async function refreshPrinterPill() {
  try { const pr = await api('GET', '/api/printer'); S.printer = pr; updatePrinterPill(pr); } catch (_) { /* ignore */ }
}

// ------------------------------------------------------------------ Kalibrierung
async function loadCalibration() {
  S.calib = await api('GET', '/api/calibration');
  const act = S.calib.profiles.find((p) => p.id === S.calib.active);
  const pill = $('#pill-calib');
  pill.textContent = `Kalibrierung: ${act ? act.name : '–'}`;
  pill.className = `pill ${act && act.id !== 'default' ? 'ok' : 'warn'}`;
}

const CALIB_SVG = `<svg class="diagram" viewBox="0 0 210 160" xmlns="http://www.w3.org/2000/svg" font-family="system-ui" font-size="9">
  <rect x="5" y="5" width="200" height="150" fill="#fff" stroke="#333" stroke-width="1.2"/>
  <line x1="30" y1="10" x2="30" y2="150" stroke="#000"/><line x1="180" y1="10" x2="180" y2="150" stroke="#000"/>
  <line x1="10" y1="30" x2="200" y2="30" stroke="#000"/>
  <text x="33" y="60" font-weight="700">A</text><text x="183" y="60" font-weight="700">B</text><text x="100" y="27" font-weight="700">C</text>
  <line x1="5" y1="95" x2="30" y2="95" stroke="#6d4aff" stroke-width="1.6" marker-end="url(#ar)" marker-start="url(#ar2)"/>
  <text x="9" y="91" fill="#6d4aff">a</text>
  <line x1="5" y1="120" x2="180" y2="120" stroke="#e6005a" stroke-width="1.6" marker-end="url(#ar)" marker-start="url(#ar2)"/>
  <text x="95" y="116" fill="#e6005a">b</text>
  <line x1="140" y1="5" x2="140" y2="30" stroke="#18a058" stroke-width="1.6" marker-end="url(#ar)" marker-start="url(#ar2)"/>
  <text x="144" y="21" fill="#18a058">c</text>
  <defs><marker id="ar" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6z" fill="#555"/></marker>
  <marker id="ar2" markerWidth="6" markerHeight="6" refX="1" refY="3" orient="auto"><path d="M6,0 L0,3 L6,6z" fill="#555"/></marker></defs>
  <text x="105" y="148" text-anchor="middle" fill="#555">Papierkante → Mitte der Linie</text></svg>
  <div class="hint" style="text-align:center">d wird wie c gemessen – von der oberen Kante bis zur unteren Linie D.</div>`;

let calibSelected = null;
async function renderCalibration() {
  await loadCalibration();
  const c = S.calib;
  if (!calibSelected || !c.profiles.find((p) => p.id === calibSelected)) calibSelected = c.active;
  const p = c.profiles.find((x) => x.id === calibSelected);
  const m = p.measurements || {};
  const ref = c.ref;
  $('#calibration').innerHTML = `
    <div class="card-box">
      <h2>Kalibrierprofil</h2>
      <div class="profile-row">
        <select id="cal-profile">${c.profiles.map((x) => `<option value="${x.id}" ${x.id === p.id ? 'selected' : ''}>${esc(x.name)}${x.id === c.active ? ' (aktiv)' : ''}</option>`).join('')}</select>
        <button class="btn" data-action="cal-activate" ${p.id === c.active ? 'disabled' : ''}>Als aktiv setzen</button>
        <button class="btn" data-action="cal-new">Neues Profil</button>
        <button class="btn" data-action="cal-rename" ${p.id === 'default' ? 'disabled' : ''}>Umbenennen</button>
        <button class="btn danger-ghost" data-action="cal-delete" ${p.id === 'default' ? 'disabled' : ''}>Löschen</button>
      </div>
      <ul class="desc">${p.description.map((d) => `<li>${esc(d)}</li>`).join('')}</ul>
      ${p.id === 'default' ? '<div class="alert info">Das Standardprofil ist unkalibriert. Lege mit „Neues Profil“ z.B. ein Profil „Glossy II“ an – unterschiedliche Papiere können sich unterschiedlich verhalten.</div>' : ''}
    </div>
    <div class="steps">
      <div class="card-box">
        <h2><span class="step-n">1</span>Kalibrierseite drucken</h2>
        <p class="hint">Gleiches Papier wie für die Karten verwenden (Glossy II). Die Seite wird mit dem gewählten Profil gedruckt – so verfeinerst du die Kalibrierung schrittweise.</p>
        <label class="chk"><input type="checkbox" id="cal-uncal"> Ohne Korrektur drucken (Neustart der Kalibrierung)</label>
        <div class="btn-row" style="margin-top:10px">
          <button class="btn primary" data-action="cal-print">🖨 Drucken</button>
          <button class="btn" data-action="cal-pdf">PDF ansehen</button>
        </div>
      </div>
      <div class="card-box">
        <h2><span class="step-n">2</span>Messen &amp; eintragen</h2>
        ${CALIB_SVG}
        <div class="meas"><b>a</b><span>Linke Kante → Linie A</span><input type="number" step="0.05" id="m-a" placeholder="≈ ${ref.A}" value="${m.a ?? ''}"></div>
        <div class="meas"><b>b</b><span>Linke Kante → Linie B</span><input type="number" step="0.05" id="m-b" placeholder="≈ ${ref.B}" value="${m.b ?? ''}"></div>
        <div class="meas"><b>c</b><span>Obere Kante → Linie C</span><input type="number" step="0.05" id="m-c" placeholder="≈ ${ref.C}" value="${m.c ?? ''}"></div>
        <div class="meas"><b>d</b><span>Obere Kante → Linie D</span><input type="number" step="0.05" id="m-d" placeholder="≈ ${ref.D}" value="${m.d ?? ''}"></div>
        <div id="cal-result"></div>
        <div class="btn-row"><button class="btn" data-action="cal-compute">Berechnen</button><button class="btn primary" data-action="cal-apply" disabled id="cal-apply">In Profil übernehmen</button></div>
      </div>
    </div>
    <div class="steps">
      <div class="card-box">
        <h2><span class="step-n">3</span>Feinjustieren (optional)</h2>
        <p class="hint">Positive Werte = der Drucker setzt zu weit nach rechts / unten bzw. druckt zu groß. Die App gleicht genau das aus.</p>
        ${nudgeRow('offset_x', 'Versatz X', p.offset_x, 'mm', 0.1)}
        ${nudgeRow('offset_y', 'Versatz Y', p.offset_y, 'mm', 0.1)}
        ${nudgeRow('scale_x', 'Maßstab X', (p.scale_x * 100).toFixed(3), '%', 0.05)}
        ${nudgeRow('scale_y', 'Maßstab Y', (p.scale_y * 100).toFixed(3), '%', 0.05)}
        <div class="btn-row" style="margin-top:10px"><button class="btn" data-action="cal-reset" ${p.id === 'default' ? 'disabled' : ''}>Auf 0 zurücksetzen</button><button class="btn primary" data-action="cal-save-manual" ${p.id === 'default' ? 'disabled' : ''}>Speichern</button></div>
        ${p.id === 'default' ? '<div class="hint">Das Standardprofil bleibt unverändert – lege ein eigenes Profil an.</div>' : ''}
      </div>
      <div class="card-box">
        <h2><span class="step-n">4</span>Probeschnitt prüfen</h2>
        <p class="hint">Druckt das aktuelle Raster als farbige Felder mit Schnittmarken. Schneide entlang der Marken (8 Schnitte) und prüfe:</p>
        <ul class="desc">
          <li>Jede Karte misst exakt Soll-Breite × Soll-Höhe (mit einer echten Karte vergleichen).</li>
          <li>Der Innenrahmen ist überall 3 mm vom Rand entfernt.</li>
          <li>Kein Streifen der Nachbarfarbe am Rand.</li>
        </ul>
        <div class="btn-row"><button class="btn primary" data-action="cut-print">🖨 Drucken</button><button class="btn" data-action="cut-pdf">PDF ansehen</button></div>
        <p class="hint" style="margin-top:12px">Tipp: Bleibt nach dem Kalibrieren ein konstanter Fehler auf <i>einer</i> Seite, hilft der Raster-Versatz im Layout. Ist die Karte insgesamt zu groß/klein, die Kartengröße im Layout (z.B. 63,0 × 88,0) prüfen.</p>
      </div>
    </div>`;
}

function nudgeRow(key, label, value, unit, step) {
  return `<div class="nudge"><span>${label}</span><button class="btn small" data-nudge="${key}" data-d="-${step}">−</button>
    <input type="number" step="${step / 10}" id="n-${key}" value="${value}"><button class="btn small" data-nudge="${key}" data-d="${step}">+</button><span>${unit}</span></div>`;
}

let calibComputed = null;
function setupCalibrationEvents() {
  const root = $('#calibration');
  root.addEventListener('change', (e) => { if (e.target.id === 'cal-profile') { calibSelected = e.target.value; renderCalibration(); } });
  root.addEventListener('click', async (e) => {
    const n = e.target.closest('[data-nudge]');
    if (n) {
      const inp = $(`#n-${n.dataset.nudge}`);
      inp.value = (Number(inp.value) + Number(n.dataset.d)).toFixed(n.dataset.nudge.startsWith('scale') ? 3 : 2);
    }
  });
}

async function calibAction(act) {
  const pid = calibSelected;
  const uncal = $('#cal-uncal') && $('#cal-uncal').checked;
  if (act === 'cal-activate') { await guard(() => api('POST', '/api/calibration/active', { id: pid }), 'Profil aktiviert'); renderCalibration(); }
  if (act === 'cal-new') {
    const name = await prompt('Neues Kalibrierprofil', 'Name', 'Glossy II');
    if (!name) return;
    const p = await guard(() => api('POST', '/api/calibration/profiles', { name, copy_from: pid !== 'default' ? pid : null }));
    if (p) { calibSelected = p.id; renderCalibration(); toast('Profil angelegt und aktiviert', 'ok'); }
  }
  if (act === 'cal-rename') {
    const cur = S.calib.profiles.find((x) => x.id === pid);
    const name = await prompt('Profil umbenennen', 'Name', cur.name);
    if (name) { await guard(() => api('PUT', `/api/calibration/profiles/${pid}`, { name })); renderCalibration(); }
  }
  if (act === 'cal-delete') {
    if (await confirmBox('Profil löschen', 'Kalibrierprofil wirklich löschen?', 'Löschen', true)) {
      await guard(() => api('DELETE', `/api/calibration/profiles/${pid}`)); calibSelected = null; renderCalibration();
    }
  }
  if (act === 'cal-print' || act === 'cal-pdf') {
    const r = await guard(() => api('POST', '/api/calibration/page', { action: act === 'cal-print' ? 'print' : 'pdf', profile_id: pid, uncalibrated: uncal }));
    if (r) { if (r.job) toast(`Kalibrierseite gesendet (${r.job})`, 'ok'); else window.open(r.url, '_blank'); }
  }
  if (act === 'cut-print' || act === 'cut-pdf') {
    const r = await guard(() => api('POST', '/api/calibration/cut-test', { action: act === 'cut-print' ? 'print' : 'pdf', profile_id: pid, project_id: S.project ? S.project.id : null }));
    if (r) { if (r.job) toast(`Probeschnitt gesendet (${r.job})`, 'ok'); else window.open(r.url, '_blank'); }
  }
  if (act === 'cal-compute') {
    const v = ['a', 'b', 'c', 'd'].map((k) => $(`#m-${k}`).value);
    if (v.some((x) => x === '')) return toast('Bitte alle vier Messwerte eintragen');
    const [a, b, c, d] = v.map(Number);
    const printedWith = !uncal;
    const r = await guard(() => api('POST', '/api/calibration/compute', { a, b, c, d, profile_id: pid, printed_with_profile: printedWith }));
    if (!r) return;
    calibComputed = { ...r.result, measurements: { a, b, c, d } };
    const meas = r.measured;
    const resid = Math.abs(meas.offset_x) < 0.15 && Math.abs(meas.offset_y) < 0.15 && Math.abs(meas.scale_x - 1) < 0.001 && Math.abs(meas.scale_y - 1) < 0.001;
    $('#cal-result').innerHTML = `<div class="alert ${resid ? 'ok' : 'info'}">
      ${resid ? '✓ Die Seite ist bereits sehr genau. ' : ''}Neue Gesamtkorrektur:<ul class="desc">${r.description.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <small>Versatz X ${fmt(r.result.offset_x, 2)} mm, Y ${fmt(r.result.offset_y, 2)} mm · Maßstab X ${fmt(r.result.scale_x * 100, 3)} %, Y ${fmt(r.result.scale_y * 100, 3)} %</small></div>`;
    $('#cal-apply').disabled = false;
    if (pid === 'default') $('#cal-result').insertAdjacentHTML('beforeend', '<div class="alert warn">Beim Übernehmen wird automatisch ein neues Profil angelegt.</div>');
  }
  if (act === 'cal-apply' && calibComputed) {
    let target = pid;
    if (pid === 'default') {
      const p = await guard(() => api('POST', '/api/calibration/profiles', { name: 'Glossy II' }));
      if (!p) return;
      target = p.id;
    }
    const r = await guard(() => api('PUT', `/api/calibration/profiles/${target}`, calibComputed));
    if (r) { calibSelected = target; calibComputed = null; await renderCalibration(); toast('Kalibrierung gespeichert. Jetzt den Probeschnitt drucken!', 'ok', 6000); }
  }
  if (act === 'cal-save-manual' || act === 'cal-reset') {
    const reset = act === 'cal-reset';
    const patch = reset ? { offset_x: 0, offset_y: 0, scale_x: 1, scale_y: 1, measurements: null } : {
      offset_x: Number($('#n-offset_x').value), offset_y: Number($('#n-offset_y').value),
      scale_x: Number($('#n-scale_x').value) / 100, scale_y: Number($('#n-scale_y').value) / 100,
    };
    const r = await guard(() => api('PUT', `/api/calibration/profiles/${pid}`, patch), 'Gespeichert');
    if (r) renderCalibration();
  }
}

// ------------------------------------------------------------------ Einstellungen
function renderSettings() {
  $('#settings-layout').innerHTML = layoutFormHTML(S.settings.default_layout, 'sl');
  const d = S.settings.upload_defaults;
  $('#settings-upload').innerHTML = `<div class="form">
    <label>Padding</label><select id="su-mode">${Object.entries(S.info.bleed_modes).map(([k, v]) => `<option value="${k}" ${k === d.bleed_mode ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>
    <label class="chk full"><input type="checkbox" id="su-corners" ${d.fill_corners ? 'checked' : ''}> Transparente Ecken füllen</label>
    <label class="chk full"><input type="checkbox" id="su-rotate" ${d.auto_rotate ? 'checked' : ''}> Querformat automatisch drehen</label></div>`;
  $('#settings-info').innerHTML = `<h2>System</h2><div class="kv">
    <div>Version</div><div>${esc(S.info.version)}</div>
    <div>Drucker-URI</div><div><code>${esc(S.info.printer_uri || 'nicht gesetzt')}</code></div>
    <div>MPC-Bleed</div><div>0,12" (3,05 mm) je Seite – Vorlage 2,74" × 3,74"</div>
    <div>Kartenmaß</div><div>${S.info.card_default.join(' × ')} mm</div></div>
    <div class="hint">Drucker-IP, Queue-Name, Papiertyp und Qualität werden in der <code>.env</code> festgelegt.</div>`;
}

// ------------------------------------------------------------------ Navigation & globale Aktionen
function showView(v) {
  S.view = v;
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach((el) => el.classList.toggle('active', el.id === `view-${v}`));
  try { localStorage.setItem('view', v); } catch (_) {}
  refreshView();
}

function refreshView() {
  if (S.view === 'editor') renderEditor();
  if (S.view === 'library') renderLibrary();
  if (S.view === 'printer') renderPrinter();
  if (S.view === 'calibration') renderCalibration();
  if (S.view === 'settings') renderSettings();
}

const ACTIONS = {
  'open-upload': () => openUpload(),
  'project-new': async () => {
    const name = await prompt('Neues Projekt', 'Name', `Proxies ${new Date().toLocaleDateString('de-DE')}`);
    if (!name) return;
    await flushSave();
    const p = await guard(() => api('POST', '/api/projects', { name }));
    if (p) { await loadProjects(); setProject(p); S.sel = { p: 0, s: 0 }; S.history = []; renderEditor(); }
  },
  'project-rename': async () => {
    const name = await prompt('Projekt umbenennen', 'Name', S.project.name);
    if (!name) return;
    const p = await guard(() => api('PUT', `/api/projects/${S.project.id}`, { name }));
    if (p) { S.project.name = p.name; await loadProjects(); renderEditor(); }
  },
  'project-duplicate': async () => {
    await flushSave();
    const p = await guard(() => api('POST', '/api/projects', { copy_from: S.project.id }), 'Projekt dupliziert');
    if (p) { await loadProjects(); setProject(p); S.history = []; renderEditor(); }
  },
  'project-delete': async () => {
    if (!await confirmBox('Projekt löschen', `„${esc(S.project.name)}“ wirklich löschen? Die Karten bleiben in der Bibliothek.`, 'Löschen', true)) return;
    clearTimeout(saveTimer); saveTimer = null;
    await guard(() => api('DELETE', `/api/projects/${S.project.id}`));
    await loadProjects();
    await openProject(S.projects[0].id);
  },
  undo, redo,
  'add-page': () => mutate((pg) => { pg.push(Array(S.geo.slots.length).fill(null)); }),
  compact: () => mutate(() => { S.project.pages = fromFlat(flat().filter(Boolean)); }),
  'remove-empty-pages': () => mutate((pg) => { S.project.pages = pg.filter((p) => p.some(Boolean)); }),
  'sort-name': () => mutate(() => {
    const ids = flat().filter(Boolean);
    ids.sort((a, b) => (S.cardMap[a]?.name || '').localeCompare(S.cardMap[b]?.name || '', 'de'));
    S.project.pages = fromFlat(ids);
  }),
  'clear-all': async () => { if (await confirmBox('Alles leeren', 'Alle Slots dieses Projekts leeren? (Strg+Z macht es rückgängig)', 'Leeren', true)) mutate(() => { S.project.pages = []; }); },
  'import-mpcfill': openMpcImport,
  'layout-as-default': async () => {
    S.settings = await guard(() => api('PATCH', '/api/settings', { default_layout: S.project.layout }), 'Als Standard gespeichert') || S.settings;
  },
  'print-dialog': openPrintDialog,
  'pdf-preview': () => exportPdf(false),
  'pdf-download': () => exportPdf(true),
  'lib-select-all': () => { filteredCards().forEach((c) => S.libSel.add(c.id)); renderLibrary(); },
  'lib-select-none': () => { S.libSel.clear(); renderLibrary(); },
  'lib-select-warn': () => { S.libSel = new Set(S.cards.filter((c) => cardWarnings(c).length).map((c) => c.id)); renderLibrary(); },
  'lib-bulk-bleed': () => bulkUpdate(() => ({ bleed_mode: $('#bulk-bleed').value })),
  'lib-bulk-rotate': () => bulkUpdate((c) => ({ rotation: ((c.rotation || 0) + 90) % 360 })),
  'lib-delete': () => { if (S.libSel.size) deleteCards([...S.libSel]); else toast('Keine Karten ausgewählt'); },
  'lib-add-project': async () => {
    if (!S.libSel.size) return toast('Keine Karten ausgewählt');
    const n = await prompt('Ins Projekt einfügen', `Anzahl je Karte (Projekt „${S.project.name}“)`, '1');
    const q = parseInt(n, 10);
    if (!q || q < 1) return;
    const ids = filteredCards().filter((c) => S.libSel.has(c.id)).flatMap((c) => Array(q).fill(c.id));
    appendCards(ids);
    toast(`${ids.length} Karten eingefügt`, 'ok');
  },
  'printer-refresh': renderPrinter,
  'printer-setup': async () => { toast('Queue wird eingerichtet…'); await guard(() => api('POST', '/api/printer/setup')); renderPrinter(); },
  'printer-options-save': async () => {
    const options = {};
    $$('#printer-options [data-opt]').forEach((s) => { options[s.dataset.opt] = s.value; });
    await guard(() => api('POST', '/api/printer/options', { options }));
    S.settings = await api('PATCH', '/api/settings', { print: { use_calibration: $('#use-calib').checked } });
    toast('Druckoptionen gespeichert', 'ok');
    renderPrinter();
  },
  'printer-options-reset': async () => { await guard(() => api('POST', '/api/printer/options', { options: {} }), 'Zurückgesetzt'); renderPrinter(); },
  'printer-diagnostics': showDiagnostics,
  'cancel-all': async () => { await guard(() => api('POST', '/api/printer/jobs/cancel-all'), 'Abgebrochen'); renderJobs(); },
  'settings-save-layout': async () => {
    const r = await guard(() => api('PATCH', '/api/settings', { default_layout: readLayoutForm($('#settings-layout')) }), 'Gespeichert');
    if (r) { S.settings = r; renderSettings(); }
  },
  'settings-save-upload': async () => {
    const r = await guard(() => api('PATCH', '/api/settings', { upload_defaults: { bleed_mode: $('#su-mode').value, fill_corners: $('#su-corners').checked, auto_rotate: $('#su-rotate').checked } }), 'Gespeichert');
    if (r) S.settings = r;
  },
};

function setupGlobalEvents() {
  $('.tabs').addEventListener('click', (e) => { const b = e.target.closest('[data-view]'); if (b) showView(b.dataset.view); });
  $('#pill-printer').onclick = () => showView('printer');
  $('#pill-calib').onclick = () => showView('calibration');
  document.addEventListener('click', async (e) => {
    const a = e.target.closest('[data-action]');
    if (a && ACTIONS[a.dataset.action]) { e.preventDefault(); await ACTIONS[a.dataset.action](); return; }
    if (a && a.dataset.action && (a.dataset.action.startsWith('cal-') || a.dataset.action.startsWith('cut-'))) { e.preventDefault(); await calibAction(a.dataset.action); return; }
    const jd = e.target.closest('[data-job-detail]');
    if (jd) { showJobDetail(jd.dataset.jobDetail); return; }
    const c = e.target.closest('[data-cancel]');
    if (c) { await guard(() => api('POST', `/api/printer/jobs/${c.dataset.cancel}/cancel`), 'Abgebrochen'); renderJobs(); }
  });

  // Dateien von außen ins Fenster ziehen -> Upload-Dialog
  const ov = $('#drop-overlay');
  let depth = 0;
  const isFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  window.addEventListener('dragenter', (e) => { if (isFiles(e) && !$('.modal-bg')) { depth++; ov.classList.add('show'); } });
  window.addEventListener('dragleave', (e) => { if (isFiles(e)) { depth = Math.max(0, depth - 1); if (!depth) ov.classList.remove('show'); } });
  window.addEventListener('dragover', (e) => { if (isFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', async (e) => {
    if (!isFiles(e)) return;
    e.preventDefault();
    depth = 0; ov.classList.remove('show');
    if ($('.modal-bg')) return;
    const files = await filesFromDrop(e.dataTransfer);
    if (files.length) openUpload(files);
  });
  window.addEventListener('beforeunload', () => { if (saveTimer) saveNow(); });
  setInterval(() => { if (S.view === 'printer') renderJobs(); else refreshPrinterPill(); }, 15000);
}

async function init() {
  S.info = await api('GET', '/api/info');
  S.settings = await api('GET', '/api/settings');
  try { S.zoom = Number(localStorage.getItem('zoom')) || 1; $('#zoom').value = S.zoom; } catch (_) {}
  await Promise.all([loadCards(), loadProjects(), loadCalibration()]);
  const last = S.settings.last_project && S.projects.find((p) => p.id === S.settings.last_project) ? S.settings.last_project : S.projects[0].id;
  setupGlobalEvents();
  setupEditorEvents();
  setupLibraryEvents();
  setupCalibrationEvents();
  await openProject(last);
  let v = 'editor';
  try { v = localStorage.getItem('view') || 'editor'; } catch (_) {}
  showView(v);
  refreshPrinterPill();
}

init().catch((e) => { console.error(e); toast(`Fehler beim Laden: ${e.message}`, 'bad', 15000); });
