// Network: the drill-down tree, why the hierarchy is not a strict tree, and one node's details.

import * as api from '../api.js';
import { LEVELS, LEVEL_LABEL, LEVEL_TAG, num, plural } from '../format.js';
import { href, setParams } from '../router.js';
import { Region, append, badge, card, dataTable, empty, h, icon, mixBar, mixLabel, nodeHref, statusLegend } from '../ui.js';

const occurrences = new Map(); // "level:code" -> [{ li, button }], one per place in the tree
let regions;
let treeEl;
let selection = { level: null, code: null };
let lastKey = null;
let started = false;

export function mount(el) {
  treeEl = h('div');
  const overviewEl = h('div');
  const detailEl = h('div');
  const tools = h('div', { class: 'tree-tools' },
    h('button', { type: 'button', class: 'btn small', onclick: () => expandAll(true) }, 'Expand all'),
    h('button', { type: 'button', class: 'btn small', onclick: () => expandAll(false) }, 'Collapse all'));
  el.append(
    h('header', { class: 'view-head' }, h('div', null,
      h('h1', { tabindex: '-1' }, 'Network'),
      h('p', null, 'The distribution network from zone down to transformer, rebuilt from what each meter reports about itself.'))),
    card({ title: 'Is it a tree?', subtitle: 'How each level links to the one above it.' }, overviewEl),
    h('div', { class: 'network-layout' },
      card({ title: 'Drill-down', subtitle: 'A code reused under several parents appears under each of them, so counts add up at every level.', actions: tools }, treeEl),
      h('section', { class: 'card node-detail', 'aria-label': 'Selected node' }, detailEl)),
  );
  regions = { overview: new Region(overviewEl), tree: new Region(treeEl), detail: new Region(detailEl) };
}

export function update(route) {
  const level = LEVELS.includes(route.get('level')) ? route.get('level') : null;
  const code = level ? route.get('code') : null;
  const key = `${level}:${code}`;
  if (key === lastKey) return;
  lastKey = key;
  selection = { level, code };
  if (!started) {
    started = true;
    regions.overview.load(() => api.networkOverview(), renderOverview);
    regions.tree.load(() => api.get('/v1/network/tree', null, { ttl: 300_000 }), renderTree);
  }
  highlight();
  if (level && code) {
    regions.detail.load(async () => {
      const [node, transformers] = await Promise.all([
        api.get(`/v1/network/${level}/${encodeURIComponent(code)}`),
        level === 'transformer' ? api.transformers().catch(() => null) : null,
      ]);
      return { node, transformer: transformers?.items.find((t) => t.code === node.code) };
    }, renderDetail);
  } else {
    regions.detail.load(async () => null, renderHint);
  }
}

// ----------------------------------------------------------------------------- is it a tree?

export function edgesTable(overview) {
  return dataTable({
    caption: 'Links between network levels',
    columns: [
      { label: 'Child → parent', cell: (e) => `${LEVEL_LABEL[e.child_level]} → ${LEVEL_LABEL[e.parent_level].toLowerCase()}` },
      { label: 'Distinct links', num: true, opt: true, cell: (e) => num(e.distinct_pairs) },
      {
        label: 'Children with several parents',
        num: true,
        cell: (e) => `${num(e.children_with_multiple_parents)} of ${num(overview.node_counts[e.child_level] ?? 0)}`,
      },
      { label: 'Tree-like', cell: (e) => (e.functional ? badge('Yes', 'good', 'installed') : badge('No', 'warning', 'warning')) },
    ],
    rows: overview.edges,
    stack: true,
  });
}

function renderOverview(overview, el) {
  el.append(
    h('div', { class: 'callout', dataset: { tone: overview.is_tree ? 'good' : 'warning' } },
      icon(overview.is_tree ? 'installed' : 'warning'),
      h('span', null, overview.is_tree ? 'Every node has exactly one parent: the hierarchy is a tree.' : overview.note)),
    h('div', { class: 'node-counts' }, LEVELS.map((level) => {
      const count = overview.node_counts[level] ?? 0;
      return h('span', null, h('b', null, num(count)), `${LEVEL_LABEL[level].toLowerCase()}${count === 1 ? '' : 's'}`);
    })),
    edgesTable(overview),
  );
}

// ----------------------------------------------------------------------------- drill-down tree

function setExpanded(li, open) {
  const list = li.querySelector(':scope > ul');
  if (!list) return;
  list.hidden = !open;
  li.querySelector(':scope > .tree-row > .tree-toggle').setAttribute('aria-expanded', String(open));
}

function expandAll(open) {
  for (const li of treeEl.querySelectorAll('li')) setExpanded(li, open);
}

function renderTree(roots, el) {
  occurrences.clear();
  const build = (nodes, depth) => h('ul', { class: depth ? null : 'tree' }, nodes.map((node) => {
    const li = h('li');
    const children = node.children.length ? build(node.children, depth + 1) : null;
    if (children) children.hidden = depth > 0;
    const toggle = children
      ? h('button', {
        type: 'button', class: 'tree-toggle', 'aria-expanded': String(depth === 0), 'aria-label': `What is under ${node.code}`,
        onclick: () => setExpanded(li, children.hidden),
      }, icon('chevron'))
      : h('span', { class: 'tree-spacer' });
    const button = h('button', {
      type: 'button',
      class: 'tree-node',
      'aria-label': `${LEVEL_LABEL[node.level]} ${node.code}${node.name ? `, ${node.name}` : ''}: ${plural(node.meter_count, 'meter')} (${mixLabel(node.meters_by_status)})`,
      onclick: () => setParams({ level: node.level, code: node.code }, { replace: false }),
    },
    h('span', { class: 'tag' }, LEVEL_TAG[node.level]),
    h('span', { class: 'label' }, h('b', null, node.code), node.name ?? ''),
    h('span', { class: 'count' }, num(node.meter_count)),
    mixBar(node.meters_by_status, 'thin'));
    append(li, h('div', { class: 'tree-row' }, toggle, button), children); // a leaf has no children
    const key = `${node.level}:${node.code}`;
    occurrences.set(key, [...(occurrences.get(key) ?? []), { li, button }]);
    return li;
  }));
  el.append(build(roots, 0));
  highlight();
  if (!selection.level) regions.detail.load(async () => null, renderHint);
}

function highlight() {
  for (const button of treeEl.querySelectorAll('.tree-node[aria-current]')) button.removeAttribute('aria-current');
  const found = occurrences.get(`${selection.level}:${selection.code}`) ?? [];
  for (const { li, button } of found) {
    button.setAttribute('aria-current', 'true');
    for (let parent = li.parentElement.closest('li'); parent; parent = parent.parentElement.closest('li')) setExpanded(parent, true);
  }
  found[0]?.button.scrollIntoView({ block: 'nearest' });
}

// ----------------------------------------------------------------------------- node details

function renderHint(_, el) {
  const levelOf = (key) => LEVELS.indexOf(key.split(':')[0]);
  const [key, places] = [...occurrences].sort((a, b) => b[1].length - a[1].length || levelOf(a[0]) - levelOf(b[0]))[0] ?? [];
  const [level, code] = key?.split(':') ?? [];
  append(el,
    h('h2', null, 'Select a node'),
    h('p', { class: 'muted', style: { 'margin-top': '6px' } }, 'Pick any node in the tree to see all of its parents, its children and its meters.'),
    places?.length > 1 && h('p', { class: 'callout', style: { 'margin-top': '12px' } }, icon('info'),
      h('span', null, `For example, ${LEVEL_LABEL[level].toLowerCase()} `, h('a', { href: nodeHref(level, code) }, code),
        ` appears in ${num(places.length)} places in the tree.`)),
  );
}

function renderDetail({ node, transformer }, el) {
  const index = LEVELS.indexOf(node.level);
  const parentLevel = LEVELS[index - 1];
  const childLevel = LEVELS[index + 1];
  const fact = (label, value) => h('div', { class: 'fact' }, h('dt', null, label), h('dd', null, value));
  const neighbours = (list, level) => dataTable({
    caption: `${LEVEL_LABEL[level]}s`,
    columns: [
      { label: LEVEL_LABEL[level], cell: (n) => [h('a', { href: nodeHref(level, n.code) }, n.code), n.name && h('span', { class: 'sub' }, n.name)] },
      { label: 'Transformers', num: true, cell: (n) => num(n.transformer_count) },
      { label: 'Meters', num: true, cell: (n) => num(n.meter_count) },
    ],
    rows: list,
  });
  const section = (title, body) => h('div', { class: 'detail-grid' }, h('h3', null, title), body);

  el.append(
    h('div', { class: 'card-head' },
      h('div', null, h('h2', null, `${LEVEL_LABEL[node.level]} ${node.code}`), node.name && h('p', null, node.name)),
      h('a', { class: 'btn small', href: href('meters', { [node.level]: node.code }) }, 'List its meters', icon('arrow'))),
    h('div', { class: 'detail-grid' },
      h('dl', { class: 'facts' },
        fact('Meters', num(node.meter_count)),
        fact('Transformers', num(node.transformer_count)),
        parentLevel && fact(`Parent ${LEVEL_LABEL[parentLevel].toLowerCase()}s`, num(node.parents.length)),
        transformer && fact('Rating', transformer.capacity_kva != null ? `${num(transformer.capacity_kva)} kVA` : 'unknown')),
      mixBar(node.meters_by_status),
      statusLegend(node.meters_by_status),
      node.parents.length > 1 && h('div', { class: 'callout', dataset: { tone: 'warning' } }, icon('warning'), h('span', null,
        `${node.code} appears under ${num(node.parents.length)} ${LEVEL_LABEL[parentLevel].toLowerCase()}s in the portal’s data. `
        + `The numbers here cover all of them; the tree shows ${node.code} once under each.`)),
      node.name_variants.length > 0 && h('div', { class: 'callout', dataset: { tone: 'warning' } }, icon('warning'), h('span', null,
        `Some meters report this transformer under another name: ${node.name_variants.map((a) => `“${a}”`).join(', ')}.`)),
      parentLevel && node.parents.length > 0 && section('Parents', neighbours(node.parents, parentLevel)),
      childLevel && node.children.length > 0 && section('Children', neighbours(node.children, childLevel)),
      node.level !== 'transformer' && section('Transformers', h('div', { class: 'chips' },
        node.transformers.map((code) => h('a', { href: nodeHref('transformer', code) }, code)))),
      node.meter_count === 0 && empty('No meters are connected here.')),
  );
}
