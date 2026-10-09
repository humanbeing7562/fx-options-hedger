// What happened, newest first: every fixing step and every trade, one line each.

import { fmtClock, fmtDay, fmtShort, fmtSigned, pnlClass } from "../format.js";

// Short text for what a policy or a manual hedge traded
function hedgeText(hedges) {
  if (!hedges) return "";
  const parts = [];
  if (hedges.packages && hedges.packages.length > 0) {
    parts.push(`${hedges.packages.length} smile package${hedges.packages.length === 1 ? "" : "s"}`);
  }
  if (hedges.spot) {
    parts.push(`${hedges.spot.usd_amount > 0 ? "bought" : "sold"} ${fmtShort(Math.abs(hedges.spot.usd_amount))} USD spot`);
  }
  if (parts.length === 0) return "";
  return "hedged: " + parts.join(", ") + (hedges.reason ? ` (${hedges.reason})` : "");
}

function describe(record) {
  if (record.type === "start") return "Session started";
  if (record.type === "step") {
    const parts = [`Fixing: spot ${fmtSigned(record.pnl_explain.dS_pips, 1)} pips`];
    if (record.settled.length > 0) parts.push(`${record.settled.length} option${record.settled.length === 1 ? "" : "s"} expired`);
    if (record.orders_booked.length > 0) parts.push(`${record.orders_booked.length} waiting order${record.orders_booked.length === 1 ? "" : "s"} booked`);
    const hedge = hedgeText(record.hedges);
    if (hedge) parts.push(hedge);
    return parts.join(" · ");
  }
  if (record.type === "order") {
    const o = record.orders_booked[0];
    const who = o.tag === "client" ? `client ${o.side}s` : `we ${o.side}`;
    let text = `Order #${o.order_id}: ${who} ${fmtShort(o.notional)} ${o.is_call ? "calls" : "puts"} (${o.status})`;
    const hedge = hedgeText(record.hedges);
    if (hedge) text += " · " + hedge;
    return text;
  }
  if (record.type === "spot") {
    const t = record.trade;
    const who = t.tag === "client" ? `client ${t.side}s` : `we ${t.side}`;
    let text = `Spot: ${who} ${fmtShort(t.notional)} USD at ${t.price.toFixed(4)}`;
    const hedge = hedgeText(record.hedges);
    if (hedge) text += " · " + hedge;
    return text;
  }
  if (record.type === "hedge") {
    return hedgeText(record.hedges) || "Hedge: nothing to trade";
  }
  return record.type;
}

export default function StepLog({ history }) {
  const rows = history.slice().reverse();
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Time</th>
            <th className="text">What happened</th>
            <th>Market P&amp;L</th>
            <th>Trading P&amp;L</th>
            <th>NAV</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={history.length - 1 - i}>
              <td>{fmtDay(r.time)} {fmtClock(r.time)}</td>
              <td className="text">{describe(r)}</td>
              <td className={"mono " + pnlClass(r.market_pnl_cny || 0)}>{r.market_pnl_cny !== undefined ? fmtSigned(r.market_pnl_cny) : ""}</td>
              <td className={"mono " + pnlClass(r.trade_pnl_cny || 0)}>{r.trade_pnl_cny !== undefined ? fmtSigned(r.trade_pnl_cny) : ""}</td>
              <td className={"mono " + pnlClass(r.nav_cny)}>{fmtSigned(r.nav_cny)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
