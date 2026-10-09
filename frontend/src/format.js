// Number and time formatting, and the unit conversions used on screen.
//
// Times from the server are Beijing wall-clock times with no timezone, e.g.
// "2026-07-07T10:00:00". They are only ever split as text here - never put through the
// browser's clock - so nothing shifts by the viewer's own timezone. Times sent back to
// the server are written the same way (no "Z", no offset).

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-07-07T10:00:00" -> {year, month, day, hour, minute}
export function splitTime(text) {
  const datePart = text.slice(0, 10).split("-");
  const timePart = text.length > 11 ? text.slice(11, 16).split(":") : ["00", "00"];
  return {
    year: Number(datePart[0]),
    month: Number(datePart[1]),
    day: Number(datePart[2]),
    hour: Number(timePart[0]),
    minute: Number(timePart[1]),
  };
}

function weekday(t) {
  // Date.UTC only does calendar arithmetic here; no timezone is involved
  return WEEKDAYS[new Date(Date.UTC(t.year, t.month - 1, t.day)).getUTCDay()];
}

function two(n) {
  return String(n).padStart(2, "0");
}

// "Tue 7 Jul 10:00"
export function fmtTime(text) {
  if (!text) return "";
  const t = splitTime(text);
  return `${weekday(t)} ${t.day} ${MONTHS[t.month - 1]} ${two(t.hour)}:${two(t.minute)}`;
}

// "7 Jul 2026"
export function fmtDate(text) {
  if (!text) return "";
  const t = splitTime(text);
  return `${t.day} ${MONTHS[t.month - 1]} ${t.year}`;
}

// "Tue 7 Jul"
export function fmtDay(text) {
  if (!text) return "";
  const t = splitTime(text);
  return `${weekday(t)} ${t.day} ${MONTHS[t.month - 1]}`;
}

// "6 Aug 10:00" (an expiry, short enough for a button)
export function fmtExpiry(text) {
  if (!text) return "";
  const t = splitTime(text);
  return `${t.day} ${MONTHS[t.month - 1]} ${two(t.hour)}:${two(t.minute)}`;
}

// "10:00"
export function fmtClock(text) {
  if (!text) return "";
  const t = splitTime(text);
  return `${two(t.hour)}:${two(t.minute)}`;
}

export function dateOf(text) {
  return text.slice(0, 10);
}

// 1234567.8 -> "1,234,568"
export function fmtNum(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  // A tiny negative leftover (e.g. -1e-10) would print as "-0": show it as 0
  if (Number(value.toFixed(digits)) === 0) value = 0;
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// Same, with a "+" in front of positive numbers
export function fmtSigned(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const text = fmtNum(value, digits);
  if (value > 0 && text !== fmtNum(0, digits)) return "+" + text;
  return text;
}

// 12_500_000 -> "12.5m", 250_000 -> "250k"
export function fmtShort(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const size = Math.abs(value);
  if (size >= 1e9) return (value / 1e9).toFixed(2) + "bn";
  if (size >= 1e6) return (value / 1e6).toFixed(2) + "m";
  if (size >= 1e3) return (value / 1e3).toFixed(0) + "k";
  return value.toFixed(0);
}

// 0.0225 -> "2.25%"
export function fmtVol(value, digits = 2) {
  if (value === null || value === undefined) return "–";
  return (value * 100).toFixed(digits) + "%";
}

export function fmtRate(value) {
  return fmtVol(value, 3);
}

export function fmtSpot(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  return value.toFixed(4);
}

// Colour class for a P&L number
export function pnlClass(value) {
  if (value > 0.5) return "pos";
  if (value < -0.5) return "neg";
  return "";
}

// ---- Units shown on screen ----
// The engine's Greeks are raw derivatives (per 1.00 of vol, per 1 CNY of spot, per year).
// The screen shows them the way a desk talks about them:
//   delta   USD                                  as is
//   gamma   USD of delta per 100-pip spot move   gamma x 0.01 (100 pips = 0.01 CNY,
//                                                roughly one day's move in USD/CNY)
//   vega    CNY per vol point (0.01)             vega / 100
//   vanna   USD of delta per vol point           vanna / 100
//   volga   CNY of vega-per-point per vol point  volga / 100 / 100
//   theta   CNY per calendar day                 theta / 365

export function gamma100(gamma) {
  return gamma * 0.01;
}

export function vegaPt(vega) {
  return vega / 100;
}

export function vannaPt(vanna) {
  return vanna / 100;
}

export function volgaPt(volga) {
  return volga / 10000;
}

export function thetaDay(theta) {
  return theta / 365;
}