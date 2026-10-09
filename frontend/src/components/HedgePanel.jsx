// Hedging suggestions. Nothing here trades by itself: each suggestion can be booked as
// it stands, or sent to the order ticket to change and book by hand.
//
//   Delta: one spot trade that brings the book's delta to zero.
//   Smile: for every expiry bucket, a mix of three packages at that bucket's expiry that
//          cancels its vega, vanna and volga (and gamma, with a 4th short-dated straddle
//          when the bucket's options expire well before the hedge would):
//            ATM straddle       -> vega and gamma
//            25D risk reversal  -> vanna (exposure to the skew)
//            25D butterfly      -> volga (exposure to the wings)

import { fmtDay, fmtExpiry, fmtShort, fmtSigned, fmtSpot, gamma100, vegaPt, vannaPt, volgaPt } from "../format.js";

const PACKAGE_INFO = {
  ATM: ["ATM straddle", "vega, gamma"],
  RR: ["25D risk reversal", "vanna (skew)"],
  BF: ["25D butterfly", "volga (wings)"],
  GAMMA: ["Short-dated straddle", "gamma"],
};

// Packages smaller than this (USD) are not booked by the server either
const MIN_NOTIONAL = 1;

export default function HedgePanel({ state, recommend, limits, busy, onDeltaHedge, onBookAll, onSendToTicket }) {
  const delta = state.greeks.delta;
  const flat = Math.abs(delta) < 1;
  const ourSide = delta > 0 ? "sell" : "buy";
  const overLimit = Math.abs(delta) > limits.delta_usd;

  const buckets = recommend ? recommend.buckets : [];
  let packageCount = 0;
  for (const item of buckets) {
    if (item.packages) {
      for (const kind in item.packages) {
        if (Math.abs(item.packages[kind].notional) >= MIN_NOTIONAL) packageCount += 1;
      }
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Hedge suggestions</h2>
      </div>

      <div className={"delta-box" + (overLimit ? " warn" : "")}>
        <div>
          <div className="label">Delta</div>
          {flat ? (
            <div>Book delta is flat.</div>
          ) : (
            <div>
              Book is <b>{delta > 0 ? "long" : "short"} {fmtShort(Math.abs(delta))} USD</b>:
              {" "}<b>{ourSide} {fmtShort(Math.abs(delta))} USD</b> spot at {fmtSpot(state.spot)} to flatten.
              {overLimit && <span className="tag red">over limit</span>}
            </div>
          )}
        </div>
        <div className="buttons">
          <button className="small" disabled={state.done || busy || flat} onClick={onDeltaHedge}>Flatten now</button>
          <button
            className="small"
            disabled={state.done || busy || flat}
            onClick={() => onSendToTicket({ kind: "spot", tag: "hedge", side: ourSide, notional: Math.abs(delta) })}
          >
            To ticket
          </button>
        </div>
      </div>

      <div className="label section-label">Smile (vega / vanna / volga by bucket)</div>
      {buckets.length === 0 && <p className="hint">No options on the book: nothing to smile-hedge.</p>}

      {buckets.map((item) => (
        <div className="bucket" key={item.bucket}>
          <div className="bucket-head">
            <b>{item.bucket}</b>
            <span className="muted">
              {item.positions} position{item.positions === 1 ? "" : "s"}
              {item.hedge_expiry_ts && ` · hedge expiry ${fmtDay(item.hedge_expiry_ts)} (${item.hedge_days_left.toFixed(0)}d)`}
            </span>
          </div>
          <div className="bucket-greeks">
            <span>Vega <b className="mono">{fmtSigned(vegaPt(item.before.vega))}</b></span>
            <span>Vanna <b className="mono">{fmtSigned(vannaPt(item.before.vanna))}</b></span>
            <span>Volga <b className="mono">{fmtSigned(volgaPt(item.before.volga))}</b></span>
            <span>Gamma 100p <b className="mono">{fmtSigned(gamma100(item.before.gamma))}</b></span>
          </div>

          {item.skipped ? (
            <p className="hint">Not hedged: {item.skipped}</p>
          ) : (
            <div className="packages">
              {Object.keys(item.packages).map((kind) => {
                const pkg = item.packages[kind];
                const info = PACKAGE_INFO[kind] || [kind, ""];
                const expiryTs = kind === "GAMMA" ? item.gamma_expiry_ts : item.hedge_expiry_ts;
                if (Math.abs(pkg.notional) < MIN_NOTIONAL) {
                  return (
                    <div key={kind} className="package dim">
                      <b>{info[0]}</b> <span className="muted">({info[1]})</span>: not needed
                    </div>
                  );
                }
                return (
                  <div key={kind} className="package">
                    <div className="package-line">
                      <span>
                        <b>{info[0]}</b> <span className="muted">({info[1]})</span>
                      </span>
                      <span className="mono">
                        <span className={pkg.notional > 0 ? "pos" : "neg"}>{pkg.notional > 0 ? "buy" : "sell"}</span>
                        {" "}{fmtExpiry(expiryTs)} · {fmtShort(Math.abs(pkg.notional))} · {fmtSigned(-pkg.notional * pkg.mid_price)} CNY
                      </span>
                    </div>
                    <div className="legs">
                      {pkg.legs.map((leg, i) => {
                        const legNotional = pkg.notional * leg.ratio;
                        const legSide = legNotional > 0 ? "buy" : "sell";
                        return (
                          <button
                            key={i}
                            className="leg"
                            disabled={state.done || busy}
                            title="Send this leg to the order ticket"
                            onClick={() => onSendToTicket({
                              kind: "option",
                              tag: "hedge",
                              side: legSide,
                              isCall: leg.is_call,
                              notional: Math.abs(legNotional),
                              strike: leg.strike,
                              expiry: expiryTs,
                            })}
                          >
                            {legSide} {fmtExpiry(expiryTs)} {fmtShort(Math.abs(legNotional))} {fmtSpot(leg.strike)} {leg.is_call ? "C" : "P"} →
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ))}

      <button className="primary full" disabled={state.done || busy || (packageCount === 0 && flat)} onClick={onBookAll}>
        Book whole plan ({packageCount} package{packageCount === 1 ? "" : "s"} + delta)
      </button>
      <p className="hint">
        A butterfly here is long the two 25D wings and short a larger ATM body, so its premium
        can be paid or received whichever way it is traded. Hedges trade at mid. The plan is re-checked by the server at booking: if the book changed since
        it was shown, nothing is booked and a fresh plan appears.
      </p>
    </div>
  );
}