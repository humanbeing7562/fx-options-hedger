// NAV and spot over the fixings so far, drawn as plain SVG lines.
// One point per fixing: the last record at that fixing (so trades booked at a fixing count).

import { useState } from "react";
import { dateOf, fmtClock, fmtDay, fmtShort, fmtSigned, fmtSpot } from "../format.js";

const WIDTH = 420;
const HEIGHT = 150;
const PAD_LEFT = 58;
const PAD_RIGHT = 10;
const PAD_TOP = 10;
const PAD_BOTTOM = 22;

// [{time, nav, spot}] - one per fixing reached
function pointsByFixing(history) {
  const byIndex = new Map();
  for (const record of history) {
    byIndex.set(record.index, { time: record.time, nav: record.nav_cny, spot: record.spot });
  }
  const points = [];
  const indexes = Array.from(byIndex.keys()).sort((a, b) => a - b);
  for (const index of indexes) {
    points.push(byIndex.get(index));
  }
  return points;
}

function LineChart({ title, points, valueOf, format, axisFormat, zeroLine }) {
  const [hover, setHover] = useState(null);

  if (points.length < 2) {
    return (
      <div className="chart">
        <div className="label">{title}</div>
        <p className="hint">The chart starts after the first fixing.</p>
      </div>
    );
  }

  let low = Infinity;
  let high = -Infinity;
  for (const p of points) {
    const v = valueOf(p);
    if (v < low) low = v;
    if (v > high) high = v;
  }
  if (zeroLine) {
    if (low > 0) low = 0;
    if (high < 0) high = 0;
  }
  if (high - low < 1e-9) {
    high += 1;
    low -= 1;
  }
  const margin = (high - low) * 0.08;
  high += margin;
  low -= margin;

  const plotWidth = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;

  function xOf(i) {
    return PAD_LEFT + (i / (points.length - 1)) * plotWidth;
  }
  function yOf(v) {
    return PAD_TOP + ((high - v) / (high - low)) * plotHeight;
  }

  let path = "";
  for (let i = 0; i < points.length; i++) {
    path += (i === 0 ? "M" : "L") + xOf(i).toFixed(1) + "," + yOf(valueOf(points[i])).toFixed(1) + " ";
  }

  // Faint lines where a new day starts
  const dayLines = [];
  for (let i = 1; i < points.length; i++) {
    if (dateOf(points[i].time) !== dateOf(points[i - 1].time)) dayLines.push(i);
  }

  function onMove(event) {
    const box = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * WIDTH;
    let i = Math.round(((x - PAD_LEFT) / plotWidth) * (points.length - 1));
    if (i < 0) i = 0;
    if (i > points.length - 1) i = points.length - 1;
    setHover(i);
  }

  const shown = hover === null ? points.length - 1 : hover;
  const shownPoint = points[shown];

  return (
    <div className="chart">
      <div className="chart-head">
        <span className="label">{title}</span>
        <span className="mono">
          {format(valueOf(shownPoint))} <span className="muted">{fmtDay(shownPoint.time)} {fmtClock(shownPoint.time)}</span>
        </span>
      </div>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="chart-svg"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={title}
      >
        {dayLines.map((i) => (
          <line key={i} x1={xOf(i)} x2={xOf(i)} y1={PAD_TOP} y2={HEIGHT - PAD_BOTTOM} className="day-line" />
        ))}
        {zeroLine && <line x1={PAD_LEFT} x2={WIDTH - PAD_RIGHT} y1={yOf(0)} y2={yOf(0)} className="zero-line" />}
        <text x={PAD_LEFT - 6} y={PAD_TOP + 8} className="axis" textAnchor="end">{(axisFormat || format)(high)}</text>
        <text x={PAD_LEFT - 6} y={HEIGHT - PAD_BOTTOM} className="axis" textAnchor="end">{(axisFormat || format)(low)}</text>
        <text x={PAD_LEFT} y={HEIGHT - 6} className="axis">{fmtDay(points[0].time)}</text>
        <text x={WIDTH - PAD_RIGHT} y={HEIGHT - 6} className="axis" textAnchor="end">{fmtDay(points[points.length - 1].time)}</text>
        <path d={path} className="line" />
        <circle cx={xOf(shown)} cy={yOf(valueOf(shownPoint))} r="3" className="dot" />
      </svg>
    </div>
  );
}

function navOf(p) {
  return p.nav;
}

function spotOf(p) {
  return p.spot;
}

function fmtCny(v) {
  return fmtSigned(v);
}

// Axis labels must fit the left margin: 325,212,315 -> "+325.21m"
function fmtCnyShort(v) {
  if (Math.abs(v) < 1e5) return fmtSigned(v);
  return (v > 0 ? "+" : "") + fmtShort(v);
}

export default function Charts({ history }) {
  const points = pointsByFixing(history);
  return (
    <div className="card">
      <LineChart title="NAV (CNY)" points={points} valueOf={navOf} format={fmtCny} axisFormat={fmtCnyShort} zeroLine />
      <LineChart title="USD/CNY spot" points={points} valueOf={spotOf} format={fmtSpot} />
    </div>
  );
}