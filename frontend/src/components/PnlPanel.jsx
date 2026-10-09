// P&L: what the last market move did to the book (the server's step-by-step explain),
// what trading at this fixing added, and the totals since the start.
//
// The explain revalues the whole book one market input at a time:
//   carry (interest on cash and the USD hedge) -> time (theta) -> spot -> rates -> vol.
// Each stage is the change in value from moving that one input, so the stages add up
// exactly to the move. Spot is split into delta (first order), gamma (second order) and
// the rest; vol into vega (first order) and the rest.

import { dateOf, fmtClock, fmtDay, fmtSigned, pnlClass } from "../format.js";

// What each line means, shown when hovering the label
const LINE_HELP = {
  carry: "Interest earned or paid on CNY cash and the USD spot position over the step.",
  time: "Change in the options' value from the clock moving forward, with the market unchanged (theta).",
  spot: "Change in value from the spot move alone, with the new time but the old rates and vols.",
  delta: "First-order part of the spot P&L: (options' delta + USD position) × spot move.",
  gamma: "Second-order part: ½ × gamma × (spot move)².",
  spotOther: "What delta and gamma don't explain (third order and above).",
  rates: "Change in value from the CNY and USD curves moving.",
  vol: "Change in value from the vol surface moving, smile included.",
  vega: "First-order part of the vol P&L: vega × the change in each option's own vol.",
  volOther: "What vega doesn't explain: second order and above (mostly volga).",
  total: "Sum of the lines above: the change in NAV from the market move.",
  trading: "Change in NAV from trades booked at this fixing: your edge against mid (negative if you paid a spread).",
  tradingTotal: "Edge earned on every trade since the start.",
  nav: "Net asset value: options at mid + USD position × spot + CNY cash. Starts at 0.",
};

function Line({ label, value, indent, strong, help }) {
  return (
    <tr className={(indent ? "indent " : "") + (strong ? "strong" : "")}>
      <td>{help ? <span className="tip" data-tip={help}>{label}</span> : label}</td>
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
              <Line label="Carry (interest)" value={e.carry} help={LINE_HELP.carry} />
              <Line label="Time (theta)" value={e.time} help={LINE_HELP.time} />
              <Line label="Spot" value={e.spot} help={LINE_HELP.spot} />
              <Line label="delta" value={e.spot_split.delta} indent help={LINE_HELP.delta} />
              <Line label="gamma" value={e.spot_split.gamma} indent help={LINE_HELP.gamma} />
              <Line label="higher order" value={e.spot_split.other} indent help={LINE_HELP.spotOther} />
              <Line label="Rates" value={e.rates} help={LINE_HELP.rates} />
              <Line label="Vol" value={e.vol} help={LINE_HELP.vol} />
              <Line label="vega" value={e.vol_split.vega} indent help={LINE_HELP.vega} />
              <Line label="smile / higher order" value={e.vol_split.other} indent help={LINE_HELP.volOther} />
              <Line label="Market move" value={e.total} strong help={LINE_HELP.total} />
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
          <Line label="Trading at this fixing (edge)" value={tradingNow} strong help={LINE_HELP.trading} />
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
              <Line label="Trading" value={pnl.trading} help={LINE_HELP.tradingTotal} />
              <Line label="Total = NAV" value={pnl.total} strong help={LINE_HELP.nav} />
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}