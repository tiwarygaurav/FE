// Data quality: what the API repaired in the portal's data, reading-level findings and the
// shape of the network hierarchy.

import * as api from '../api.js';
import { ISSUE_INFO, LEVEL_LABEL, dateTime, issueLabel, num, pct, plural } from '../format.js';
import { setParams } from '../router.js';
import { Region, append, badge, card, dataTable, empty, h, icon, kpi, meterLink } from '../ui.js';
import { edgesTable } from './network.js';

let regions;
let issuesEl;
let report = null;
let filter = '';
let started = false;

export function mount(el) {
  const summaryEl = h('div', { class: 'kpis' });
  issuesEl = h('div');
  const readingsEl = h('div');
  const networkEl = h('div');
  el.append(
    h('header', { class: 'view-head' }, h('div', null,
      h('h1', { tabindex: '-1' }, 'Data quality'),
      h('p', null, 'Everything the API had to repair in the portal’s data, with the value the portal reported next to the one served instead.'))),
    summaryEl,
    card({ title: 'Repaired meter records', subtitle: 'Network codes and names each meter reported, checked against the other meters on its transformer.' }, issuesEl),
    card({ title: 'Readings', subtitle: 'Findings in the cached reading series.' }, readingsEl),
    card({ title: 'Network hierarchy', subtitle: 'Whether each level has a single parent above it.' }, networkEl),
  );
  regions = { summary: new Region(summaryEl), readings: new Region(readingsEl), network: new Region(networkEl), issues: new Region(issuesEl) };
}

export function update(route) {
  const next = route.get('issue') ?? '';
  if (started && next === filter) return;
  filter = next;
  if (report) {
    issuesEl.replaceChildren();
    renderIssues(issuesEl);
  }
  if (started) return;
  started = true;
  // Each fetcher asks again (the TTL cache shares one request) so that Retry refetches.
  regions.summary.load(() => Promise.all([api.dataQuality(), api.allMeters().catch(() => null)]), renderSummary);
  regions.issues.load(() => api.dataQuality(), (data, el) => {
    report = data;
    renderIssues(el);
  });
  regions.network.load(() => api.dataQuality(), (data, el) => el.append(
    h('p', { class: 'small muted', style: { 'margin-bottom': '10px' } }, data.network.note), edgesTable(data.network)));
  regions.readings.load(() => Promise.all([api.dataQuality(), api.anomalyReport({ rule: 'duplicate_timestamps' })]), renderReadings);
}

function renderSummary([data, meters], el) {
  const total = meters?.length;
  const brokenLinks = data.network.edges.filter((e) => !e.functional).length;
  const tile = (...content) => h('div', { class: 'card' }, kpi(...content));
  el.append(
    tile({ label: 'Meters with repaired records', iconName: 'quality', value: num(data.meters_with_issues), sub: total ? `${pct(data.meters_with_issues / total)} of ${plural(total, 'meter')}` : null }),
    tile({ label: 'Repairs by type', iconName: 'warning', value: num(Object.values(data.issues_by_code).reduce((a, b) => a + b, 0)) },
      h('ul', { class: 'rules' }, Object.entries(data.issues_by_code).map(([code, count]) => h('li', null, issueLabel(code), h('span', { class: 'count' }, num(count)))))),
    tile({ label: 'Duplicate timestamps', iconName: 'clock', value: num(data.readings.duplicate_timestamps), unit: 'meters', sub: `of ${plural(data.readings.meters_cached, 'meter')} with cached readings` }),
    tile({ label: 'Network levels that are not a tree', iconName: 'network', value: `${num(brokenLinks)} of ${num(data.network.edges.length)}`, sub: data.network.is_tree ? 'The hierarchy is a tree.' : 'Codes are reused under several parents.' }),
  );
}

function renderIssues(el) {
  const rows = report.meters.flatMap((m) => m.issues.map((issue) => ({ meter_id: m.meter_id, ...issue })));
  if (!rows.length) {
    el.append(empty('Nothing needed repairing: every meter record agrees with its transformer.'));
    return;
  }
  const codes = Object.keys(report.issues_by_code);
  const chips = h('fieldset', { class: 'check-group', style: { 'margin-bottom': '12px' } }, h('legend', { class: 'sr-only' }, 'Issue type'),
    [['', `All ${num(rows.length)}`], ...codes.map((code) => [code, `${issueLabel(code)} ${num(report.issues_by_code[code])}`])].map(([value, label]) =>
      h('label', { class: 'chip' },
        h('input', { type: 'radio', name: 'issue-filter', value, checked: value === filter, onchange: () => setParams({ issue: value }) }), label)));
  const shown = filter ? rows.filter((r) => r.code === filter) : rows;
  append(el,
    chips,
    filter && ISSUE_INFO[filter] && h('p', { class: 'note', style: { 'margin-bottom': '10px' } }, icon('info'), ISSUE_INFO[filter][1]),
    dataTable({
      caption: 'Repaired meter records',
      columns: [
        { label: 'Meter', cell: (r) => meterLink(r.meter_id) },
        { label: 'Issue', cell: (r) => badge(issueLabel(r.code), 'warning', 'warning') },
        { label: 'Level', cell: (r) => LEVEL_LABEL[r.level] ?? '—' },
        { label: 'Portal reported', cell: (r) => (r.reported != null ? `“${r.reported}”` : h('span', { class: 'muted' }, 'nothing')) },
        { label: 'API serves', cell: (r) => (r.resolved != null ? `“${r.resolved}”` : h('span', { class: 'muted' }, 'unresolved')) },
      ],
      rows: shown,
      rowProps: (r) => ({ dataset: { meter: r.meter_id } }),
      stack: true,
    }),
  );
}

// The data-integrity anomaly rules, as /v1/data-quality counts them (meters per rule).
const INTEGRITY = [
  ['duplicate_timestamps', 'Meters with duplicate timestamps'],
  ['conflicting_duplicates', 'Meters with conflicting duplicates'],
  ['missing_values', 'Meters with blank kWh or voltage readings'],
  ['gaps', 'Meters with missing readings'],
  ['register_decrease', 'Meters whose register went down'],
];

function renderReadings([data, duplicates], el) {
  el.append(h('ul', { class: 'rules', style: { 'margin-bottom': '14px' } },
    h('li', null, 'Meters with cached readings', h('span', { class: 'count' }, num(data.readings.meters_cached))),
    INTEGRITY.map(([rule, label]) => h('li', null, label, h('span', { class: 'count' }, num(data.readings[rule]))))));
  const rows = duplicates.meters.map((m) => ({ ...m, finding: m.anomalies.find((a) => a.rule === 'duplicate_timestamps') }));
  if (!rows.length) {
    el.append(empty('No meter sent the same timestamp twice.'));
    return;
  }
  el.append(
    h('p', { class: 'small muted', style: { 'margin-bottom': '8px' } },
      'These meters sent the same timestamp more than once. The API merges such rows field by field (a present value beats a blank one) and flags the reading.'),
    dataTable({
      caption: 'Meters with duplicate timestamps',
      columns: [
        { label: 'Meter', cell: (m) => meterLink(m.meter_id) },
        { label: 'Transformer', cell: (m) => m.transformer_code },
        { label: 'Repeated at', cell: (m) => (m.finding?.first_at ? dateTime(m.finding.first_at) : '—') },
      ],
      rows,
      rowProps: (m) => ({ dataset: { meter: m.meter_id } }),
    }),
  );
}
