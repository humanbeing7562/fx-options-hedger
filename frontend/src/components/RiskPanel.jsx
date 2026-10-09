// Risk: the book's Greeks against the desk's limits, total and by expiry bucket.
//
// Colours: green under 50% of a limit, amber from 50%, red at or over it.
// The limits can be changed here (they only colour the screen; with automatic
// "threshold" hedging the session uses the limits it was created with).

import { useState } from "react";
import { fmtNum, fmtSigned, gamma100, vegaPt, vannaPt, volgaPt, thetaDay } from "../format.js";

// Fraction of the limit used; null when no limit is set (0 or empty = no limit)
export function usage(value, limit) {
  if (!(limit > 0)) return null;
  return Math.abs(value) / limit;
}

export function levelClass(fraction) {
  if (fraction === null) return "plain";
  if (fraction >= 1) return "red";
  if (fraction >= 0.5) return "amber";
  return "green";
}

function limitText(limit) {
  if (!(limit > 0)) return "no limit";
  return "limit " + fmtNum(limit);
}

function LimitBar({ fraction }) {
  if (fraction === null) return null;
  const width = Math.min(fraction, 1) * 100;
  return (
    <div className="bar">
      <div className={"bar-fill " + levelClass(fraction)} style={{ width: width + "%" }} />
    </div>
  );
}

export default function RiskPanel({ state, limits, onLimitsChange }) {
  const [editing, setEditing] = useState(false);
  const g = state.greeks;
  const spot = state.spot;

  // The largest vega in any one bucket is what the vega limit is about
  let worstVega = 0;
  let worstBucket = "";
  for (const label in state.greeks_by_bucket) {
    const v = vegaPt(state.greeks_by_bucket[label].vega);
    if (Math.abs(v) > Math.abs(worstVega)) {
      worstVega = v;
      worstBucket = label;
    }
  }

  const deltaUse = usage(g.delta, limits.delta_usd);
  const gammaValue = gamma100(g.gamma);
  const gammaUse = usage(gammaValue, limits.gamma_usd_per_100pips);
  const vegaUse = usage(worstVega, limits.vega_cny_per_volpt);

  function setLimit(name, text) {
    const next = { ...limits };
    next[name] = Number(text);
    onLimitsChange(next);
  }

  const bucketRows = [];
  for (const label in state.greeks_by_bucket) {
    bucketRows.push([label, state.greeks_by_bucket[label]]);
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Risk</h2>
        <button className="small" onClick={() => setEditing(!editing)}>{editing ? "Done" : "Limits"}</button>
      </div>

      {editing && (
        <div className="row limits-edit">
          <label>
            Delta (USD)
            <input type="number" min="0" step="any" value={limits.delta_usd} onChange={(e) => setLimit("delta_usd", e.target.value)} />
          </label>
          <label>
            Gamma (USD / 100 pips)
            <input type="number" min="0" step="any" value={limits.gamma_usd_per_100pips} onChange={(e) => setLimit("gamma_usd_per_100pips", e.target.value)} />
          </label>
          <label>
            Vega per bucket (CNY / pt)
            <input type="number" min="0" step="any" value={limits.vega_cny_per_volpt} onChange={(e) => setLimit("vega_cny_per_volpt", e.target.value)} />
          </label>
        </div>
      )}

      <div className="greek-cards">
        <div className={"greek " + levelClass(deltaUse)}>
          <div className="label">Delta</div>
          <div className="value mono">{fmtSigned(g.delta)}</div>
          <div className="unit">USD · {limitText(limits.delta_usd)}</div>
          <LimitBar fraction={deltaUse} />
        </div>
        <div className={"greek " + levelClass(gammaUse)}>
          <div className="label">Gamma</div>
          <div className="value mono">{fmtSigned(gammaValue)}</div>
          <div className="unit">USD delta per 100 pips spot · {limitText(limits.gamma_usd_per_100pips)}</div>
          <LimitBar fraction={gammaUse} />
        </div>
        <div className={"greek " + levelClass(vegaUse)}>
          <div className="label">Vega</div>
          <div className="value mono">{fmtSigned(vegaPt(g.vega))}</div>
          <div className="unit">
            CNY per vol pt · worst bucket {worstBucket ? `${worstBucket} ${fmtSigned(worstVega)}` : "–"} · {limitText(limits.vega_cny_per_volpt)}
          </div>
          <LimitBar fraction={vegaUse} />
        </div>
        <div className="greek plain">
          <div className="label">Theta</div>
          <div className="value mono">{fmtSigned(thetaDay(g.theta))}</div>
          <div className="unit">CNY per day</div>
        </div>
      </div>

      <div className="table-wrap">
      <table className="data compact">
        <thead>
          <tr>
            <th>Bucket</th>
            <th title="option positions">Pos</th>
            <th title="USD">Delta</th>
            <th title="USD delta per 100-pip spot move">Gamma 100p</th>
            <th title="CNY per vol point">Vega</th>
            <th title="USD delta per vol point (risk reversal risk)">Vanna</th>
            <th title="CNY vega per vol point, per vol point (butterfly risk)">Volga</th>
          </tr>
        </thead>
        <tbody>
          {bucketRows.map(([label, b]) => {
            const vega = vegaPt(b.vega);
            const empty = b.positions === 0;
            return (
              <tr key={label} className={empty ? "dim" : ""}>
                <td>{label}</td>
                <td>{b.positions}</td>
                <td className="mono">{fmtSigned(b.delta)}</td>
                <td className="mono">{fmtSigned(gamma100(b.gamma))}</td>
                <td className={"mono " + (empty ? "" : "cell-" + levelClass(usage(vega, limits.vega_cny_per_volpt)))}>{fmtSigned(vega)}</td>
                <td className="mono">{fmtSigned(vannaPt(b.vanna))}</td>
                <td className="mono">{fmtSigned(volgaPt(b.volga))}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          {/* Spot has no expiry, so it sits in no bucket; it only carries delta */}
          <tr className="spot-row">
            <td>Spot</td>
            <td></td>
            <td className="mono">{fmtSigned(state.hedge_usd)}</td>
            <td className="mono muted">–</td>
            <td className="mono muted">–</td>
            <td className="mono muted">–</td>
            <td className="mono muted">–</td>
          </tr>
          <tr className="total-row">
            <td>Total</td>
            <td>{state.positions.length}</td>
            <td className="mono">{fmtSigned(g.delta)}</td>
            <td className="mono">{fmtSigned(gamma100(g.gamma))}</td>
            <td className="mono">{fmtSigned(vegaPt(g.vega))}</td>
            <td className="mono">{fmtSigned(vannaPt(g.vanna))}</td>
            <td className="mono">{fmtSigned(volgaPt(g.volga))}</td>
          </tr>
        </tfoot>
      </table>
      </div>
      <p className="hint">
        Spot is your USD position from delta hedges and spot trades. Delta is hedged as one total
        (a spot move hits every expiry alike); vega, vanna and volga are hedged per bucket.
      </p>
    </div>
  );
}