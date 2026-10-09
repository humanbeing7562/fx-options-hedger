// P&L: what the last market move did to the book (the server's step-by-step explain),
// what trading at this fixing added, and the totals since the start.
//
// The explain revalues the whole book one market input at a time:
//   carry (interest on cash and the USD hedge) -> time (theta) -> spot -> rates -> vol.
// Each stage is the change in value from moving that one input, so the stages add up
// exactly to the move. Spot is split into delta (first order), gamma (second order) and
// the rest; vol into vega (first order) and the rest.

import { dateOf, fmtClock, fmtDay, fmtSigned, pnlClass } from "../format.js";

function Line({ label, value, indent, strong }) {
  return (
    <tr className={(indent ? "indent " : "") + (strong ? "strong" : "")}>
      <td>{label}</td>
      <td className={"mono " + pnlClass(value)}>{fmtSigned(value)}</td>
    </tr>
  );
}

export default function PnlPanel({ history, pnl }) {
  // The most recent clock step, and the trading done since it (at the current fixing)
  let lastStep = null;
  let lastStepAt = -1;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].type === "step") {
      lastStep = history[i];
      lastStepAt = i;
      break;
    }
  }
  let tradingNow = 0;
  for (let i = Math.max(lastStepAt, 0); i < history.length; i++) {
    if (history[i].trade_pnl_cny !== undefined) tradingNow += history[i].trade_pnl_cny;
  }

  const e = lastStep ? lastStep.pnl_explain : null;

  return (
    <div className="card">
      <div className="card-head">
        <h2>P&amp;L</h2>
        <span className="muted">CNY</span>
      </div>

      {e ? (
        <>
          <div className="label section-label">
            Last move: {fmtDay(lastStep.from)} {fmtClock(lastStep.from)} → {dateOf(lastStep.time) !== dateOf(lastStep.from) && fmtDay(lastStep.time) + " "}{fmtClock(lastStep.time)}
            {" "}· spot {fmtSigned(e.dS_pips, 1)} pips
          </div>
          <table className="kv pnl">
            <tbody>
              <Line label="Carry (interest)" value={e.carry} />
              <Line label="Time (theta)" value={e.time} />
              <Line label="Spot" value={e.spot} />
              <Line label="delta" value={e.spot_split.delta} indent />
              <Line label="gamma" value={e.spot_split.gamma} indent />
              <Line label="higher order" value={e.spot_split.other} indent />
              <Line label="Rates" value={e.rates} />
              <Line label="Vol" value={e.vol} />
              <Line label="vega" value={e.vol_split.vega} indent />
              <Line label="smile / higher order" value={e.vol_split.other} indent />
              <Line label="Market move" value={e.total} strong />
            </tbody>
          </table>
          <details>
            <summary>By bucket</summary>
            <div className="table-wrap">
            <table className="data compact">
              <thead>
                <tr><th>Bucket</th><th>Time</th><th>Spot</th><th>Rates</th><th>Vol</th></tr>
              </thead>
              <tbody>
                {Object.keys(e.by_bucket).map((label) => {
                  const b = e.by_bucket[label];
                  if (b.positions === 0) return null;
                  return (
                    <tr key={label}>
                      <td>{label}</td>
                      <td className={"mono " + pnlClass(b.time)}>{fmtSigned(b.time)}</td>
                      <td className={"mono " + pnlClass(b.spot)}>{fmtSigned(b.spot)}</td>
                      <td className={"mono " + pnlClass(b.rates)}>{fmtSigned(b.rates)}</td>
                      <td className={"mono " + pnlClass(b.vol)}>{fmtSigned(b.vol)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
            <p className="hint">Options only; the spot hedge and cash are in the totals above.</p>
          </details>
        </>
      ) : (
        <p className="hint">No market move yet. Book some trades, then press "Next fixing".</p>
      )}

      <table className="kv pnl">
        <tbody>
          <Line label="Trading at this fixing (edge)" value={tradingNow} strong />
        </tbody>
      </table>

      {pnl && (
        <>
          <div className="label section-label">Since the start ({pnl.steps} fixings)</div>
          <table className="kv pnl">
            <tbody>
              <Line label="Carry" value={pnl.carry} />
              <Line label="Time" value={pnl.time} />
              <Line label="Spot" value={pnl.spot} />
              <Line label="Rates" value={pnl.rates} />
              <Line label="Vol" value={pnl.vol} />
              <Line label="Trading" value={pnl.trading} />
              <Line label="Total = NAV" value={pnl.total} strong />
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
