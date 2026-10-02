// Overview: the fleet at a glance, led by the finding that matters most.

import * as api from '../api.js';
import {
  SEVERITIES, addDays, ago, age, ageDays, date, dateTime, dayRange, kwh, num, pct, plural, ruleLabel,
} from '../format.js';
import { href } from '../router.js';
import {
  Region, append, card, dataTable, empty, h, icon, kpi, legend, meterLink, mixBar, severityBadge, statusBadge, statusLegend,
} from '../ui.js';

const GROUPS = {
  zone: 'Zone', circle: 'Circle', division: 'Division', substation: 'Substation',
  feeder: 'Feeder', transformer: 'Transformer', make: 'Make',
};
const VISIBLE_ROWS = 12;
let groupBy = 'zone';
let regions;
let windowNote;

export function mount(el) {
  const heroEl = h('section', { class: 'card', 'aria-label': 'Key finding' });
  const tiles = ['meters', 'transformers', 'freshness', 'anomalies'].map((name) => [name, h('div', { class: 'card' })]);
  windowNote = h('span', null, 'Metered energy by group');
  const select = h('select', { id: 'overview-group', onchange: (e) => { groupBy = e.target.value; loadEnergy(); } },
    Object.entries(GROUPS).map(([value, label]) => h('option', { value }, label)));
  const energyEl = h('div');
  const topEl = h('div');

  el.append(
    h('header', { class: 'view-head' }, h('div', null,
      h('h1', { tabindex: '-1' }, 'Overview'),
      h('p', null, 'The Jaipur smart-meter fleet at a glance: what is installed, what is wrong with it and where the energy goes.'))),
    heroEl,
    h('div', { class: 'kpis' }, tiles.map(([, tile]) => tile)),
    h('div', { class: 'two-col' },
      card({ title: 'Where the energy goes', subtitle: windowNote, actions: h('label', { class: 'field' }, 'Group by', select) },
        legend([{ label: 'Installed and faulty meters', tone: 'context' }, { label: 'Decommissioned meters', tone: 'decommissioned' }]),
        energyEl),
      card({ title: 'Top consumers', subtitle: 'The ten meters that used the most energy in the window.' }, topEl)),
  );
  regions = {
    hero: new Region(heroEl),
    ...Object.fromEntries(tiles.map(([name, tile]) => [name, new Region(tile)])),
    energy: new Region(energyEl),
    top: new Region(topEl),
  };
}

let loaded = false;
export function update() {
  if (loaded) return;
  loaded = true;
  regions.hero.load(loadHero, renderHero);
  regions.meters.load(() => api.allMeters(), renderMeters);
  regions.transformers.load(() => Promise.all([api.transformers(), api.networkOverview()]), renderTransformers);
  loadFreshness();
  regions.anomalies.load(loadAnomalies, renderAnomalies);
  loadEnergy();
  regions.top.load(loadTop, renderTop);
}

/** Consumption over the last 30 days of data. The API counts complete IST days only and says
 *  which ones (days_counted, first_day, last_day), so labels come from the response. */
async function monthParams() {
  const span = await api.fleetSpan();
  if (!span) {
    const error = new Error('The API fetches every meter’s readings in the background after its first sync. Retry in a minute.');
    error.title = 'No readings cached yet';
    error.retryAfter = 30;
    throw error;
  }
  return { from: addDays(span.last, -29) };
}

/** "1–29 Jun 2026 (29 complete days)", or null when no complete day was counted. */
const countedDays = (c) => (c.days_counted ? `${dayRange(c.first_day, c.last_day)} (${plural(c.days_counted, 'complete day')})` : null);
const NO_DAYS = 'No complete day of readings (one with a reading at both midnights) is cached for the last 30 days yet.';

function coverageNote(c) {
  if (c.meters_analysed >= c.meters_total) return null;
  return h('p', { class: 'callout', dataset: { tone: 'warning' }, style: { 'margin-bottom': '12px' } }, icon('warning'),
    `Readings are cached for ${num(c.meters_analysed)} of ${num(c.meters_total)} meters so far, so these totals are incomplete.`);
}

const countBy = (items, key) => items.reduce((acc, item) => ({ ...acc, [item[key]]: (acc[item[key]] ?? 0) + 1 }), {});

// ----------------------------------------------------------------------------- key finding

async function loadHero() {
  const params = await monthParams();
  const [byStatus, anomalies, meters] = await Promise.all([
    api.consumption({ group_by: 'status', ...params }),
    api.anomalyCounts(),
    api.allMeters(),
  ]);
  return { byStatus, anomalies, meters };
}

function renderHero({ byStatus, anomalies, meters }, el) {
  const decommissioned = meters.filter((m) => m.status === 'decommissioned').length;
  const consuming = anomalies.meters_by_rule.decommissioned_reporting ?? 0;
  const group = byStatus.groups.find((g) => g.key === 'decommissioned');
  const days = countedDays(byStatus);
  if (!group?.consumption_kwh) {
    el.append(h('p', null, days ? `No energy was recorded on decommissioned meters over ${days}.` : NO_DAYS));
    return;
  }
  const headline = consuming === decommissioned
    ? `All ${num(decommissioned)} decommissioned meters are still consuming energy`
    : `${num(consuming)} of ${num(decommissioned)} decommissioned meters are still consuming energy`;
  const partial = byStatus.meters_analysed < byStatus.meters_total
    ? ` So far these figures cover the ${num(byStatus.meters_analysed)} of ${num(byStatus.meters_total)} meters whose readings are cached.`
    : '';
  el.classList.add('hero');
  el.append(
    h('div', { class: 'hero-figure' }, pct(group.share), h('small', null, 'of metered energy')),
    h('div', null,
      h('h2', null, icon('warning'), headline),
      h('p', null,
        `Over ${days} they recorded ${kwh(group.consumption_kwh)}, `
        + `${pct(group.share)} of the ${kwh(byStatus.total_kwh)} metered across the fleet. Either their status in the portal `
        + `is out of date or this energy is not being billed. Both call for a field check.${partial}`),
      h('div', { class: 'actions' },
        h('a', { class: 'btn primary', href: href('meters', { status: 'decommissioned' }) }, `List the ${num(decommissioned)} meters`, icon('arrow')),
        h('a', { class: 'btn', href: href('map', { status: 'decommissioned' }) }, icon('map'), 'Show on map'))),
  );
}

// ----------------------------------------------------------------------------- tiles

function renderMeters(meters, el) {
  const counts = countBy(meters, 'status');
  const share = (status) => pct((counts[status] ?? 0) / meters.length, 0);
  el.append(kpi(
    { label: 'Meters', iconName: 'meters', value: num(meters.length), sub: `${share('faulty')} faulty, ${share('decommissioned')} decommissioned` },
    mixBar(counts),
    statusLegend(counts),
  ));
}

function renderTransformers([transformers, network], el) {
  const capacities = transformers.items.map((t) => t.capacity_kva).filter((c) => c != null);
  const mva = capacities.reduce((sum, c) => sum + c, 0) / 1000;
  const counts = network.node_counts;
  el.append(kpi(
    { label: 'Transformers', iconName: 'transformers', value: num(transformers.total), sub: `${num(mva, 1)} MVA of installed capacity` },
    h('span', { class: 'kpi-sub' }, `${plural(counts.feeder, 'feeder')}, ${plural(counts.substation, 'substation')}, ${plural(counts.zone, 'zone')}`),
    h('a', { class: 'small', href: href('transformers') }, 'Loading by transformer'),
  ));
}

const SYNC_SOURCE = { export: 'bulk export', crawl: 'page crawl' };
let freshnessTimer = null;

/** Refreshed every 15 s while the API is still syncing or fetching readings. */
function loadFreshness() {
  clearTimeout(freshnessTimer);
  regions.freshness.load(() => api.status(), (status, el) => {
    renderFreshness(status, el);
    if (!status.index_ready || status.readings_cache.warmup.state === 'running') freshnessTimer = setTimeout(loadFreshness, 15_000);
  });
}

function renderFreshness(status, el) {
  const { readings_cache: cache, reference_data: reference } = status;
  const { warmup } = cache;
  const synced = reference.last_success_at
    ? `Portal index synced ${ago(reference.last_success_at)} (${SYNC_SOURCE[reference.source] ?? reference.source ?? 'unknown source'}).`
    : 'The portal index has not been synced yet.';
  const lastRun = reference.last_run;
  const problems = [
    reference.stale && reference.last_success_at && 'The last successful sync is over two sync intervals old, so meter records may be out of date.',
    status.index_ready && lastRun?.status === 'failed' && `The latest sync attempt failed${lastRun.error ? `: ${lastRun.error}` : '.'}`,
  ].filter(Boolean).map((text) => h('div', { class: 'callout', dataset: { tone: 'warning' } }, icon('warning'), h('span', null, text)));
  const warming = warmup.state === 'running';
  const cached = warming
    ? `Fetching readings in the background: ${num(warmup.done)} of ${plural(warmup.total, 'meter')} done.`
    : `Readings cached for ${plural(cache.meter_count, 'meter')}.`;
  const failed = warmup.failed > 0 && `${plural(warmup.failed, 'meter')} could not be fetched${warming ? ' so far' : ' in the last warm-up'}.`;
  const last = cache.last_reading_at;
  if (!last) {
    el.append(kpi(
      { label: 'Data freshness', iconName: 'clock', value: 'No readings yet', sub: status.index_ready ? 'Readings are being fetched in the background.' : api.firstSyncState(reference) },
      ...problems,
      status.index_ready && h('span', { class: 'kpi-sub' }, cached, failed && ` ${failed}`),
    ));
    return;
  }
  const days = ageDays(last);
  el.append(kpi(
    { label: 'Data freshness', iconName: 'clock', value: `${num(Math.floor(days))} days old`, sub: `Latest reading ${dateTime(last)}` },
    days > 2 && h('div', { class: 'callout', dataset: { tone: 'warning' } }, icon('warning'),
      h('span', null, `The portal has nothing newer: its data stops ${age(days)} ago, so everything here describes the period up to ${date(last)}.`)),
    ...problems,
    h('span', { class: 'kpi-sub' }, synced),
    h('span', { class: 'kpi-sub' }, cached, failed && ` ${failed}`),
  ));
}

/** One counts-only call: meters per rule, each rule's severity, and distinct meters per severity. */
function loadAnomalies() {
  return api.anomalyCounts();
}

function renderAnomalies(all, el) {
  const severity = (rule) => all.rule_severity[rule] ?? 'info';
  const rank = (rule) => SEVERITIES.indexOf(severity(rule));
  const rules = Object.entries(all.meters_by_rule).sort(([a, n], [b, m]) => rank(a) - rank(b) || m - n);
  const withErrors = all.meters_by_severity.error ?? 0;
  el.append(kpi(
    { label: 'Anomalies', iconName: 'error', value: num(withErrors), unit: withErrors === 1 ? 'meter with errors' : 'meters with errors', sub: `${num(all.meters_analysed)} of ${num(all.meters_total)} meters analysed` },
    rules.length
      ? h('ul', { class: 'rules' }, rules.map(([rule, count]) => h('li', null,
        severityBadge(severity(rule)),
        h('span', null, ruleLabel(rule)),
        h('span', { class: 'count' }, count === all.meters_analysed ? 'all' : num(count)))))
      : h('span', { class: 'kpi-sub' }, 'No anomalies detected.'),
  ));
}

// ----------------------------------------------------------------------------- energy by group

function loadEnergy() {
  regions.energy.load(async () => {
    const params = { group_by: groupBy, limit: 500, ...(await monthParams()) };
    const [all, active] = await Promise.all([api.consumption(params), api.consumption({ ...params, include_decommissioned: false })]);
    return { all, active };
  }, renderEnergy);
}

function stackedBar(parts, max) {
  const visible = parts.filter((p) => p.value > 0);
  const total = visible.reduce((sum, p) => sum + p.value, 0);
  return h('div', { class: 'track' },
    visible.map((p, i) => h('span', { dataset: { tone: p.tone }, class: i === visible.length - 1 ? 'end' : null, style: { 'flex-grow': String(p.value) } })),
    h('i', { style: { 'flex-grow': String(Math.max(0, max - total)) } }));
}

function renderEnergy({ all, active }, el) {
  const days = countedDays(all);
  windowNote.textContent = days ? `${days}. ${kwh(all.total_kwh)} in total.` : 'Metered energy by group';
  if (!days) {
    el.append(empty(NO_DAYS));
    return;
  }
  const activeBy = new Map(active.groups.map((g) => [g.key, g.consumption_kwh]));
  const rows = all.groups.map((g) => {
    const decommissioned = Math.max(0, g.consumption_kwh - (activeBy.get(g.key) ?? 0));
    return { ...g, decommissioned, other: g.consumption_kwh - decommissioned };
  });
  if (!rows.length) {
    el.append(empty('No consumption in this window.'));
    return;
  }
  append(el, coverageNote(all));
  const max = Math.max(...rows.map((r) => r.consumption_kwh));
  const label = GROUPS[groupBy];
  const table = dataTable({
    caption: `Consumption by ${label.toLowerCase()}`,
    columns: [
      { label, cell: (r) => [h('b', null, r.key), r.name && h('span', { class: 'sub' }, r.name)] },
      {
        label: 'Consumption',
        className: 'bar-col',
        cell: (r) => {
          const label = `${kwh(r.other)} on installed and faulty meters, ${kwh(r.decommissioned)} on decommissioned meters`;
          return h('div', { class: 'cell-bar', role: 'img', 'aria-label': label, title: label },
            stackedBar([{ value: r.other, tone: 'context' }, { value: r.decommissioned, tone: 'decommissioned' }], max));
        },
      },
      { label: 'kWh', num: true, cell: (r) => kwh(r.consumption_kwh, { unit: false }) },
      { label: 'Decommissioned share', num: true, opt: true, cell: (r) => pct(r.consumption_kwh ? r.decommissioned / r.consumption_kwh : 0) },
    ],
    rows,
  });
  const body = table.querySelector('tbody');
  const extra = [...body.rows].slice(VISIBLE_ROWS);
  for (const row of extra) row.hidden = true;
  el.append(table);
  if (extra.length) {
    const button = h('button', { type: 'button', class: 'btn small show-more', 'aria-expanded': 'false' }, `Show all ${num(rows.length)}`);
    button.addEventListener('click', () => {
      const expand = button.getAttribute('aria-expanded') === 'false';
      for (const row of extra) row.hidden = !expand;
      button.setAttribute('aria-expanded', String(expand));
      button.textContent = expand ? `Show top ${VISIBLE_ROWS}` : `Show all ${num(rows.length)}`;
    });
    el.append(button);
  }
}

// ----------------------------------------------------------------------------- top consumers

async function loadTop() {
  const params = await monthParams();
  const [top, meters] = await Promise.all([
    api.consumption({ group_by: 'meter', ...params, limit: 10 }),
    api.allMeters(),
  ]);
  return { top, meters: new Map(meters.map((m) => [m.meter_id, m])) };
}

function renderTop({ top, meters }, el) {
  const days = countedDays(top);
  if (!days || !top.groups.length) {
    el.append(empty(days ? 'No consumption in this window.' : NO_DAYS));
    return;
  }
  el.append(dataTable({
    caption: 'Top consumers',
    columns: [
      { label: 'Meter', cell: (g) => meterLink(g.key) },
      { label: 'Status', cell: (g) => (meters.has(g.key) ? statusBadge(meters.get(g.key).status) : '') },
      { label: 'Transformer', opt: true, cell: (g) => meters.get(g.key)?.transformer_code ?? '' },
      { label: 'kWh', num: true, cell: (g) => kwh(g.consumption_kwh, { unit: false }) },
      { label: 'Share', num: true, opt: true, cell: (g) => pct(g.share, 2) },
    ],
    rows: top.groups,
    rowProps: (g) => ({ dataset: { meter: g.key } }),
  }));
  el.append(h('p', { class: 'card-foot' }, `${days}. Complete IST days only: a day counts when it has a reading at both midnights.`));
}
