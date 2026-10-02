// Map: every meter at its reported location, coloured by status, plus a radius search.

import * as api from '../api.js';
import { STATUSES, STATUS_LABEL, num, plural } from '../format.js';
import { openMeter, setParams } from '../router.js';
import { Region, dataTable, debounce, empty, h, icon, loadScript, loadStyle, meterLink, statusBadge } from '../ui.js';

const LEAFLET_CSS = ['https://unpkg.com/leaflet@1.9.4/dist/leaflet.css', 'sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H'];
const LEAFLET_JS = ['https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', 'sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH'];
// The slider's stops: fine around the default, coarse up to 20 km, which reaches across the
// fleet (every meter is within about 17 km of its centre). A link's radius snaps to the nearest
// stop, so the URL, the slider, its label and the search always agree.
const RADII = [0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 7.5, 10, 15, 20];
const DEFAULT_RADIUS = 1;
const snapRadius = (km) => {
  const target = Math.min(km, RADII.at(-1)); // Infinity (radius=1e400) is equally far from every stop
  return RADII.reduce((best, r) => (Math.abs(r - target) < Math.abs(best - target) ? r : best));
};
const radiusText = (km) => `${num(km, Number.isInteger(km) ? 0 : Number.isInteger(Math.round(km * 100) / 10) ? 1 : 2)} km`;

let els;
let map = null;
let markerLayer;
let searchLayer;
let meters = [];
const markers = new Map();
let hits = new Set();
let state = { status: [], make: '', near: null, radius: DEFAULT_RADIUS };
let lastKey = null;
let started = false;
let fitNext = false;
let pickedOnMap = false; // true while a map click is being routed (synchronously): that point is already in view
let nearRegion;

export function mount(el) {
  els = {
    statusBoxes: h('fieldset', { class: 'check-group' }, h('legend', null, 'Status')),
    make: h('select', { onchange: () => apply() }, h('option', { value: '' }, 'Any make')),
    count: h('p', { class: 'small muted', 'aria-live': 'polite' }),
    mapHost: h('div'),
    // PROTOCOL.md (data quality) shows the coordinates are uniform noise around Jaipur.
    note: h('p', { class: 'note' }, icon('info'), 'The portal’s coordinates look synthetic: meters sit no closer to their own transformer than to any other. Use positions for rough orientation only.'),
    radius: h('input', { type: 'range', min: '0', max: String(RADII.length - 1), step: '1', 'aria-label': 'Search radius' }),
    radiusOut: h('output'),
    results: h('div', { class: 'near-results' }),
  };
  showRadius(DEFAULT_RADIUS);
  const locate = h('button', { type: 'button', class: 'btn small', onclick: useMyLocation, disabled: !('geolocation' in navigator) }, icon('locate'), 'Use my location');
  const clear = h('button', { type: 'button', class: 'btn small ghost', onclick: () => apply({ near: '' }) }, 'Clear');
  const commitRadius = debounce(() => apply(), 250);
  els.radius.addEventListener('input', () => {
    showRadius(sliderRadius());
    commitRadius();
  });

  el.append(
    h('header', { class: 'view-head' }, h('div', null,
      h('h1', { tabindex: '-1' }, 'Map'),
      h('p', null, 'Every meter at its reported location, coloured by status. Select a meter for its details, or click anywhere else on the map to list the meters around that point.'))),
    h('div', { class: 'map-layout' },
      h('section', { class: 'card', 'aria-label': 'Meter map' },
        h('div', { class: 'map-toolbar' }, els.statusBoxes, h('label', { class: 'field' }, 'Make', els.make), els.count),
        els.mapHost,
        h('div', { style: { 'margin-top': '12px' } }, els.note)),
      h('aside', { class: 'card near-panel', 'aria-labelledby': 'near-title' },
        h('div', null, h('h2', { id: 'near-title' }, 'Meters near a point'),
          h('p', { class: 'small muted' }, 'Click the map to drop a search point. Status and make filters apply here too.')),
        h('div', { class: 'near-controls' },
          h('div', { class: 'field' }, 'Radius', h('div', { class: 'range-row' }, els.radius, els.radiusOut)),
          h('div', { class: 'near-actions' }, locate, clear)),
        els.results)),
  );
  nearRegion = new Region(els.results);
}

export function show() {
  map?.invalidateSize();
}

export function update(route) {
  const params = new URLSearchParams(route);
  params.delete('meter');
  const key = params.toString();
  if (key === lastKey) return;
  lastKey = key;
  const raw = params.get('radius');
  const radius = raw && Number(raw) > 0 ? snapRadius(Number(raw)) : DEFAULT_RADIUS;
  if (raw != null && raw !== String(radius)) {
    // Out of range or between stops: rewrite the link to the radius actually searched.
    params.set('radius', radius === DEFAULT_RADIUS ? '' : String(radius));
    setParams(params);
    return;
  }
  const previousNear = state.near;
  const near = params.get('near');
  state = {
    status: params.getAll('status'),
    make: params.get('make') ?? '',
    near: near && /^-?\d+(\.\d+)?,-?\d+(\.\d+)?$/.test(near) ? near : null,
    radius,
  };
  showRadius(radius);
  if (map) {
    // A point from a link, such as a meter's "Show on map", may be far outside the current view.
    fitNext ||= Boolean(state.near) && state.near !== previousNear && !pickedOnMap;
    render();
  } else if (!started) {
    started = true;
    new Region(els.mapHost).load(async () => {
      await Promise.all([loadStyle(...LEAFLET_CSS), loadScript(...LEAFLET_JS)]);
      return api.allMeters();
    }, createMap);
  }
}

function apply(changes = {}) {
  const status = [...els.statusBoxes.querySelectorAll('input:checked')].map((box) => box.value);
  const available = [...els.statusBoxes.querySelectorAll('input')].length;
  const radius = sliderRadius();
  setParams({
    status: status.length === available ? [] : status.length ? status : ['none'],
    make: els.make.value,
    near: state.near,
    radius: radius === DEFAULT_RADIUS ? '' : radius,
    ...changes,
  });
}

const sliderRadius = () => RADII[Number(els.radius.value)] ?? DEFAULT_RADIUS;

function showRadius(km) {
  els.radius.value = String(RADII.indexOf(km));
  els.radius.setAttribute('aria-valuetext', radiusText(km));
  els.radiusOut.textContent = radiusText(km);
}

// ----------------------------------------------------------------------------- map

function createMap(items, host) {
  const { L } = window;
  meters = items.filter((m) => m.location);
  const counts = {};
  for (const m of meters) counts[m.status] = (counts[m.status] ?? 0) + 1;
  els.statusBoxes.append(...STATUSES.filter((s) => counts[s]).map((s) =>
    h('label', { class: 'chip' },
      h('input', { type: 'checkbox', value: s, onchange: () => apply() }),
      h('span', { class: 'swatch', dataset: { tone: s } }), STATUS_LABEL[s], h('span', { class: 'count' }, num(counts[s])))));
  const makes = [...new Set(meters.map((m) => m.make))].sort((a, b) => a.localeCompare(b));
  els.make.append(...makes.map((make) => h('option', { value: make }, make)));

  const mapEl = h('div', { class: 'map', role: 'region', 'aria-label': 'Map of meter locations. The Meters view lists the same meters as a table.' });
  host.append(mapEl);
  map = L.map(mapEl, { zoomSnap: 0.5 });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
  searchLayer = L.layerGroup().addTo(map);
  for (const m of meters) {
    const marker = L.circleMarker([m.location.latitude, m.location.longitude], {
      radius: 6, weight: 1.5, className: `m m-${m.status in STATUS_LABEL ? m.status : 'unknown'}`, bubblingMouseEvents: false,
    });
    marker.bindTooltip(() => h('span', null, h('b', null, m.meter_id), ` · ${STATUS_LABEL[m.status] ?? m.status} · ${m.make} · ${m.transformer_code}`), { direction: 'top', offset: [0, -6] });
    marker.on('click', () => openMeter(m.meter_id));
    markers.set(m.meter_id, marker);
  }
  if (meters.length) map.fitBounds(L.latLngBounds(meters.map((m) => [m.location.latitude, m.location.longitude])), { padding: [20, 20] });
  map.on('click', (event) => {
    pickedOnMap = true;
    apply({ near: `${event.latlng.lat.toFixed(5)},${event.latlng.lng.toFixed(5)}` });
    pickedOnMap = false;
  });
  fitNext = Boolean(state.near);
  render();
}

const shownStatuses = () => (state.status.length ? state.status : STATUSES);
const matches = (m) => shownStatuses().includes(m.status) && (!state.make || m.make === state.make);

function render() {
  for (const box of els.statusBoxes.querySelectorAll('input')) box.checked = shownStatuses().includes(box.value);
  els.make.value = state.make;
  const visible = meters.filter(matches);
  markerLayer.clearLayers();
  for (const m of visible) markerLayer.addLayer(markers.get(m.meter_id));
  els.count.textContent = `Showing ${num(visible.length)} of ${plural(meters.length, 'meter')}`;
  search(fitNext);
  fitNext = false;
}

function highlight() {
  els.mapHost.querySelector('.map')?.classList.toggle('has-search', Boolean(state.near));
  for (const [id, marker] of markers) marker.getElement()?.classList.toggle('hit', hits.has(id));
}

// ----------------------------------------------------------------------------- radius search

function search(fit) {
  const { L } = window;
  searchLayer.clearLayers();
  if (!state.near) {
    hits = new Set();
    highlight();
    nearRegion.load(async () => null, (_, el) => el.append(empty('No search point yet. Click the map, or use your location.')));
    return;
  }
  const [lat, lng] = state.near.split(',').map(Number);
  const area = L.circle([lat, lng], { radius: state.radius * 1000, className: 'search-area', interactive: false }).addTo(searchLayer);
  L.circleMarker([lat, lng], { radius: 5, className: 'search-point', interactive: false }).addTo(searchLayer);
  if (fit) map.fitBounds(area.getBounds(), { padding: [30, 30], maxZoom: 16 });
  highlight();
  const statuses = state.status.length ? state.status : [];
  nearRegion.load(
    (signal) => (statuses.includes('none')
      ? Promise.resolve({ items: [], total: 0 })
      : api.get('/v1/meters', { near: state.near, radius_km: state.radius, status: statuses, make: state.make, limit: 500 }, { signal })),
    (page, el) => renderResults(page, el, lat, lng),
  );
}

function renderResults(page, el, lat, lng) {
  hits = new Set(page.items.map((m) => m.meter_id));
  highlight();
  el.append(h('p', { class: 'small', 'aria-live': 'polite' },
    h('b', null, plural(page.total, 'meter')), ` within ${radiusText(state.radius)} of ${num(lat, 4)}, ${num(lng, 4)}`));
  if (!page.items.length) {
    const nearest = meters.filter(matches)
      .map((m) => [m, distanceKm(lat, lng, m.location.latitude, m.location.longitude)])
      .sort((a, b) => a[1] - b[1])[0];
    el.append(empty(nearest
      ? `The nearest matching meter, ${nearest[0].meter_id}, is ${num(nearest[1], 1)} km away. Widen the radius or pick another point.`
      : 'No meters match the current filters.'));
    return;
  }
  el.append(h('div', { class: 'near-scroll' }, dataTable({
    caption: 'Meters near the search point',
    columns: [
      { label: 'Meter', cell: (m) => [meterLink(m.meter_id), h('span', { class: 'sub' }, m.transformer_code)] },
      { label: 'Status', cell: (m) => statusBadge(m.status) },
      { label: 'km', num: true, cell: (m) => num(m.distance_km, 2) },
    ],
    rows: page.items,
    rowProps: (m) => ({ dataset: { meter: m.meter_id } }),
  })));
  if (page.total > page.items.length) el.append(h('p', { class: 'small muted' }, `Showing the nearest ${num(page.items.length)}.`));
}

function useMyLocation() {
  navigator.geolocation.getCurrentPosition(
    (position) => {
      fitNext = true;
      apply({ near: `${position.coords.latitude.toFixed(5)},${position.coords.longitude.toFixed(5)}` });
    },
    (error) => nearRegion.load(async () => null, (_, el) => el.append(empty(`Your location is not available (${error.message || 'permission denied'}).`))),
    { timeout: 10_000 },
  );
}

// The same sphere as the API's haversine, for "the nearest meter is N km away" hints.
const EARTH_KM = 6371.0088;

function distanceKm(lat1, lng1, lat2, lng2) {
  const rad = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * rad) / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lng2 - lng1) * rad) / 2) ** 2;
  return 2 * EARTH_KM * Math.asin(Math.min(1, Math.sqrt(a)));
}
