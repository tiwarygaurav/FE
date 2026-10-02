// Meters: search, filter and sort the fleet (all server-side), then page through the matches.

import * as api from '../api.js';
import { INSTALL_LABEL, INTERVAL_LABEL, LEVELS, LEVEL_LABEL, PHASE_LABEL, STATUS_LABEL, intervalLabel, num, plural } from '../format.js';
import { setParams } from '../router.js';
import { Region, badge, dataTable, debounce, empty, h, icon, meterLink, nodeHref, statusBadge } from '../ui.js';

const SIZES = ['25', '50', '100', '200'];
const DEFAULT_SIZE = '50';
// The API's sort keys: `sort=key` ascending, `sort=-key` descending.
const SORTS = {
  meter_id: 'Meter ID', serial_number: 'Serial number', status: 'Status', make: 'Make', phase: 'Phase',
  installation_type: 'Installation', interval: 'Interval', transformer: 'Transformer', feeder: 'Feeder',
  issues: 'Data issues',
};
const DEFAULT_SORT = 'meter_id';
const firstSort = (key) => (key === 'issues' ? '-issues' : key); // what a first click on a heading asks for
let f; // form controls
let region;
let countEl;
let lastKey = null;
let refocus = null; // sort key of the heading just clicked

const option = (value, label) => h('option', { value }, label);
const field = (label, control, className) => h('label', { class: className ? `field ${className}` : 'field' }, label, control);

/** The `sort` param if valid, else the default. */
function sortParam(params) {
  const value = params.get('sort') ?? '';
  return Object.hasOwn(SORTS, value.replace(/^-/, '')) ? value : DEFAULT_SORT;
}

function sortLabel(value) {
  const key = value.replace(/^-/, '');
  if (key === 'issues') return value === key ? 'Data issues, fewest first' : 'Data issues, most first';
  return value === key ? SORTS[key] : `${SORTS[key]}, reversed`;
}

/** One choice per key in the direction a first click gives, plus the current one if reversed. */
function sortOptions(current) {
  return Object.keys(SORTS).flatMap((key) => {
    const first = firstSort(key);
    const reversed = first === key ? `-${key}` : key;
    return [option(first, sortLabel(first)), current === reversed && option(reversed, sortLabel(reversed))];
  }).filter(Boolean);
}

export function mount(el) {
  f = {
    q: h('input', { type: 'search', placeholder: 'Meter ID or serial number', autocomplete: 'off', spellcheck: 'false' }),
    status: ['installed', 'faulty', 'decommissioned'].map((value) => h('input', { type: 'checkbox', value })),
    make: h('select', null, option('', 'Any make')),
    phase: h('select', null, option('', 'Any phase'), ...['single', 'three'].map((v) => option(v, PHASE_LABEL[v]))),
    install: h('select', null, option('', 'Any type'), ...['whole_current', 'ct_operated'].map((v) => option(v, INSTALL_LABEL[v]))),
    interval: h('select', null, option('', 'Any interval'), ...Object.entries(INTERVAL_LABEL).map(([v, label]) => option(v, label))),
    level: h('select', null, option('', 'Any level'), ...LEVELS.map((level) => option(level, LEVEL_LABEL[level]))),
    code: h('select', { disabled: true }, option('', 'Any node')),
    issues: h('input', { type: 'checkbox' }),
    sort: h('select'),
    size: h('select', null, SIZES.map((size) => option(size, size))),
  };
  f.size.value = DEFAULT_SIZE;
  const results = h('div');
  countEl = h('p', { class: 'small muted', 'aria-live': 'polite' });
  region = new Region(results);

  el.append(
    h('header', { class: 'view-head' }, h('div', null,
      h('h1', { tabindex: '-1' }, 'Meters'),
      h('p', null, 'Search by meter ID or serial number, filter by any attribute or network node, and sort by any column. Select a row for the meter’s details.'))),
    h('section', { class: 'card', 'aria-label': 'Filters' },
      h('div', { class: 'filters' },
        field('Search', f.q, 'grow'),
        field('Make', f.make),
        field('Phase', f.phase),
        field('Installation', f.install),
        field('Reading interval', f.interval),
        field('Network level', f.level),
        field('Node', f.code)),
      h('div', { class: 'filters', style: { 'margin-top': '12px' } },
        h('fieldset', { class: 'check-group' }, h('legend', null, 'Status'),
          f.status.map((box) => h('label', { class: 'chip' }, box, h('span', { class: 'swatch', dataset: { tone: box.value } }), STATUS_LABEL[box.value]))),
        h('label', { class: 'chip' }, f.issues, icon('warning'), 'Only meters with data issues'),
        h('button', { type: 'button', class: 'btn ghost', onclick: clearFilters }, 'Clear filters'))),
    h('section', { class: 'card', 'aria-label': 'Results' },
      h('div', { class: 'results-head' }, countEl, field('Sort by', f.sort)),
      results),
  );

  const search = debounce(() => apply(), 300);
  f.q.addEventListener('input', search);
  for (const control of [f.make, f.phase, f.install, f.interval, f.issues, f.sort, f.size, ...f.status]) control.addEventListener('change', () => apply());
  f.level.addEventListener('change', () => {
    loadCodes(f.level.value, '');
    apply();
  });
  f.code.addEventListener('change', () => {
    f.code.dataset.wanted = f.code.value;
    apply();
  });

  api.allMeters().then((meters) => {
    const makes = [...new Set(meters.map((m) => m.make))].sort((a, b) => a.localeCompare(b));
    const wanted = f.make.dataset.wanted ?? f.make.value;
    f.make.append(...makes.map((make) => option(make, make)));
    f.make.value = wanted;
  }, () => {});
}

/** The network filter the form expresses, as `level=code` ('' for none). */
const selectedNode = () => (f.level.value && f.code.dataset.wanted ? `${f.level.value}=${f.code.dataset.wanted}` : '');

async function loadCodes(level, wanted) {
  f.code.dataset.wanted = wanted;
  f.code.replaceChildren(option('', 'Any node'));
  f.code.disabled = !level;
  if (!level) return;
  let nodes = [];
  try {
    nodes = await api.networkLevel(level);
  } catch {
    // Without the node list the select still offers the code from the link.
  }
  if (f.level.value !== level) return;
  f.code.append(...nodes.map((n) => option(n.code, n.name ? `${n.code} · ${n.name}` : n.code)));
  if (wanted && !nodes.some((n) => n.code === wanted)) f.code.append(option(wanted, wanted));
  f.code.value = f.code.dataset.wanted;
}

/** Sorting and page size are not filters: clearing keeps them. */
function clearFilters() {
  setParams({ sort: f.sort.value === DEFAULT_SORT ? '' : f.sort.value, size: f.size.value === DEFAULT_SIZE ? '' : f.size.value });
}

function apply(changes = {}) {
  const params = {
    q: f.q.value.trim(),
    status: f.status.filter((box) => box.checked).map((box) => box.value),
    make: f.make.options.length > 1 ? f.make.value : (f.make.dataset.wanted ?? ''),
    phase: f.phase.value,
    installation_type: f.install.value,
    interval_minutes: f.interval.value,
    has_issues: f.issues.checked ? 'true' : '',
    sort: f.sort.value === DEFAULT_SORT ? '' : f.sort.value,
    size: f.size.value === DEFAULT_SIZE ? '' : f.size.value,
    ...changes,
  };
  if (selectedNode()) params[f.level.value] = f.code.dataset.wanted;
  setParams(params);
}

function onSort(key) {
  const current = f.sort.value;
  const next = current === key ? `-${key}` : current === `-${key}` ? key : firstSort(key);
  refocus = key;
  apply({ sort: next === DEFAULT_SORT ? '' : next });
}

function syncForm(params) {
  if (f.q.value.trim() !== (params.get('q') ?? '')) f.q.value = params.get('q') ?? '';
  const statuses = params.getAll('status');
  for (const box of f.status) box.checked = statuses.includes(box.value);
  f.make.dataset.wanted = params.get('make') ?? '';
  if (f.make.options.length > 1) f.make.value = f.make.dataset.wanted;
  f.phase.value = params.get('phase') ?? '';
  f.install.value = params.get('installation_type') ?? '';
  f.interval.value = Object.hasOwn(INTERVAL_LABEL, params.get('interval_minutes') ?? '') ? params.get('interval_minutes') : '';
  f.issues.checked = params.get('has_issues') === 'true';
  const sort = sortParam(params);
  f.sort.replaceChildren(...sortOptions(sort));
  f.sort.value = sort;
  f.size.value = SIZES.includes(params.get('size')) ? params.get('size') : DEFAULT_SIZE;
  const level = LEVELS.find((l) => params.get(l)) ?? '';
  if ((level ? `${level}=${params.get(level)}` : '') !== selectedNode()) {
    f.level.value = level;
    loadCodes(level, level ? params.get(level) : '');
  }
}

/** Readings coverage, for the interval filter: only meters with cached readings can match it. */
async function readingsCoverage() {
  try {
    const cache = (await api.status()).readings_cache;
    return { cached: cache.meter_count, total: cache.meters_total, warming: cache.warmup.state === 'running' };
  } catch {
    return null; // only a note depends on it
  }
}

export function update(route) {
  const params = new URLSearchParams(route);
  params.delete('meter');
  const key = params.toString();
  if (key === lastKey) return;
  lastKey = key;
  syncForm(params);

  const size = Number(f.size.value);
  const page = Math.max(1, Number.parseInt(params.get('page') ?? '1', 10) || 1);
  const sort = f.sort.value;
  const query = {
    q: params.get('q'),
    status: params.getAll('status'),
    make: params.get('make'),
    phase: params.get('phase'),
    installation_type: params.get('installation_type'),
    interval_minutes: f.interval.value,
    has_issues: params.get('has_issues'),
    sort: sort === DEFAULT_SORT ? null : sort,
    limit: size,
    offset: (page - 1) * size,
  };
  for (const level of LEVELS) query[level] = params.get(level);

  const focusSort = refocus; // a heading's sort button: put focus back on its replacement
  refocus = null;
  countEl.textContent = '';
  region.load(
    (signal) => Promise.all([api.get('/v1/meters', query, { signal }), query.interval_minutes && readingsCoverage()]),
    ([result, coverage], el) => {
      renderResults(result, coverage, page, size, sort, el);
      if (focusSort) el.querySelector(`th[data-sort="${focusSort}"] button`)?.focus();
    },
  );
}

function renderResults(result, coverage, page, size, sort, el) {
  const pages = Math.max(1, Math.ceil(result.total / size));
  const first = result.offset + 1;
  const last = result.offset + result.items.length;
  countEl.textContent = result.total
    ? `Showing ${num(first)}–${num(last)} of ${num(result.total)} meters`
    : 'No meters match these filters.';
  if (coverage && coverage.cached < coverage.total) {
    el.append(h('p', { class: 'callout', dataset: { tone: 'warning' }, style: { 'margin-bottom': '12px' } }, icon('clock'), h('span', null,
      `Only meters whose readings are cached can match a reading interval, and readings are cached for ${num(coverage.cached)} of ${plural(coverage.total, 'meter')}`,
      coverage.warming ? ' so far. The API is still fetching the rest in the background.' : '.')));
  }
  if (!result.items.length) {
    el.append(result.total
      ? h('p', { class: 'empty' }, 'This page is past the last result. ', h('button', { type: 'button', class: 'link-btn', onclick: () => apply({ page: '' }) }, 'Go to the first page'))
      : empty('Try a shorter search or fewer filters.'));
    return;
  }
  const key = sort.replace(/^-/, '');
  el.append(dataTable({
    caption: `Meters, sorted by ${sortLabel(sort)}`,
    sort: { key, dir: sort === key ? 'asc' : 'desc' },
    onSort,
    columns: [
      { label: 'Meter', sort: 'meter_id', cell: (m) => meterLink(m.meter_id) },
      { label: 'Serial', sort: 'serial_number', opt: true, cell: (m) => m.serial_number },
      { label: 'Status', sort: 'status', cell: (m) => statusBadge(m.status) },
      { label: 'Make', sort: 'make', cell: (m) => m.make },
      { label: 'Phase', sort: 'phase', opt: true, cell: (m) => PHASE_LABEL[m.phase] ?? m.phase },
      { label: 'Installation', sort: 'installation_type', opt: true, className: 'opt-md', cell: (m) => INSTALL_LABEL[m.installation_type] ?? m.installation_type },
      {
        label: 'Interval',
        sort: 'interval',
        opt: true,
        className: 'opt-md',
        cell: (m) => (m.reading_interval_minutes == null ? h('span', { class: 'muted' }, 'Not cached') : intervalLabel(m.reading_interval_minutes)),
      },
      { label: 'Transformer', sort: 'transformer', cell: (m) => h('a', { href: nodeHref('transformer', m.transformer_code) }, m.transformer_code) },
      { label: 'Feeder', sort: 'feeder', opt: true, className: 'opt-md', cell: (m) => h('a', { href: nodeHref('feeder', m.feeder_code) }, m.feeder_code) },
      {
        label: 'Data issues',
        sort: 'issues',
        num: true,
        cell: (m) => (m.data_issue_count ? badge(num(m.data_issue_count), 'warning', 'warning') : ''),
      },
    ],
    rows: result.items,
    rowProps: (m) => ({ dataset: { meter: m.meter_id } }),
    stack: true,
  }));
  if (pages > 1) {
    const go = (target) => apply({ page: target > 1 ? String(target) : '' });
    el.append(h('nav', { class: 'pager', 'aria-label': 'Pages' },
      h('div', { class: 'pager-controls' },
        h('button', { type: 'button', class: 'btn small', disabled: page <= 1, onclick: () => go(page - 1) }, icon('back'), 'Previous'),
        h('span', { class: 'small muted' }, `Page ${num(page)} of ${num(pages)}`),
        h('button', { type: 'button', class: 'btn small', disabled: page >= pages, onclick: () => go(page + 1) }, 'Next', icon('chevron'))),
      field('Rows per page', f.size)));
  } else {
    el.append(h('div', { class: 'pager' }, h('span'), field('Rows per page', f.size)));
  }
}
