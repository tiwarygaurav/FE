// Talking to the Urja Meter API: same-origin JSON, RFC 9457 problems, optional API key.
// No DOM here; the key prompt is plugged in by the app (see setKeyPrompt).

import { istDay } from './format.js';

const KEY_STORAGE = 'urja.apiKey';
const MINUTE = 60_000;

export class ApiError extends Error {
  constructor(status, problem = {}, retryAfter = null) {
    super(problem.detail || problem.title || `The API answered with status ${status}.`);
    this.name = 'ApiError';
    this.status = status;
    this.code = problem.code ?? null;
    this.errors = Array.isArray(problem.errors) ? problem.errors : [];
    this.retryAfter = retryAfter;
  }
}

/** What an HTTP header can carry (and the server accepts): printable ASCII, no spaces. */
export const isSendableKey = (key) => /^[\x21-\x7E]+$/.test(key ?? '');

function loadKey() {
  try {
    const key = localStorage.getItem(KEY_STORAGE);
    return isSendableKey(key) ? key : null; // a key fetch() would refuse to send is no key
  } catch {
    return null;
  }
}

function saveKey(key) {
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    // Storage unavailable (private mode, blocked site data): the key lives for this page only.
  }
}

let apiKey = loadKey();
let promptForKey = async () => null;
let pendingPrompt = null;
let declined = false;
const keyListeners = new Set();

export const hasKey = () => Boolean(apiKey);

/** `prompt({ rejected })` resolves to a key, or to null when the user cancels. */
export function setKeyPrompt(prompt) {
  promptForKey = prompt;
}

/** Call `listener` whenever the user enters a key, whichever request or button asked for it. */
export function onKey(listener) {
  keyListeners.add(listener);
}

/** Ask the user for a key. Concurrent 401s share one prompt; after a cancel we stop asking
 *  until the user explicitly asks again (`force`). */
export function requestKey({ rejected = false, force = false } = {}) {
  if (declined && !force) return Promise.resolve(null);
  pendingPrompt ??= promptForKey({ rejected }).then((key) => {
    pendingPrompt = null;
    declined = !key;
    if (key) {
      apiKey = key;
      saveKey(key);
      for (const listener of keyListeners) listener();
    }
    return key || null;
  });
  return pendingPrompt;
}

export function query(params) {
  const search = new URLSearchParams();
  const entries = params instanceof URLSearchParams ? params.entries() : Object.entries(params ?? {});
  for (const [name, value] of entries) {
    for (const v of Array.isArray(value) ? value : [value]) {
      if (v != null && v !== '') search.append(name, String(v));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

async function send(url, key, signal) {
  const headers = { Accept: 'application/json' };
  if (key) headers['X-API-Key'] = key;
  try {
    return await fetch(url, { headers, signal });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, { code: 'network', detail: 'The server did not answer. Check that it is running, then retry.' });
  }
}

async function problem(res) {
  let body = {};
  try {
    body = await res.json();
  } catch {
    // Not JSON, e.g. a proxy's error page: fall back to the status alone.
  }
  const retryAfter = Number.parseInt(res.headers.get('Retry-After') ?? '', 10);
  return new ApiError(res.status, body, Number.isFinite(retryAfter) ? retryAfter : null);
}

async function request(url, signal) {
  const key = apiKey;
  let res = await send(url, key, signal);
  if (res.status === 401) {
    // Another request may have obtained a key while this one was in flight.
    const next = apiKey !== key ? apiKey : await requestKey({ rejected: Boolean(key) });
    if (next) res = await send(url, next, signal);
  }
  if (!res.ok) throw await problem(res);
  return res.json();
}

const cache = new Map();

/** GET a JSON resource. With `ttl` (ms) the response is shared between callers and reused;
 *  shared requests ignore `signal` (callers simply drop results they no longer need). */
export function get(path, params, { signal, ttl = 0 } = {}) {
  const url = path + query(params);
  if (!ttl) return request(url, signal);
  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < ttl) return hit.promise;
  const promise = request(url);
  cache.set(url, { at: Date.now(), promise });
  promise.catch(() => {
    if (cache.get(url)?.promise === promise) cache.delete(url);
  });
  return promise;
}

function memo(load, ttl) {
  let at = 0;
  let value = null;
  return () => {
    if (!value || Date.now() - at > ttl) {
      at = Date.now();
      value = load();
      value.catch(() => {
        value = null;
      });
    }
    return value;
  };
}

// ----------------------------------------------------------------------------- shared resources

export const status = () => get('/v1/status', null, { ttl: 5_000 });
export const transformers = () => get('/v1/transformers', { limit: 500 }, { ttl: 5 * MINUTE });
export const dataQuality = () => get('/v1/data-quality', null, { ttl: 5 * MINUTE });
export const networkOverview = () => get('/v1/network', null, { ttl: 5 * MINUTE });
export const networkLevel = (level) => get(`/v1/network/${level}`, null, { ttl: 5 * MINUTE });
export const anomalyReport = (params) => get('/v1/insights/anomalies', params, { ttl: 5 * MINUTE });
/** Meters per rule only, without the per-meter list. */
export const anomalyCounts = (params) => anomalyReport({ ...params, include_meters: false });
export const consumption = (params) => get('/v1/insights/consumption', params, { ttl: 5 * MINUTE });

/** Every meter; the list endpoint returns at most 500 per page. */
export const allMeters = memo(async () => {
  const items = [];
  for (let offset = 0; ; offset += 500) {
    const page = await get('/v1/meters', { limit: 500, offset });
    items.push(...page.items);
    if (!page.items.length || items.length >= page.total) return items;
  }
}, 5 * MINUTE);

/** The IST days a set of readings spans, as `{ first, last }` (YYYY-MM-DD), from anything with
 *  `first_reading_at`/`last_reading_at` (a meter's `readings_coverage`, the status' `readings_cache`);
 *  null when there are none. The portal's data is historical (it ends on 2026-06-30), so query
 *  windows follow the data, not today's date. */
export function readingsSpan(coverage) {
  const last = coverage?.last_reading_at;
  return last ? { first: istDay(coverage.first_reading_at ?? last), last: istDay(last) } : null;
}

/** Why the index isn't ready, from /v1/status' `reference_data`: still building, or failed and why. */
export function firstSyncState(reference) {
  const run = reference?.last_run;
  return run?.status === 'failed'
    ? `The first sync with the portal failed (${run.error}). It is retried automatically.`
    : 'The local index is still being built from the portal.';
}

/** The span of every cached reading (see readingsSpan); null until the first ones are cached. */
export async function fleetSpan() {
  const { index_ready: ready, readings_cache: cache, reference_data: reference } = await status();
  if (!ready) {
    throw new ApiError(503, { code: 'index_not_ready', detail: firstSyncState(reference) }, 10);
  }
  return readingsSpan(cache);
}
