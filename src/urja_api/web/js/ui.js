// DOM helpers shared by the views: an element builder, badges, async regions and tables.
// Text from the API always goes into the DOM as text nodes, never as HTML.

import { ApiError, hasKey, onKey, requestKey } from './api.js';
import { STATUSES, STATUS_LABEL, SEVERITY_LABEL, humanize, num } from './format.js';

const SVG = 'http://www.w3.org/2000/svg';
let ids = 0;
export const uid = (prefix = 'u') => `${prefix}-${++ids}`;

/** h('a', { href, class, onclick, dataset, style }, ...children); null/false are skipped. */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style') for (const [name, v] of Object.entries(value)) el.style.setProperty(name, v);
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  return append(el, ...children);
}

/** Like Element.append, but skips null/false/'' so `cond && node` is safe. */
export function append(el, ...children) {
  el.append(...children.flat(Infinity).filter((child) => child != null && child !== false && child !== ''));
  return el;
}

export function icon(name, className) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', className ? `icon ${className}` : 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

export function debounce(fn, ms = 300) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ----------------------------------------------------------------------------- external libraries

const assets = new Map();

function loadAsset(tag, url, integrity) {
  if (!assets.has(url)) {
    const promise = new Promise((resolve, reject) => {
      const el =
        tag === 'script'
          ? h('script', { src: url, integrity, crossorigin: 'anonymous' })
          : h('link', { rel: 'stylesheet', href: url, integrity, crossorigin: 'anonymous' });
      el.onload = resolve;
      el.onerror = () => {
        assets.delete(url);
        el.remove();
        reject(new Error(`Could not load a library from ${new URL(url).host}. Check the connection, then retry.`));
      };
      document.head.append(el);
    });
    assets.set(url, promise);
  }
  return assets.get(url);
}

export const loadScript = (url, integrity) => loadAsset('script', url, integrity);
export const loadStyle = (url, integrity) => loadAsset('link', url, integrity);

// ----------------------------------------------------------------------------- small components

const STATUS_ICON = { installed: 'installed', faulty: 'warning', decommissioned: 'decommissioned' };
const SEVERITY_ICON = { error: 'error', warning: 'warning', info: 'info' };

export function badge(text, tone, iconName, { wrap = false } = {}) {
  return h('span', { class: wrap ? 'badge wrap' : 'badge', dataset: { tone } }, iconName && icon(iconName), text);
}

export function statusBadge(status) {
  const known = status in STATUS_ICON;
  return badge(STATUS_LABEL[status] ?? humanize(status), known ? status : 'unknown', STATUS_ICON[status] ?? 'unknown');
}

export const severityBadge = (severity) =>
  badge(SEVERITY_LABEL[severity] ?? humanize(severity), severity, SEVERITY_ICON[severity] ?? 'info');

export function mixLabel(counts) {
  const parts = STATUSES.filter((s) => counts?.[s] > 0).map((s) => `${num(counts[s])} ${STATUS_LABEL[s].toLowerCase()}`);
  return parts.join(', ') || 'no meters';
}

/** Meters by status as a stacked bar; the counts are its accessible name. */
export function mixBar(counts, className = '') {
  const label = mixLabel(counts);
  return h(
    'span',
    { class: `mix ${className}`.trim(), role: 'img', 'aria-label': label, title: label },
    STATUSES.filter((s) => counts?.[s] > 0).map((s) => h('span', { dataset: { tone: s }, style: { 'flex-grow': String(counts[s]) } })),
  );
}

export function statusLegend(counts) {
  return h(
    'ul',
    { class: 'legend' },
    STATUSES.filter((s) => counts?.[s] > 0).map((s) =>
      h('li', null, h('span', { class: 'swatch', dataset: { tone: s } }), STATUS_LABEL[s], h('b', null, num(counts[s]))),
    ),
  );
}

export function legend(items) {
  return h(
    'ul',
    { class: 'legend' },
    items.map(({ label, tone, hollow }) => h('li', null, h('span', { class: hollow ? 'swatch hollow' : 'swatch', dataset: { tone } }), label)),
  );
}

export function card({ title, subtitle, actions, className, level = 'h2' }, ...body) {
  const id = uid('card');
  return h(
    'section',
    { class: className ? `card ${className}` : 'card', 'aria-labelledby': title ? id : null },
    (title || actions) && h('div', { class: 'card-head' }, h('div', null, title && h(level, { id }, title), subtitle && h('p', null, subtitle)), actions),
    ...body,
  );
}

export function kpi({ label, iconName, value, unit, sub }, ...extra) {
  return h(
    'div',
    { class: 'kpi' },
    h('span', { class: 'kpi-label' }, iconName && icon(iconName), label),
    h('span', { class: 'kpi-value' }, value, unit && h('small', null, ` ${unit}`)),
    sub && h('span', { class: 'kpi-sub' }, sub),
    ...extra,
  );
}

export const meterLink = (id) => h('a', { href: `#/meters?meter=${encodeURIComponent(id)}`, class: 'meter-link', dataset: { meter: id } }, id);
export const nodeHref = (level, code) => `#/network?level=${level}&code=${encodeURIComponent(code)}`;
export const empty = (text) => h('p', { class: 'empty' }, text);

/** columns: [{ label, cell(row), num?, opt? (hidden on narrow screens), sort? }].
 *  `stack` turns each row into a labelled card on narrow screens instead of hiding columns.
 *  Sortable headers carry `data-sort`, so a view can put focus back after re-rendering. */
export function dataTable({ caption, columns, rows, sort, onSort, rowProps, stack = false }) {
  const cls = (col) => [col.num && 'num', col.opt && 'opt', col.className].filter(Boolean).join(' ') || null;
  const header = columns.map((col) => {
    if (!col.sort || !onSort) return h('th', { scope: 'col', class: cls(col) }, col.label);
    const active = sort?.key === col.sort;
    return h(
      'th',
      { scope: 'col', class: cls(col), dataset: { sort: col.sort }, 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : null },
      h('button', { type: 'button', onclick: () => onSort(col.sort) }, col.label, icon('chevron')),
    );
  });
  return h(
    'div',
    { class: 'table-wrap' },
    h(
      'table',
      { class: stack ? 'data stack' : 'data' },
      caption && h('caption', { class: 'sr-only' }, caption),
      h('thead', null, h('tr', null, header)),
      h('tbody', null, rows.map((row) => h('tr', rowProps?.(row), columns.map((col) => h('td', { class: cls(col), dataset: stack ? { label: col.label } : null }, col.cell(row)))))),
    ),
  );
}

// ----------------------------------------------------------------------------- async regions

const loading = (text = 'Loading…') => h('div', { class: 'state', role: 'status' }, h('span', { class: 'spinner' }), text);

/** An element whose content comes from the API. It shows loading, error and retry states;
 *  a newer load cancels an older one, and a refresh keeps the old content (dimmed) until
 *  the new one arrives. `slow` is shown if the first load takes a while. */
export class Region {
  constructor(el, { slow } = {}) {
    this.el = el;
    this.slow = slow;
    this.controller = null;
    this.timer = null;
  }

  load(fetcher, render) {
    this.controller?.abort();
    clearTimeout(this.timer);
    const controller = (this.controller = new AbortController());
    const { el } = this;
    const refreshing = el.dataset.state === 'ready';
    el.setAttribute('aria-busy', 'true');
    el.classList.toggle('is-refreshing', refreshing);
    if (!refreshing) {
      el.replaceChildren(loading());
      if (this.slow) this.timer = setTimeout(() => el.replaceChildren(loading(this.slow)), 2500);
    }
    const settle = (show) => {
      if (controller.signal.aborted) return;
      clearTimeout(this.timer);
      el.removeAttribute('aria-busy');
      el.classList.remove('is-refreshing');
      el.replaceChildren();
      show();
    };
    const retry = () => this.load(fetcher, render);
    return (async () => {
      try {
        const data = await fetcher(controller.signal);
        settle(() => {
          render(data, el);
          el.dataset.state = 'ready';
        });
      } catch (error) {
        if (error?.name === 'AbortError') return;
        if (!(error instanceof ApiError) && !error?.title) console.error(error);
        settle(() => {
          el.dataset.state = 'error';
          el.append(problemView(error, retry));
        });
      }
    })();
  }
}

function describe(error) {
  if (!(error instanceof ApiError)) {
    return { title: error?.title ?? 'Something went wrong', text: error?.message ?? String(error), tone: error?.title && 'warning', autoRetry: error?.retryAfter };
  }
  const text = error.message;
  if (error.code === 'network') return { title: 'Cannot reach the API', text };
  if (error.code === 'index_not_ready') {
    // The API's own detail says whether the first sync is still running or failed, and why.
    const failed = /failed/i.test(text);
    return {
      title: failed ? 'The first sync with the portal failed' : 'Still syncing with the portal',
      text: text || 'The API is building its local copy of the portal’s data for the first time. This usually takes under a minute.',
      tone: 'warning',
      autoRetry: error.retryAfter ?? 10,
    };
  }
  if (error.code === 'upstream_rate_limited') return { title: 'The portal is rate limiting requests', text, tone: 'warning', hint: error.retryAfter };
  if (error.code === 'upstream_unavailable') return { title: 'The portal is not responding', text, tone: 'warning', hint: error.retryAfter };
  if (error.status === 502) return { title: 'The portal answered unexpectedly', text };
  if (error.status === 401) return { title: 'API key required', text, key: true };
  if (error.status === 404) return { title: 'Not found', text };
  if (error.status === 422) {
    const details = error.errors.map((e) => e.message).filter(Boolean);
    return { title: 'The request was not accepted', text, details };
  }
  return { title: `Error ${error.status}`, text };
}

const awaitingKey = new Set(); // { view, retry } of regions that failed with 401

// Whichever prompt a key came from (this button, or a later request's 401), every region still
// showing "API key required" tries again with it.
onKey(() => {
  const waiting = [...awaitingKey];
  awaitingKey.clear();
  for (const { view, retry } of waiting) if (view.isConnected) retry();
});

const enterKey = () => requestKey({ force: true, rejected: hasKey() });

export function problemView(error, retry) {
  const info = describe(error);
  const actions = h('div', { class: 'state-actions' });
  const body = h(
    'div',
    { class: 'state-body' },
    h('strong', null, info.title),
    h('span', null, info.text),
    info.details?.length > 0 && h('ul', null, info.details.map((d) => h('li', null, d))),
    info.hint && h('span', null, `Try again in about ${num(info.hint)} s.`),
    actions,
  );
  const view = h('div', { class: 'state error', role: 'alert', dataset: info.tone ? { tone: info.tone } : null }, icon(info.tone === 'warning' ? 'clock' : 'error'), body);
  if (info.key) {
    if (retry) {
      for (const entry of awaitingKey) if (!entry.view.isConnected) awaitingKey.delete(entry); // replaced since
      awaitingKey.add({ view, retry });
    }
    actions.append(h('button', { type: 'button', class: 'btn primary small', onclick: enterKey }, icon('key'), 'Enter API key'));
  }
  if (!retry) return view;
  if (error?.status !== 404) actions.append(h('button', { type: 'button', class: 'btn small', onclick: retry }, icon('refresh'), 'Retry'));
  if (info.autoRetry) {
    let left = info.autoRetry;
    const counter = h('span', { class: 'small' }, `Retrying in ${left} s…`);
    actions.append(counter);
    const tick = setInterval(() => {
      if (!view.isConnected) return clearInterval(tick);
      left -= 1;
      counter.textContent = `Retrying in ${left} s…`;
      if (left <= 0) {
        clearInterval(tick);
        retry();
      }
    }, 1000);
  }
  return view;
}
