// Every trade the book has made, newest first: options, spot, and option expiries.
// Notional / USD are ours (+ bought, − sold); cash is CNY paid (−) or received (+).

import { fmtClock, fmtDay, fmtSigned, fmtSpot } from "../format.js";

export default function BlotterTable({ blotter }) {
  if (blotter.length === 0) {
    return <p className="hint pad">No trades yet.</p>;
  }
  // The option behind each position id, so settlement rows can say what expired
  const optionById = {};
  for (const t of blotter) {
    if (t.type === "option" && optionById[t.position_id] === undefined) optionById[t.position_id] = t;
  }
  const rows = blotter.slice().reverse();
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th className="text">Note</th>
            <th>Pos #</th>
            <th>Our USD</th>
            <th>Option</th>
            <th>Price / spot</th>
            <th>Cash CNY</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => {
            const amount = t.notional !== undefined ? t.notional : t.usd_amount;
            let option = "";
            if (t.type === "option") {
              option = `${fmtSpot(t.strike)} ${t.is_call ? "C" : "P"} ${fmtDay(t.expiry)}`;
            } else if (t.type === "package") {
              option = `${t.kind} package, ${fmtDay(t.expiry)}`;
            } else if (t.type === "settlement" && optionById[t.position_id]) {
              const o = optionById[t.position_id];
              option = `${fmtSpot(o.strike)} ${o.is_call ? "C" : "P"} expired`;
            } else if (t.type === "net" && optionById[t.position_ids[0]]) {
              const o = optionById[t.position_ids[0]];
              option = `${fmtSpot(o.strike)} ${o.is_call ? "C" : "P"} ${fmtDay(o.expiry)}`;
            }
            // Which positions the row is about; a netting row lists the merged ids
            let ids = "";
            if (t.type === "net") {
              ids = t.position_ids.join(" + ") + (t.kept_id === null ? " → closed" : ` → #${t.kept_id}`);
            } else if (t.type === "package") {
              ids = t.position_ids.join(", ");
            } else if (t.position_id !== undefined && t.position_id !== null) {
              ids = String(t.position_id);
            }
            let price = "";
            if (t.type === "option") {
              price = (t.price * 1e4).toFixed(1) + " pips";
            } else if (t.type === "spot" || t.type === "settlement") {
              price = fmtSpot(t.spot);
            }
            return (
              <tr key={i}>
                <td>{fmtDay(t.time)} {fmtClock(t.time)}</td>
                <td>{t.type}</td>
                <td className="text">{t.note || ""}</td>
                <td>{ids}</td>
                <td className="mono">{amount !== undefined && amount !== null ? fmtSigned(amount) : ""}</td>
                <td className="mono">{option}</td>
                <td className="mono">{price}</td>
                <td className="mono">{t.cash_cny !== undefined ? fmtSigned(t.cash_cny) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="hint">Package rows group the option legs listed with them; their cash is in the legs.</p>
    </div>
  );
}