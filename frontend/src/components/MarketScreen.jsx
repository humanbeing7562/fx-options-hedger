// The market at this fixing: forwards, rates and the vol smile per tenor.
// The data has quoted vols only (ATM, 25D and 10D calls and puts); every premium and
// Greek here is priced by the engine at the quoted vol. Click a tenor to see its pillars;
// "Use" sends that tenor and strike to the order ticket.

import { useState } from "react";
import { fmtDay, fmtNum, fmtRate, fmtSigned, fmtSpot, fmtVol, gamma100, vegaPt, thetaDay } from "../format.js";

const MILLION = 1e6;

export default function MarketScreen({ market, onUse }) {
  const [open, setOpen] = useState(null);
  if (!market) return <p className="hint pad">Loading the market…</p>;

  return (
    <div className="table-wrap">
      <p className="hint">
        Spot <b className="mono">{fmtSpot(market.spot)}</b> · overnight CNY {fmtRate(market.rate_cny_on)} · USD {fmtRate(market.rate_usd_on)}.
        RR = 25D call vol − 25D put vol; BF = average 25D vol − ATM vol.
        Booked options use the model surface: exact at ATM and 25D; the 10D wings come from
        the SABR fit, within about 0.1 vol pt of these quotes.
      </p>
      <table className="data">
        <thead>
          <tr>
            <th>Tenor</th>
            <th>Expiry</th>
            <th>Forward</th>
            <th>Fwd pts</th>
            <th>CNY rate</th>
            <th>USD rate</th>
            <th>10P</th>
            <th>25P</th>
            <th>ATM</th>
            <th>25C</th>
            <th>10C</th>
            <th>25 RR</th>
            <th>25 BF</th>
          </tr>
        </thead>
        <tbody>
          {market.tenors.map((t) => {
            const isOpen = open === t.tenor;
            const rows = [
              <tr key={t.tenor} className="clickable" onClick={() => setOpen(isOpen ? null : t.tenor)}>
                <td><b>{isOpen ? "▾" : "▸"} {t.tenor}</b></td>
                <td>{fmtDay(t.expiry)}</td>
                <td className="mono">{fmtSpot(t.forward)}</td>
                <td className="mono">{fmtSigned(t.forward_points_pips, 1)}</td>
                <td className="mono">{fmtRate(t.rate_cny)}</td>
                <td className="mono">{fmtRate(t.rate_usd)}</td>
                {t.pillars.map((p) => (
                  <td key={p.pillar} className="mono">{fmtVol(p.vol)}</td>
                ))}
                <td className="mono">{fmtSigned(t.rr25 * 100, 2)}</td>
                <td className="mono">{fmtSigned(t.bf25 * 100, 2)}</td>
              </tr>,
            ];
            if (isOpen) {
              rows.push(
                <tr key={t.tenor + "-detail"} className="detail">
                  <td colSpan="13">
                    <table className="data compact">
                      <thead>
                        <tr>
                          <th>Pillar</th>
                          <th>Strike</th>
                          <th>Vol</th>
                          <th>Premium (pips)</th>
                          <th>Delta</th>
                          <th>Per 1m USD: premium CNY</th>
                          <th>gamma 100p</th>
                          <th>vega / pt</th>
                          <th>theta / day</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {t.pillars.map((p) => {
                          const label = p.pillar === "ATM" ? "ATM" : p.pillar.slice(0, 2) + "D";
                          return (
                            <tr key={p.pillar}>
                              <td>{p.pillar}{p.pillar === "ATM" ? " (call)" : ""}</td>
                              <td className="mono">{fmtSpot(p.strike)}</td>
                              <td className="mono">{fmtVol(p.vol)}</td>
                              <td className="mono">{(p.price * 1e4).toFixed(1)}</td>
                              <td className="mono">{p.delta.toFixed(3)}</td>
                              <td className="mono">{fmtNum(p.price * MILLION)}</td>
                              <td className="mono">{fmtNum(gamma100(p.gamma) * MILLION)}</td>
                              <td className="mono">{fmtNum(vegaPt(p.vega) * MILLION)}</td>
                              <td className="mono">{fmtNum(thetaDay(p.theta) * MILLION)}</td>
                              <td>
                                <button
                                  className="small"
                                  onClick={() => onUse({ kind: "option", tenorDays: t.days, strikeLabel: label, isCall: p.is_call })}
                                >
                                  Use
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </td>
                </tr>
              );
            }
            return rows;
          })}
        </tbody>
      </table>
    </div>
  );
}
