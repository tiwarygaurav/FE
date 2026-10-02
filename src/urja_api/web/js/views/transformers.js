// Transformers: rating, meters served and metered energy per distribution transformer (DT).

import * as api from '../api.js';
import { dayRange, kwh, num, pct, plural } from '../format.js';
import { href, setParams } from '../router.js';
import { Region, append, badge, dataTable, empty, h, icon, mixBar, nodeHref } from '../ui.js';

const RANGES = { week: 'Last complete week', all: 'All complete days' };
const SORTS = {
  code: (r) => r.code,
  capacity: (r) => r.capacity_kva ?? -1,
  meters: (r) => r.meter_count,
  kwh: (r) => r.kwh,
  load: (r) => r.avgKw ?? -1,
  loading: (r) => r.loading ?? -1,
  decommissioned: (r) => r.decommissionedShare,
};
const SORT_CHOICES = [
  ['kwh', 'desc', 'Energy, highest first'], ['loading', 'desc', 'Load vs rating, highest first'],
  ['capacity', 'desc', 'Rating, largest first'], ['meters', 'desc', 'Meters, most first'],
  ['decommissioned', 'desc', 'Decommissioned share, highest first'], ['code', 'asc', 'Transformer code'],
];
let region;
let rangeBoxes;
let sortSelect;
let range = 'week';
let sort = { key: 'kwh', dir: 'desc' };
let shown = null; // the data in the table: { range, rows, consumption }
let pending = null; // { range } of the fetch in flight
let refocus = null; // sort key of the heading just clicked
let lastKey = null;

const go = (changes) => setParams({ range: range === 'week' ? '' : range, sort: sort.key, dir: sort.dir, ...changes });

export function mount(el) {
  rangeBoxes = Object.entries(RANGES).map(([value, label]) =>
    h('label', null, h('input', { type: 'radio', name: 'dt-range', value, onchange: () => go({ range: value === 'week' ? '' : value }) }), label));
  sortSelect = h('select', { onchange: () => {
    const [key, dir] = sortSelect.value.split(':');
    go({ sort: key, dir });
  } }, SORT_CHOICES.map(([key, dir, label]) => h('option', { value: `${key}:${dir}` }, label)));
  const tableEl = h('div');
  region = new Region(tableEl);
  el.append(
    h('header', { class: 'view-head' },
      h('div', null,
        h('h1', { tabindex: '-1' }, 'Transformers'),
        h('p', null, 'Each distribution transformer with its rating, the meters it serves and the energy they used. Select a column heading to sort.')),
      h('fieldset', { class: 'segmented' }, h('legend', { class: 'sr-only' }, 'Period'), rangeBoxes)),
    h('section', { class: 'card', 'aria-label': 'Transformers' },
      h('label', { class: 'field narrow-only', style: { 'margin-bottom': '12px' } }, 'Sort by', sortSelect),
      tableEl,
      h('p', { class: 'card-foot' },
        'Average load is energy divided by the hours in the complete days counted. Load vs rating compares it with the nameplate kVA at unity power factor. '
        + 'Only this fleet’s meters are counted and peaks run above averages, so real loading is higher.')),
  );
}

export function update(route) {
  const key = Object.hasOwn(SORTS, route.get('sort') ?? '') ? route.get('sort') : 'kwh';
  const next = {
    range: route.get('range') === 'all' ? 'all' : 'week',
    sort: { key, dir: route.get('dir') === 'asc' || (route.get('dir') !== 'desc' && key === 'code') ? 'asc' : 'desc' },
  };
  const routeKey = `${next.range}|${next.sort.key}|${next.sort.dir}`;
  if (routeKey === lastKey) return;
  lastKey = routeKey;
  ({ range, sort } = next);
  for (const box of rangeBoxes) box.querySelector('input').checked = box.querySelector('input').value === range;
  sortSelect.value = `${sort.key}:${sort.dir}`;

  const focusSort = refocus; // a heading's sort button: put focus back on its replacement
  refocus = null;
  // Data is only ever shown under the period it was fetched for: after a failed switch, a
  // sort change fetches the selected period again instead of re-sorting the previous one.
  if (pending?.range === range) return; // on its way, and it renders with the current sort
  if (shown?.range === range) {
    pending = null;
    region.load(async () => shown, (data, el) => render(data, el, focusSort));
    return;
  }
  const job = { range };
  pending = job;
  region.load(() => load(job.range), (data, el) => {
    shown = data;
    render(data, el, focusSort);
  }).finally(() => {
    if (pending === job) pending = null;
  });
}

async function load(period) {
  const params = { group_by: 'transformer', limit: 500 };
  // The week is the API's default window (the 7 days up to the latest reading); "all" starts
  // at the first cached reading. Either way the API reports which complete days it counted.
  if (period === 'all') {
    const span = await api.fleetSpan();
    if (span) params.from = span.first;
  }
  const [transformers, all, active, quality, meters] = await Promise.all([
    api.transformers(),
    api.consumption(params),
    api.consumption({ ...params, include_decommissioned: false }),
    api.dataQuality().catch(() => null),
    api.allMeters().catch(() => []),
  ]);
  const hours = all.days_counted * 24;
  const energy = new Map(all.groups.map((g) => [g.key, g]));
  const activeEnergy = new Map(active.groups.map((g) => [g.key, g.consumption_kwh]));
  const dtOf = new Map(meters.map((m) => [m.meter_id, m.transformer_code]));
  const variantReports = new Map();
  for (const m of quality?.meters ?? []) {
    if (m.issues.some((i) => i.code === 'stale_name' && i.level === 'transformer')) {
      const code = dtOf.get(m.meter_id);
      variantReports.set(code, (variantReports.get(code) ?? 0) + 1);
    }
  }
  const rows = transformers.items.map((t) => {
    const used = energy.get(t.code)?.consumption_kwh ?? 0;
    const decommissioned = Math.max(0, used - (activeEnergy.get(t.code) ?? 0));
    const avgKw = hours ? used / hours : null;
    return {
      ...t,
      kwh: used,
      decommissioned,
      decommissionedShare: used ? decommissioned / used : 0,
      avgKw,
      loading: avgKw != null && t.capacity_kva ? avgKw / t.capacity_kva : null,
      variantReports: variantReports.get(t.code) ?? 0,
    };
  });
  return { range: period, rows, consumption: all };
}

function applySort(key) {
  refocus = key;
  go({ sort: key, dir: sort.key === key ? (sort.dir === 'asc' ? 'desc' : 'asc') : key === 'code' ? 'asc' : 'desc' });
}

function render(data, el, focusSort) {
  renderSummary(data, el);
  renderTable(data, el);
  if (focusSort) el.querySelector(`th[data-sort="${focusSort}"] button`)?.focus();
}

function renderTable({ rows }, el) {
  if (!rows.length) {
    el.append(empty('No transformers in the index.'));
    return;
  }
  const value = SORTS[sort.key];
  const sorted = [...rows].sort((a, b) => {
    const [x, y] = [value(a), value(b)];
    const order = typeof x === 'string' ? x.localeCompare(y) : x - y;
    return (sort.dir === 'asc' ? order : -order) || a.code.localeCompare(b.code);
  });
  const max = Math.max(...rows.map((r) => r.kwh), 0);
  el.append(dataTable({
    caption: 'Transformers',
    sort,
    onSort: applySort,
    stack: true,
    columns: [
      {
        label: 'Transformer',
        sort: 'code',
        cell: (r) => [
          h('a', { href: nodeHref('transformer', r.code) }, h('b', null, r.code)),
          h('span', { class: 'sub' }, `${r.name} · feeder ${r.feeder_code}`),
          r.name_variants.map((variant) => h('span', { class: 'sub' },
            badge(`Also reported as “${variant}”${r.variantReports ? ` by ${plural(r.variantReports, 'meter')}` : ''}`, 'warning', 'warning', { wrap: true }))),
        ],
      },
      { label: 'Rating', num: true, sort: 'capacity', cell: (r) => (r.capacity_kva != null ? `${num(r.capacity_kva)} kVA` : '—') },
      {
        label: 'Meters',
        sort: 'meters',
        cell: (r) => h('div', { class: 'grid', style: { gap: '4px' } },
          h('a', { href: href('meters', { transformer: r.code }) }, num(r.meter_count)), mixBar(r.meters_by_status, 'thin')),
      },
      {
        label: 'Energy (kWh)',
        sort: 'kwh',
        className: 'bar-col',
        cell: (r) => h('div', { class: 'cell-bar' },
          h('div', { class: 'track' },
            r.kwh > 0 && h('span', { class: 'end', style: { 'flex-grow': String(r.kwh) } }),
            h('i', { style: { 'flex-grow': String(max - r.kwh) } })),
          h('span', { class: 'num', style: { 'min-width': '64px', 'text-align': 'right' } }, kwh(r.kwh, { unit: false }))),
      },
      { label: 'Average load', num: true, opt: true, sort: 'load', cell: (r) => (r.avgKw != null ? `${num(r.avgKw, 1)} kW` : '—') },
      { label: 'Load vs rating', num: true, sort: 'loading', cell: (r) => pct(r.loading) },
      { label: 'Decommissioned share', num: true, opt: true, sort: 'decommissioned', cell: (r) => pct(r.decommissionedShare) },
    ],
    rows: sorted,
  }));
}

function renderSummary({ rows, consumption: c }, el) {
  const capacity = rows.reduce((sum, r) => sum + (r.capacity_kva ?? 0), 0);
  const decommissioned = rows.reduce((sum, r) => sum + r.decommissioned, 0);
  const busiest = rows.filter((r) => r.loading != null).sort((a, b) => b.loading - a.loading)[0];
  const renamed = rows.filter((r) => r.name_variants.length);
  append(el,
    h('p', { style: { 'margin-bottom': '12px' } },
      `${plural(rows.length, 'transformer')} with ${num(capacity / 1000, 1)} MVA of rated capacity. `,
      c.days_counted
        ? `Over ${dayRange(c.first_day, c.last_day)} (${plural(c.days_counted, 'complete day')}) their meters used ${kwh(c.total_kwh)}, `
          + `${pct(c.total_kwh ? decommissioned / c.total_kwh : 0)} of it on decommissioned meters. `
        : 'No complete day of readings (one with a reading at both midnights) is cached for this period yet. ',
      busiest ? `The most loaded is ${busiest.code} (${busiest.name}) at ${pct(busiest.loading)} of its rating on average.` : ''),
    c.meters_analysed < c.meters_total && h('p', { class: 'callout', dataset: { tone: 'warning' }, style: { 'margin-bottom': '12px' } }, icon('warning'),
      `Readings are cached for ${num(c.meters_analysed)} of ${num(c.meters_total)} meters so far, so these totals are incomplete.`),
    renamed.length > 0 && h('div', { class: 'callout', dataset: { tone: 'warning' }, style: { 'margin-bottom': '12px' } }, icon('warning'), h('span', null,
      `${plural(renamed.length, 'transformer')} ${renamed.length === 1 ? 'is' : 'are'} also reported under an outdated name: `,
      renamed.map((r, i) => [i ? '; ' : '', h('b', null, r.code), ` (${r.name}) as ${r.name_variants.map((v) => `“${v}”`).join(', ')}`]),
      '. The API serves the name from the portal’s transformer list.')),
  );
}
