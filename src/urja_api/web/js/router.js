// Hash routing: #/<view>?<params>. A `meter` param opens the meter drawer over any view,
// so every state (filters, selected node, open meter) is a shareable link.

import { query } from './api.js';

const listeners = new Set();
let lastHash = null;

export function parse(hash = location.hash) {
  const [path, search = ''] = hash.replace(/^#\/?/, '').split('?');
  return { view: path || 'overview', params: new URLSearchParams(search) };
}

export const href = (view, params) => `#/${view}${query(params)}`;

export function onRoute(listener) {
  listeners.add(listener);
}

export function emit() {
  if (location.hash === lastHash) return;
  lastHash = location.hash;
  const route = parse();
  for (const listener of listeners) listener(route);
}

export function navigate(view, params, { replace = false, state = null } = {}) {
  const url = href(view, params);
  if (url === location.hash) return;
  history[replace ? 'replaceState' : 'pushState'](state, '', url);
  emit();
}

/** Replace the current view's params, keeping an open drawer. */
export function setParams(params, { replace = true } = {}) {
  const { view, params: before } = parse();
  const next = new URLSearchParams(query(params));
  if (before.has('meter')) next.set('meter', before.get('meter'));
  navigate(view, next, { replace });
}

export function openMeter(id) {
  const { view, params } = parse();
  if (params.get('meter') === id) return;
  params.set('meter', id);
  navigate(view, params, { state: { drawer: true } });
}

export function closeMeter() {
  const { view, params } = parse();
  if (!params.has('meter')) return;
  if (history.state?.drawer) {
    history.back();
  } else {
    params.delete('meter');
    navigate(view, params, { replace: true });
  }
}

window.addEventListener('popstate', emit);
window.addEventListener('hashchange', emit);
