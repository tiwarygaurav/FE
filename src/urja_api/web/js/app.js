// Boot: routing between views, the meter drawer, the API-key prompt, theme and data freshness.

import * as api from './api.js';
import { redraw } from './charts.js';
import { ageDays, date, num, plural } from './format.js';
import { emit, navigate, onRoute, openMeter } from './router.js';
import { h, icon } from './ui.js';
import * as mapView from './views/map.js';
import * as meterPanel from './views/meter-panel.js';
import * as meters from './views/meters.js';
import * as network from './views/network.js';
import * as overview from './views/overview.js';
import * as quality from './views/quality.js';
import * as transformers from './views/transformers.js';

const VIEWS = {
  overview: ['Overview', overview],
  map: ['Map', mapView],
  meters: ['Meters', meters],
  network: ['Network', network],
  transformers: ['Transformers', transformers],
  quality: ['Data quality', quality],
};
const mounted = new Set();
let active = null;

function show({ view, params }) {
  if (!VIEWS[view]) {
    navigate('overview', null, { replace: true });
    return;
  }
  const [title, module] = VIEWS[view];
  const section = document.getElementById(`view-${view}`);
  if (!mounted.has(view)) {
    module.mount(section);
    mounted.add(view);
  }
  if (active !== view) {
    for (const el of document.querySelectorAll('.view')) el.hidden = el !== section;
    for (const link of document.querySelectorAll('.nav a')) {
      if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    const nav = document.querySelector('.nav');
    if (nav.scrollWidth > nav.clientWidth) nav.querySelector('[aria-current]')?.scrollIntoView({ inline: 'nearest', block: 'nearest' });
    document.title = `${title} · Urja Meter Ops`;
    if (active) {
      window.scrollTo(0, 0);
      section.querySelector('h1')?.focus();
    }
    active = view;
    module.show?.();
  }
  module.update(params);
  const meter = params.get('meter')?.trim();
  if (meter) meterPanel.show(meter);
  else meterPanel.hide();
}

// Meter links and table rows open the drawer over the current view.
document.addEventListener('click', (event) => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[data-meter]');
  if (link) {
    event.preventDefault();
    openMeter(link.dataset.meter);
    return;
  }
  const row = event.target.closest('tr[data-meter]');
  if (row && !event.target.closest('a, button, input, select, label') && !getSelection()?.toString()) openMeter(row.dataset.meter);
});

// The skip link must not change the hash: the router would read "#main" as a view and go to the overview.
document.querySelector('.skip-link').addEventListener('click', (event) => {
  event.preventDefault();
  (document.querySelector('.view:not([hidden]) h1') ?? document.getElementById('main')).focus();
});

// ----------------------------------------------------------------------------- API key

const keyDialog = document.getElementById('key-dialog');
document.getElementById('key-cancel').addEventListener('click', () => keyDialog.close('cancel'));
const keyError = keyDialog.querySelector('#key-error');
const KEY_REJECTED = keyError.textContent;
const KEY_UNSENDABLE = 'API keys are plain ASCII without spaces. Check for stray or invisible characters and try again.';
api.setKeyPrompt(({ rejected }) => new Promise((resolve) => {
  const input = keyDialog.querySelector('#key-input');
  const ask = (message) => {
    keyError.textContent = message ?? '';
    keyError.hidden = !message;
    input.value = '';
    keyDialog.returnValue = '';
    keyDialog.addEventListener('close', () => {
      const key = keyDialog.returnValue === 'save' ? input.value.trim() : '';
      if (!key) resolve(null);
      else if (!api.isSendableKey(key)) ask(KEY_UNSENDABLE); // fetch() would refuse to send it at all
      else resolve(key);
    }, { once: true });
    keyDialog.showModal();
  };
  ask(rejected ? KEY_REJECTED : null);
}));

// ----------------------------------------------------------------------------- theme

const THEMES = ['auto', 'light', 'dark'];
const THEME_NAMES = { auto: 'system', light: 'light', dark: 'dark' };
const themeButton = document.getElementById('theme-toggle');

function storedTheme() {
  try {
    return THEMES.includes(localStorage.getItem('urja.theme')) ? localStorage.getItem('urja.theme') : 'auto';
  } catch {
    return 'auto';
  }
}

let theme = storedTheme();
function applyTheme() {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  themeButton.replaceChildren(icon(`theme-${theme}`));
  themeButton.setAttribute('aria-label', `Theme: ${THEME_NAMES[theme]}. Switch to ${THEME_NAMES[next]}`);
  themeButton.title = `Theme: ${THEME_NAMES[theme]}`;
  redraw();
}
themeButton.addEventListener('click', () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try {
    localStorage.setItem('urja.theme', theme);
  } catch {
    // Not persisted; the choice still applies to this page.
  }
  applyTheme();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => theme === 'auto' && redraw());

// ----------------------------------------------------------------------------- freshness pill

const pill = document.getElementById('freshness');
let pillState = 'idle'; // loading | ok | failed
let pillTimer = null;

/** Polls while the API is still syncing or fetching readings. After an error it hides and tries
 *  again a minute later, on the next navigation, or once an API key is entered. */
async function freshness() {
  if (pillState === 'loading') return;
  pillState = 'loading';
  clearTimeout(pillTimer);
  let again = null;
  try {
    const { index_ready: ready, readings_cache: cache, reference_data: reference } = await api.status();
    const last = cache.last_reading_at;
    const warming = cache.warmup.state === 'running';
    if (!ready || !last) {
      const failed = !ready && reference.last_run?.status === 'failed';
      pill.replaceChildren(
        h('strong', null, icon(failed ? 'warning' : 'clock'), failed ? 'First sync failed' : 'Syncing with the portal'),
        h('span', null, ready ? 'Readings are still being fetched.' : failed ? 'It is retried automatically.' : 'The first sync is still running.'));
    } else {
      pill.replaceChildren(
        h('strong', null, icon('clock'), `Data ends ${date(last)}`),
        h('span', null, warming
          ? `Fetching readings: ${num(cache.warmup.done)} of ${plural(cache.warmup.total, 'meter')}`
          : `Latest reading is ${plural(Math.floor(ageDays(last)), 'day')} old`));
    }
    pill.hidden = false;
    pillState = 'ok';
    if (!ready || !last || warming) again = 15_000;
  } catch (error) {
    pill.hidden = true;
    pillState = 'failed';
    // Not after a 401: only a new key (onKey) can fix that, and a timed retry would pop up the key prompt.
    if (error?.status !== 401) again = 60_000;
  }
  if (again) pillTimer = setTimeout(freshness, again);
}

const retryPill = () => pillState === 'failed' && freshness();

applyTheme();
onRoute(show);
onRoute(retryPill);
api.onKey(retryPill);
emit();
freshness();
