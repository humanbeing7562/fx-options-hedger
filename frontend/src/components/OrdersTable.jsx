// Every option order sent to the session: pending (waiting for a later fixing),
// booked, cancelled or rejected. Pending ones can be cancelled.

import { fmtClock, fmtDay, fmtShort, fmtSigned, fmtSpot, fmtVol, pnlClass } from "../format.js";

function strikeText(order) {
  if (order.strike_value !== undefined) {
    if (typeof order.strike === "string") return `${order.strike} = ${fmtSpot(order.strike_value)}`;
    return fmtSpot(order.strike_value);
  }
  return typeof order.strike === "string" ? order.strike : fmtSpot(order.strike);
}

// The vol an order was agreed or booked at ("" when a premium was agreed instead)
function volText(order) {
  if (order.booked_vol !== undefined && order.booked_vol !== null) return fmtVol(order.booked_vol, 3);
  if (order.vol !== undefined && order.vol !== null) return fmtVol(order.vol, 3);
  return "";
}

export default function OrdersTable({ orders, busy, onCancel }) {
  if (orders.length === 0) {
    return <p className="hint pad">No option orders yet. Spot trades are in the blotter.</p>;
  }
  const rows = orders.slice().reverse();      // newest first
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>#</th>
            <th>Status</th>
            <th>With</th>
            <th>Order</th>
            <th>We</th>
            <th>USD</th>
            <th>Strike</th>
            <th>Expiry</th>
            <th>For</th>
            <th>Vol</th>
            <th>Price (pips)</th>
            <th>Mid (pips)</th>
            <th>Edge CNY</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => (
            <tr key={o.order_id}>
              <td>{o.order_id}</td>
              <td><span className={"tag status-" + o.status}>{o.status}</span>{o.reason && <div className="muted">{o.reason}</div>}</td>
              <td>{o.tag}</td>
              <td>{o.tag === "client" ? `client ${o.side}s` : `we ${o.side}`} {o.is_call ? "call" : "put"}</td>
              <td>{o.our_side}</td>
              <td className="mono">{fmtShort(o.notional)}</td>
              <td className="mono">{strikeText(o)}</td>
              <td>{fmtDay(o.expiry)}</td>
              <td>{fmtDay(o.time)} {fmtClock(o.time)}</td>
              <td className="mono">{volText(o)}</td>
              <td className="mono">{o.booked_price !== undefined ? (o.booked_price * 1e4).toFixed(1) : o.price !== null ? (o.price * 1e4).toFixed(1) : o.vol !== null ? "at vol" : "mid"}</td>
              <td className="mono">{o.mid !== undefined ? (o.mid * 1e4).toFixed(1) : ""}</td>
              <td className={"mono " + pnlClass(o.edge_cny)}>{o.edge_cny !== undefined ? fmtSigned(o.edge_cny) : ""}</td>
              <td>
                {o.status === "pending" && (
                  <button className="small danger" disabled={busy} onClick={() => onCancel(o.order_id)}>Cancel</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}