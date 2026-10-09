// Every option on the book, valued at this fixing. Notional is OURS: + long, − short.

import { fmtDay, fmtNum, fmtShort, fmtSigned, fmtSpot, fmtVol, gamma100, vegaPt, thetaDay } from "../format.js";

export default function PositionsTable({ state }) {
  if (state.positions.length === 0) {
    return <p className="hint pad">No options on the book.</p>;
  }
  // Shortest expiry first
  const rows = state.positions.slice().sort((a, b) => a.T - b.T || a.position_id - b.position_id);
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>#</th>
            <th>From</th>
            <th>Our side</th>
            <th>Type</th>
            <th>USD</th>
            <th>Strike</th>
            <th>Expiry</th>
            <th>Days</th>
            <th>Bucket</th>
            <th>Vol</th>
            <th>Value CNY</th>
            <th>Delta USD</th>
            <th>Gamma 100p</th>
            <th>Vega / pt</th>
            <th>Theta / day</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.position_id}>
              <td>{p.position_id}</td>
              <td><span className={"tag " + (p.source === "hedge" ? "blue" : "grey")}>{p.source}</span></td>
              <td className={p.notional > 0 ? "pos" : "neg"}>{p.notional > 0 ? "long" : "short"}</td>
              <td>{p.is_call ? "Call" : "Put"}</td>
              <td className="mono">{fmtShort(Math.abs(p.notional))}</td>
              <td className="mono">{fmtSpot(p.strike)}</td>
              <td>{fmtDay(p.expiry_ts)}</td>
              <td className="mono">{p.days_to_expiry.toFixed(1)}</td>
              <td>{p.bucket}</td>
              <td className="mono">{fmtVol(p.vol)}</td>
              <td className="mono">{fmtSigned(p.value)}</td>
              <td className="mono">{fmtSigned(p.delta)}</td>
              <td className="mono">{fmtSigned(gamma100(p.gamma))}</td>
              <td className="mono">{fmtSigned(vegaPt(p.vega))}</td>
              <td className="mono">{fmtSigned(thetaDay(p.theta))}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan="10">Options total</td>
            <td className="mono">{fmtSigned(state.options_value_cny)}</td>
            <td className="mono">{fmtSigned(state.greeks.option_delta)}</td>
            <td className="mono">{fmtSigned(gamma100(state.greeks.gamma))}</td>
            <td className="mono">{fmtSigned(vegaPt(state.greeks.vega))}</td>
            <td className="mono">{fmtSigned(thetaDay(state.greeks.theta))}</td>
          </tr>
          <tr>
            <td colSpan="10">USD spot position {fmtSigned(state.hedge_usd)} USD · CNY cash {fmtNum(state.cash_cny)}</td>
            <td className="mono">{fmtSigned(state.hedge_value_cny)}</td>
            <td className="mono">{fmtSigned(state.hedge_usd)}</td>
            <td colSpan="3"></td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}