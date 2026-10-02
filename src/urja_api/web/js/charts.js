// Charts: Chart.js from jsDelivr, loaded on first use, styled from the CSS tokens and redrawn
// when the theme changes. Every chart comes with a table of the same numbers.

import { h, icon, loadScript } from './ui.js';

const CHART_JS = [
  'https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js',
  'sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ',
];
const live = new Map(); // canvas -> build(theme) returning a Chart.js config

function theme() {
  const style = getComputedStyle(document.documentElement);
  const v = (name) => style.getPropertyValue(name).trim();
  return {
    series: v('--series-1'),
    surface: v('--surface'),
    text1: v('--text-1'),
    text2: v('--text-2'),
    text3: v('--text-3'),
    grid: v('--grid'),
    border: v('--border-strong'),
    font: v('--font'),
    motion: !matchMedia('(prefers-reduced-motion: reduce)').matches,
  };
}

function create(Chart, canvas, build, t) {
  Chart.getChart(canvas)?.destroy();
  Chart.defaults.font.family = t.font;
  Chart.defaults.font.size = 12;
  new Chart(canvas, build(t));
}

function prune(Chart) {
  for (const canvas of live.keys()) {
    if (!canvas.isConnected) {
      Chart.getChart(canvas)?.destroy();
      live.delete(canvas);
    }
  }
}

/** Without Chart.js (e.g. the CDN is unreachable) the figure keeps its table of the same numbers. */
function showTableInstead(canvas) {
  const figure = canvas.closest('figure');
  if (!figure) return;
  figure.querySelector('.chart-box')?.remove();
  figure.querySelector('.chart-head > .btn')?.remove();
  for (const el of figure.querySelectorAll(':scope > [hidden]')) el.hidden = false;
  figure.querySelector('.chart-head').after(
    h('p', { class: 'note' }, icon('warning'), 'The charting library could not be loaded, so the numbers are shown as a table.'),
  );
}

export async function draw(canvas, build) {
  if (!window.Chart) {
    try {
      await loadScript(...CHART_JS);
    } catch {
      showTableInstead(canvas);
      return;
    }
  }
  const { Chart } = window;
  prune(Chart);
  if (!canvas.isConnected) return;
  live.set(canvas, build);
  create(Chart, canvas, build, theme());
}

export function redraw() {
  const { Chart } = window;
  if (!Chart) return;
  prune(Chart);
  const t = theme();
  for (const [canvas, build] of live) create(Chart, canvas, build, t);
}

export function baseOptions(t, { yTitle, tooltip } = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: t.motion ? { duration: 250 } : false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: t.surface,
        borderColor: t.border,
        borderWidth: 1,
        cornerRadius: 8,
        padding: 10,
        displayColors: false,
        titleColor: t.text2,
        titleFont: { weight: '500' },
        bodyColor: t.text1,
        bodyFont: { weight: '600', size: 13 },
        footerColor: t.text2,
        footerFont: { weight: '400' },
        callbacks: tooltip,
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: t.grid }, ticks: { color: t.text3, maxRotation: 0, autoSkipPadding: 12 } },
      y: {
        beginAtZero: true,
        grid: { color: t.grid },
        border: { display: false },
        ticks: { color: t.text3, maxTicksLimit: 5 },
        title: { display: Boolean(yTitle), text: yTitle, color: t.text2 },
      },
    },
  };
}

/** A hairline that follows the hovered x position (line charts). */
export const crosshair = {
  id: 'crosshair',
  afterDatasetsDraw(chart, _args, options) {
    const [active] = chart.tooltip?.getActiveElements() ?? [];
    if (!active) return;
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.strokeStyle = options.color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(active.element.x, chartArea.top);
    ctx.lineTo(active.element.x, chartArea.bottom);
    ctx.stroke();
    ctx.restore();
  },
};

/** A chart plus its accessible twin: the same numbers as a table, behind a toggle. */
export function chartFigure({ title, label, legendEl, table, caption, height = 220 }) {
  const canvas = h('canvas', { role: 'img', 'aria-label': label });
  const box = h('div', { class: 'chart-box', style: { height: `${height}px` } }, canvas);
  const tableBox = h('div', { hidden: true }, table);
  const toggle = h('button', { type: 'button', class: 'btn ghost small', 'aria-pressed': 'false' }, icon('table'), 'Table view');
  toggle.addEventListener('click', () => {
    const showTable = tableBox.hidden;
    tableBox.hidden = !showTable;
    box.hidden = showTable;
    toggle.setAttribute('aria-pressed', String(showTable));
  });
  const figure = h(
    'figure',
    { class: 'chart' },
    h('div', { class: 'chart-head' }, title && h('h3', null, title), legendEl, toggle),
    box,
    tableBox,
    caption && h('figcaption', null, caption),
  );
  return { figure, canvas };
}
