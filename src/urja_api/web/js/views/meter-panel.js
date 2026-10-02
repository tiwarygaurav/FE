// The meter drawer: everything about one meter, opened over any view (#/...?meter=ID).

import * as api from '../api.js';
import { baseOptions, chartFigure, crosshair, draw } from '../charts.js';
import {
  INSTALL_LABEL, ISSUE_INFO, LEVELS, LEVEL_LABEL, PHASE_LABEL, addDays, age, ageDays, ago, clock, date, dateTime, dayMonth,
  dayRange, intervalLabel, issueLabel, istDay, kwh, num, pct, plural, ruleLabel, weekday,
} from '../format.js';
import { closeMeter, href } from '../router.js';
import {
  Region, badge, card, dataTable, empty, h, icon, legend, mixLabel, nodeHref, problemView, severityBadge, statusBadge,
} from '../ui.js';

const SLOW = 'Fetching this meter’s readings from the portal. When the cache has expired this can take up to half a minute.';
const TTL = 60_000;
const dialog = document.getElementById('meter-drawer');
let currentId = null;

dialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeMeter();
});
dialog.addEventListener('click', (event) => {
  const box = dialog.getBoundingClientRect();
  const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
  if (event.target === dialog && outside) closeMeter();
});

export function show(id) {
  if (id === currentId && dialog.open) return;
  currentId = id;
  render(id);
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  dialog.querySelector('#drawer-title').focus();
}

export function hide() {
  currentId = null;
  if (dialog.open) dialog.close();
}

const alpha = (color, a) => (/^#[0-9a-f]{6}$/i.test(color) ? `${color}${Math.round(a * 255).toString(16).padStart(2, '0')}` : color);
const meterUrl = (id, part = '') => `/v1/meters/${encodeURIComponent(id)}${part}`;

function render(id) {
  const title = h('h2', { id: 'drawer-title', tabindex: '-1' }, id);
  const subtitle = h('p', { class: 'muted' }, 'Loading…');
  const parts = Object.fromEntries(['where', 'facts', 'anomalies', 'daily', 'details'].map((name) => [name, h('div')]));
  const profileSlot = h('div', { hidden: true });
  const body = h('div', { class: 'drawer-body' },
    card({ title: 'Network position', level: 'h3' }, parts.where),
    card({ title: 'Readings', level: 'h3' }, parts.facts),
    card({ title: 'Anomalies', level: 'h3' }, parts.anomalies),
    h('section', { class: 'card', 'aria-label': 'Daily consumption' }, parts.daily),
    profileSlot,
    card({ title: 'Nameplate and location', level: 'h3' }, parts.details));
  dialog.replaceChildren(
    h('div', { class: 'drawer-head' },
      h('div', { class: 'drawer-title' }, title, subtitle),
      h('button', { type: 'button', class: 'btn ghost icon-only', 'aria-label': 'Close meter details', onclick: closeMeter }, icon('close'))),
    body,
  );

  // Fetchers call the API afresh so Retry works; the TTL cache shares concurrent calls.
  const meter = () => api.get(meterUrl(id), null, { ttl: TTL });
  // The days this meter's cached readings span, or the fleet's until its own are cached.
  const span = async () => api.readingsSpan((await meter().catch(() => null))?.readings_coverage) ?? api.fleetSpan().catch(() => null);
  // The last 30 days of that span; without one, the API's default (the meter's last 7 days).
  const month = async () => {
    const s = await span();
    return s && { from: maxDay(s.first, addDays(s.last, -29)), to: s.last };
  };
  const current = () => currentId === id;

  meter().catch((error) => {
    if (!current()) return;
    subtitle.textContent = 'This meter could not be loaded.';
    if (error.status === 404) body.replaceChildren(card({}, problemView(error)));
  });
  const showHeader = (m) => {
    title.replaceChildren(m.meter_id, statusBadge(m.status));
    subtitle.textContent = `${m.make} · ${PHASE_LABEL[m.phase] ?? m.phase} · ${INSTALL_LABEL[m.installation_type] ?? m.installation_type} · serial ${m.serial_number}`;
  };
  // The hourly profile needs half-hourly readings. The meter record says which interval it
  // has once its readings are cached; before that, the readings response decides.
  let profileDecided = false;
  const offerProfile = (interval, days) => {
    if (profileDecided || !current()) return;
    profileDecided = true;
    if (!interval || interval > 30 || !days) return;
    profileSlot.hidden = false;
    profileSlot.replaceChildren(loadProfile(id, days));
  };
  meter().then((m) => m.readings_coverage && offerProfile(m.readings_coverage.interval_minutes, api.readingsSpan(m.readings_coverage)), () => {});

  new Region(parts.where).load(() => Promise.all([meter(), api.transformers().catch(() => null)]), (data, el) => {
    showHeader(data[0]);
    renderWhere(data, el);
  });
  new Region(parts.details).load(meter, renderDetails);
  new Region(parts.facts, { slow: SLOW }).load(async () => api.get(meterUrl(id, '/readings'), await month(), { ttl: TTL }), (r, el) => {
    renderFacts(r, el);
    const first = r.readings[0]?.timestamp ?? r.last_reading_at;
    if (first) offerProfile(r.summary.interval_minutes, { first: istDay(first), last: istDay(r.last_reading_at ?? first) });
  });
  new Region(parts.anomalies, { slow: SLOW }).load(() => api.get(meterUrl(id, '/anomalies'), null, { ttl: TTL }), renderAnomalies);
  new Region(parts.daily, { slow: SLOW }).load(
    async () => api.get(meterUrl(id, '/consumption'), { granularity: 'day', ...(await month()) }, { ttl: TTL }),
    renderDaily,
  );
}

const maxDay = (a, b) => (a > b ? a : b);

// ----------------------------------------------------------------------------- sections

function renderWhere([meter, transformers], el) {
  const issuesAt = (level) => meter.data_issues.filter((issue) => issue.level === level);
  el.append(h('nav', { 'aria-label': 'Network path' }, h('ol', { class: 'path' }, LEVELS.map((level) => {
    const node = meter.network[level];
    return h('li', null,
      h('a', { href: nodeHref(level, node.code), title: `${LEVEL_LABEL[level]} ${node.code}` },
        h('span', { class: 'sr-only' }, `${LEVEL_LABEL[level]}: `), node.name ?? node.code, ' ', h('span', { class: 'code' }, node.code)),
      issuesAt(level).map((issue) => badge(issueLabel(issue.code), 'warning', 'warning')));
  }))));

  const dt = meter.transformer;
  const siblings = transformers?.items.find((t) => t.code === meter.transformer_code);
  el.append(h('p', { class: 'small muted', style: { 'margin-top': '10px' } },
    `Transformer ${dt?.name ?? meter.transformer_code}`,
    dt?.capacity_kva != null ? `, rated ${num(dt.capacity_kva)} kVA` : ', capacity unknown',
    siblings ? `, serves ${plural(siblings.meter_count, 'meter')} (${mixLabel(siblings.meters_by_status)}). ` : '. ',
    h('a', { href: href('meters', { transformer: meter.transformer_code }) }, 'List them')));

  if (meter.data_issues.length) {
    el.append(h('ul', { class: 'issue-list' }, meter.data_issues.map((issue) => h('li', { class: 'callout', dataset: { tone: 'warning' } },
      icon('warning'),
      h('span', null,
        h('b', null, `${issueLabel(issue.code)}${issue.level ? ` (${issue.level})` : ''}. `),
        issue.reported != null ? `Portal reported “${issue.reported}”` : 'Portal reported nothing',
        issue.resolved != null ? `; this API serves “${issue.resolved}”. ` : '. ',
        h('span', { class: 'muted' }, ISSUE_INFO[issue.code]?.[1] ?? issue.message))))));
  }
}

function renderFacts(readings, el) {
  const s = readings.summary;
  const last = readings.last_reading_at;
  const list = readings.readings;
  const spanDays = list.length > 1 ? (Date.parse(list.at(-1).timestamp) - Date.parse(list[0].timestamp)) / 86_400_000 : 0;
  const flags = Object.entries(s.flags);
  const stale = last && ageDays(last) > 2;
  const fact = (label, value, note) => h('div', { class: 'fact' }, h('dt', null, label), h('dd', null, value, note && h('small', null, ' ', note)));

  el.append(h('dl', { class: 'facts' },
    fact('Latest reading', last ? date(last) : '—', last && clock(last)),
    fact(list.length ? `Used ${dayRange(istDay(list[0].timestamp), istDay(list.at(-1).timestamp))}` : 'Used in window', kwh(s.consumption_kwh)),
    fact('Average per day', spanDays >= 1 && s.consumption_kwh != null ? kwh(s.consumption_kwh / spanDays) : '—'),
    fact('Power factor', s.average_power_factor != null ? num(s.average_power_factor, 3) : '—'),
    fact('Voltage', s.min_voltage_v != null ? `${num(s.min_voltage_v)}–${num(s.max_voltage_v)} V` : '—'),
    fact('Interval', intervalLabel(s.interval_minutes), `${plural(s.count, 'reading')}${s.missing_intervals ? `, ${num(s.missing_intervals)} missing` : ''}`)));

  const notes = h('div', { class: 'grid', style: { 'margin-top': '14px', gap: '8px' } });
  if (stale) {
    notes.append(h('div', { class: 'callout', dataset: { tone: 'warning' } }, icon('clock'),
      h('span', null, `Stale: the latest reading is ${age(ageDays(last))} old. The portal has no newer data for this meter.`)));
  }
  if (flags.length) {
    notes.append(h('div', { class: 'callout' }, icon('info'),
      h('span', null, `Reading flags in this window: ${flags.map(([flag, n]) => `${flag.replaceAll('_', ' ')} × ${num(n)}`).join(', ')}.`)));
  }
  if (s.interval_minutes === 1440) {
    notes.append(h('p', { class: 'note' }, icon('info'), 'This meter reports once a day, at midnight, so there is no intraday load profile.'));
  }
  const fetched = readings.freshness.fetched_at;
  notes.append(h('p', { class: 'note' }, icon('refresh'),
    h('span', null, fetched ? `Fetched from the portal ${ago(fetched)}. ` : '',
      readings.freshness.stale && badge('Cached copy: the portal could not be reached', 'warning', 'warning'))));
  el.append(notes);
}

function renderAnomalies(report, el) {
  if (!report.anomalies.length) {
    el.append(empty('No anomalies detected in this meter’s readings.'));
    return;
  }
  el.append(h('ul', { class: 'anomaly-list' }, report.anomalies.map((a) => h('li', null,
    h('span', null, severityBadge(a.severity), ' ', h('b', null, ruleLabel(a.rule))),
    h('span', null, a.message),
    h('span', { class: 'meta' },
      `${plural(a.occurrences, 'occurrence')}`,
      a.first_at && (a.first_at === a.last_at ? `, ${dateTime(a.first_at)}` : `, ${dateTime(a.first_at)} to ${dateTime(a.last_at)}`))))));
}

function renderDaily(consumption, el) {
  const { buckets } = consumption;
  if (!buckets.length) {
    el.append(h('h3', null, 'Daily consumption'), empty('No readings in this window.'));
    return;
  }
  const range = dayRange(istDay(buckets[0].start), istDay(buckets.at(-1).start));
  const partial = buckets.filter((b) => !b.complete && b.consumption_kwh != null).length;
  const missing = buckets.filter((b) => b.consumption_kwh == null).map((b) => dayMonth(b.start));
  const notes = [`${kwh(consumption.total_kwh)} over ${range}. Each day is the register difference between two midnights (IST).`];
  if (partial) notes.push('Hollow bars are incomplete days, without a reading at both midnights.');
  if (missing.length) notes.push(`No value for ${missing.length > 3 ? `${num(missing.length)} days` : missing.join(', ')}: no reading at the closing midnight.`);
  const table = dataTable({
    caption: 'Daily consumption',
    columns: [
      { label: 'Day', cell: (b) => weekday(b.start) },
      { label: 'kWh', num: true, cell: (b) => kwh(b.consumption_kwh, { unit: false }) },
      { label: 'Power factor', num: true, cell: (b) => (b.power_factor != null ? num(b.power_factor, 3) : '—') },
      { label: 'Coverage', num: true, cell: (b) => pct(b.coverage, 0) },
      { label: 'Complete', cell: (b) => (b.complete ? 'Yes' : 'No') },
    ],
    rows: buckets,
  });
  const { figure, canvas } = chartFigure({
    title: 'Daily consumption',
    label: `Daily consumption, ${range}. Total ${kwh(consumption.total_kwh)}.`,
    legendEl: partial ? legend([{ label: 'Complete day', tone: 'series' }, { label: 'Incomplete day', tone: 'series', hollow: true }]) : null,
    table,
    caption: notes.join(' '),
  });
  el.append(figure);
  draw(canvas, (t) => {
    const options = baseOptions(t, {
      yTitle: 'kWh',
      tooltip: {
        title: (items) => weekday(buckets[items[0].dataIndex].start),
        label: (item) => kwh(item.raw),
        footer: (items) => {
          const b = buckets[items[0].dataIndex];
          if (!b.complete) return `Incomplete: ${pct(b.coverage, 0)} of the day covered`;
          return b.power_factor != null ? `Power factor ${num(b.power_factor, 3)}` : '';
        },
      },
    });
    return {
      type: 'bar',
      data: {
        labels: buckets.map((b) => dayMonth(b.start)),
        datasets: [{
          label: 'Consumption',
          data: buckets.map((b) => b.consumption_kwh),
          backgroundColor: buckets.map((b) => (b.complete ? t.series : alpha(t.series, 0.28))),
          borderColor: t.series,
          borderWidth: buckets.map((b) => (b.complete ? 0 : 1.5)),
          borderRadius: 4,
          borderSkipped: 'start',
          maxBarThickness: 24,
          categoryPercentage: 0.85,
        }],
      },
      options,
    };
  });
}

// ----------------------------------------------------------------------------- load profile

/** `first`/`last`: the days with readings. The last one is rarely complete (that needs a
 *  reading at the following midnight), so the profile opens on the day before it. */
function loadProfile(id, { first, last }) {
  let day = maxDay(first, addDays(last, -1));
  const input = h('input', { type: 'date', min: first, max: last, value: day, 'aria-label': 'Day to show' });
  const prev = h('button', { type: 'button', class: 'btn small icon-only', 'aria-label': 'Previous day' }, icon('back'));
  const next = h('button', { type: 'button', class: 'btn small icon-only', 'aria-label': 'Next day' }, icon('chevron'));
  const body = h('div');
  const region = new Region(body, { slow: SLOW });

  const show = (value) => {
    if (!value || (first && value < first) || (last && value > last)) return;
    day = value;
    input.value = day;
    prev.disabled = Boolean(first) && day <= first;
    next.disabled = Boolean(last) && day >= last;
    region.load(() => api.get(meterUrl(id, '/consumption'), { granularity: 'hour', from: day, to: day }, { ttl: TTL }), renderProfile);
  };
  input.addEventListener('change', () => show(input.value));
  prev.addEventListener('click', () => show(addDays(day, -1)));
  next.addEventListener('click', () => show(addDays(day, 1)));
  show(day);

  return card(
    { title: 'Hourly load profile', level: 'h3', actions: h('div', { class: 'day-picker' }, prev, input, next) },
    body,
  );
}

function renderProfile(consumption, el) {
  const { buckets } = consumption;
  if (!buckets.length || buckets.every((b) => b.consumption_kwh == null)) {
    el.append(empty('No readings on this day.'));
    return;
  }
  const complete = buckets.filter((b) => b.complete && b.consumption_kwh != null).map((b) => b.consumption_kwh);
  const mean = complete.reduce((sum, v) => sum + v, 0) / (complete.length || 1);
  const spread = complete.length ? Math.max(...complete.map((v) => Math.abs(v - mean))) / (mean || 1) : 0;
  const incomplete = buckets.some((b) => !b.complete);
  const table = dataTable({
    caption: 'Hourly consumption',
    columns: [
      { label: 'Hour', cell: (b) => `${clock(b.start)}–${clock(b.end)}` },
      { label: 'kWh', num: true, cell: (b) => kwh(b.consumption_kwh, { unit: false }) },
      { label: 'Coverage', num: true, cell: (b) => pct(b.coverage, 0) },
      { label: 'Complete', cell: (b) => (b.complete ? 'Yes' : 'No') },
    ],
    rows: buckets,
  });
  const notes = [`${kwh(consumption.total_kwh)} on ${weekday(buckets[0].start)}. kWh in an hour equals the average load in kW.`];
  if (incomplete) notes.push('Hollow points are hours without a reading at both ends.');
  if (complete.length >= 12 && spread < 0.1) {
    notes.push(`The profile is flat: every hour is within ±${pct(spread, 0)} of ${kwh(mean)}, with no evening peak. Real household demand is rarely this even.`);
  }
  const { figure, canvas } = chartFigure({ label: `Hourly consumption on ${weekday(buckets[0].start)}`, table, caption: notes.join(' ') });
  el.append(figure);
  draw(canvas, (t) => {
    const options = baseOptions(t, {
      yTitle: 'kWh per hour',
      tooltip: {
        title: (items) => {
          const b = buckets[items[0].dataIndex];
          return `${clock(b.start)}–${clock(b.end)}`;
        },
        label: (item) => kwh(item.raw),
        footer: (items) => {
          const b = buckets[items[0].dataIndex];
          return b.complete ? '' : `Incomplete: ${pct(b.coverage, 0)} of the hour covered`;
        },
      },
    });
    options.plugins.crosshair = { color: t.text3 };
    return {
      type: 'line',
      data: {
        labels: buckets.map((b) => clock(b.start)),
        datasets: [{
          label: 'Consumption',
          data: buckets.map((b) => b.consumption_kwh),
          borderColor: t.series,
          backgroundColor: alpha(t.series, 0.1),
          fill: 'origin',
          borderWidth: 2,
          pointRadius: buckets.map((b) => (b.complete ? 0 : 4)),
          pointHoverRadius: 5,
          pointBackgroundColor: buckets.map((b) => (b.complete ? t.series : t.surface)),
          pointBorderColor: t.series,
          pointBorderWidth: 2,
          segment: { borderDash: (ctx) => (buckets[ctx.p1DataIndex]?.complete ? undefined : [4, 3]) },
        }],
      },
      options,
      plugins: [crosshair],
    };
  });
}

// ----------------------------------------------------------------------------- nameplate

function renderDetails(meter, el) {
  const loc = meter.location;
  const row = (label, ...value) => [h('dt', null, label), h('dd', null, ...value)];
  el.append(h('dl', { class: 'plain' },
    row('Meter ID', meter.meter_id),
    row('Serial number', meter.serial_number),
    row('Make', meter.make),
    row('Phase', PHASE_LABEL[meter.phase] ?? meter.phase),
    row('Installation', INSTALL_LABEL[meter.installation_type] ?? meter.installation_type),
    row('Status', statusBadge(meter.status)),
    row('Transformer', `${meter.transformer?.name ?? ''} (${meter.transformer_code})`,
      meter.transformer?.capacity_kva != null ? `, ${num(meter.transformer.capacity_kva)} kVA` : ''),
    row('Feeder', meter.feeder_code),
    row('Location', loc
      ? [`${num(loc.latitude, 5)}, ${num(loc.longitude, 5)} `, h('a', { href: href('map', { near: `${loc.latitude},${loc.longitude}`, radius: 0.5 }) }, 'Show on map')]
      : 'Not reported'),
    row('Record synced', `${dateTime(meter.synced_at)} (${ago(meter.synced_at)})`)));
  if (loc) {
    el.append(h('p', { class: 'note', style: { 'margin-top': '10px' } }, icon('pin'),
      'Coordinates are as reported by the portal and look synthetic (see the Map view); treat them as approximate.'));
  }
}

