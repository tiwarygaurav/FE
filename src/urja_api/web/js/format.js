// Formatting, labels and calendar helpers. No DOM here.
// Numbers use Indian digit grouping and every time is shown in IST: the utility is in Jaipur.

export const DASH = '—';
const LOCALE = 'en-IN';
const TIME_ZONE = 'Asia/Kolkata';
const DAY_MS = 86_400_000;

const numberFormats = new Map();
export function num(value, digits = 0) {
  if (value == null || Number.isNaN(value)) return DASH;
  if (!numberFormats.has(digits)) {
    numberFormats.set(digits, new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return numberFormats.get(digits).format(value);
}

/** kWh with as many decimals as the magnitude deserves: 12,345 · 123.4 · 1.23. */
export function kwh(value, { unit = true } = {}) {
  if (value == null) return DASH;
  const abs = Math.abs(value);
  const text = num(value, abs >= 1000 ? 0 : abs >= 100 ? 1 : 2);
  return unit ? `${text} kWh` : text;
}

export const pct = (share, digits = 1) => (share == null ? DASH : `${num(share * 100, digits)}%`);
export const plural = (n, one, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`;
export const humanize = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replaceAll('_', ' ') : '');

// ----------------------------------------------------------------------------- dates

const isDay = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
const toDate = (value) => (value instanceof Date ? value : new Date(isDay(value) ? `${value}T00:00:00+05:30` : value));
const dtf = (options) => new Intl.DateTimeFormat(LOCALE, { timeZone: TIME_ZONE, ...options });
const DATE = dtf({ day: 'numeric', month: 'short', year: 'numeric' });
const DATE_TIME = dtf({ day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const DAY_MONTH = dtf({ day: 'numeric', month: 'short' });
const CLOCK = dtf({ hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const WEEKDAY = dtf({ weekday: 'short', day: 'numeric', month: 'short' });
const YMD = new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' });

export const date = (value) => (value ? DATE.format(toDate(value)) : DASH);
export const dateTime = (value) => (value ? `${DATE_TIME.format(toDate(value))} IST` : DASH);
export const dayMonth = (value) => DAY_MONTH.format(toDate(value));
export const weekday = (value) => WEEKDAY.format(toDate(value));
export const clock = (value) => CLOCK.format(toDate(value));
/** The IST calendar day of an instant, as YYYY-MM-DD. */
export const istDay = (value) => YMD.format(toDate(value));

export function addDays(day, days) {
  const d = new Date(Date.parse(`${day}T00:00:00Z`) + days * DAY_MS);
  return d.toISOString().slice(0, 10);
}
export const ageDays = (value, now = Date.now()) => (now - toDate(value).getTime()) / DAY_MS;

/** "23–29 Jun 2026", "28 May – 3 Jun 2026", or a single day. */
export function dayRange(from, to) {
  if (from === to) return date(from);
  if (from.slice(0, 7) === to.slice(0, 7)) return `${Number(from.slice(8))}–${date(to)}`;
  return `${dayMonth(from)} – ${date(to)}`;
}

const RELATIVE = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
export function ago(value, now = Date.now()) {
  const seconds = (toDate(value).getTime() - now) / 1000;
  const abs = Math.abs(seconds);
  if (abs < 60) return RELATIVE.format(Math.round(seconds), 'second');
  if (abs < 3600) return RELATIVE.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return RELATIVE.format(Math.round(seconds / 3600), 'hour');
  return RELATIVE.format(Math.round(seconds / 86400), 'day');
}

/** "92 days (about 3 months)" for ages worth a second unit. */
export function age(days) {
  const whole = Math.floor(days);
  if (whole < 1) return 'less than a day';
  const months = Math.round(days / 30.44);
  return whole < 45 ? plural(whole, 'day') : `${plural(whole, 'day')} (about ${plural(months, 'month')})`;
}

// ----------------------------------------------------------------------------- vocabulary

export const STATUSES = ['installed', 'faulty', 'decommissioned', 'unknown'];
export const STATUS_LABEL = { installed: 'Installed', faulty: 'Faulty', decommissioned: 'Decommissioned', unknown: 'Unknown' };
export const PHASE_LABEL = { single: 'Single-phase', three: 'Three-phase', unknown: 'Unknown phase' };
export const INSTALL_LABEL = { whole_current: 'Whole current', ct_operated: 'CT operated', unknown: 'Unknown' };
export const INTERVAL_LABEL = { 30: 'Half-hourly', 1440: 'Daily' };
export const intervalLabel = (minutes) => (minutes == null ? DASH : INTERVAL_LABEL[minutes] ?? `Every ${num(minutes)} min`);
export const LEVELS = ['zone', 'circle', 'division', 'subdivision', 'substation', 'feeder', 'transformer'];
export const LEVEL_LABEL = {
  zone: 'Zone', circle: 'Circle', division: 'Division', subdivision: 'Subdivision',
  substation: 'Substation', feeder: 'Feeder', transformer: 'Transformer',
};
export const LEVEL_TAG = { zone: 'Z', circle: 'C', division: 'D', subdivision: 'SD', substation: 'SS', feeder: 'F', transformer: 'DT' };
export const SEVERITIES = ['error', 'warning', 'info'];
export const SEVERITY_LABEL = { error: 'Error', warning: 'Warning', info: 'Info' };

export const RULE_LABEL = {
  decommissioned_reporting: 'Decommissioned but consuming',
  stale: 'No recent readings',
  duplicate_timestamps: 'Duplicate timestamps',
  conflicting_duplicates: 'Conflicting duplicate readings',
  missing_values: 'Missing register or voltage values',
  gaps: 'Gaps in the reading series',
  register_decrease: 'kWh register went down',
  implausible_load: 'Load above the meter’s rating',
  flatline: 'No consumption for 24 h+',
  consumption_surge: 'Consumption surge',
  power_factor_above_one: 'Power factor above 1',
  no_voltage: 'No supply voltage',
  voltage_outside_10pct: 'Voltage outside ±10%',
  voltage_outside_6pct: 'Voltage outside the statutory ±6%',
};
export const ruleLabel = (rule) => RULE_LABEL[rule] ?? humanize(rule);

export const ISSUE_INFO = {
  blank_code: ['Missing code', 'The portal left this network code empty; the API derived it from the meter’s transformer.'],
  blank_name: ['Missing name', 'The portal left this name empty; the API serves the name other meters report for the code.'],
  stale_name: ['Outdated name', 'The meter reports an old name for its node; the API serves the current one.'],
  path_conflict: ['Path conflict', 'The meter reports a different node than the other meters on its transformer.'],
  unrecognised_value: ['Unrecognised value', 'The portal sent a value outside the known vocabulary.'],
};
export const issueLabel = (code) => ISSUE_INFO[code]?.[0] ?? humanize(code);
