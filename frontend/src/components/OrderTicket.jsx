// The order ticket: options and spot, from a client or as our own hedge.
//
// Client trade: "side" is what the CLIENT does (client buys -> we sell).
// Hedge trade:  "side" is what WE do, in the market.
// The server works out our side from the tag, so the ticket only sends what was chosen.
//
// For options, the ticket asks the server for a live quote whenever a field changes and
// shows the price, the Greeks of the trade, and what the trade does to the book.

import { useEffect, useState } from "react";
import { api } from "../api.js";
import {
  fmtClock, fmtDay, fmtNum, fmtSigned, fmtSpot, fmtVol, dateOf,
  gamma100, vegaPt, thetaDay, pnlClass,
} from "../format.js";

export const TENORS = [
  ["1W", 7], ["2W", 14], ["3W", 21], ["1M", 30], ["2M", 61], ["3M", 91], ["6M", 182], ["9M", 273], ["1Y", 365],
];

const STRIKES = [
  ["ATM", "ATM (delta-neutral)"],
  ["ATMF", "ATM forward"],
  ["25D", "25 delta"],
  ["10D", "10 delta"],
  ["delta", "Other delta…"],
  ["strike", "Strike…"],
];

// How long to wait after the last keystroke before asking for a quote
const QUOTE_DELAY_MS = 300;

export default function OrderTicket({ sessionId, state, fixings, prefill, busy, onSubmitOption, onSubmitSpot }) {
  const [kind, setKind] = useState("option");        // "option" or "spot"
  const [tag, setTag] = useState("client");          // "client" or "hedge"
  const [side, setSide] = useState("buy");
  const [isCall, setIsCall] = useState(true);
  const [notionalM, setNotionalM] = useState("10");  // USD millions

  const [tenorMode, setTenorMode] = useState("1M");  // a tenor label, "custom" (days) or "expiry" (exact)
  const [customDays, setCustomDays] = useState("45");
  const [expiry, setExpiry] = useState("");

  const [strikeMode, setStrikeMode] = useState("ATM");
  const [customDelta, setCustomDelta] = useState("35");
  const [customStrike, setCustomStrike] = useState("");

  const [priceMode, setPriceMode] = useState("our"); // "our" (bid/ask), "mid", "vol" (agreed vol), "custom" (agreed pips)
  const [customVolPct, setCustomVolPct] = useState("");  // agreed vol in percent, e.g. 2.25
  const [customPips, setCustomPips] = useState("");  // option premium in pips (CNY per USD x 10,000)
  const [spotPriceMode, setSpotPriceMode] = useState("mid");   // "mid", "spread", "custom"
  const [spreadPips, setSpreadPips] = useState("5");
  const [customRate, setCustomRate] = useState("");

  const [when, setWhen] = useState("now");           // "now" or a later fixing time

  // The last quote reply, with the order it was for: {key, data} or {key, error}
  const [quoted, setQuoted] = useState(null);

  // Filled from elsewhere on the page (a recommended hedge leg, a market pillar, the delta hedge)
  useEffect(() => {
    if (!prefill) return;
    if (prefill.kind) setKind(prefill.kind);
    if (prefill.tag) setTag(prefill.tag);
    if (prefill.side) setSide(prefill.side);
    if (prefill.isCall !== undefined) setIsCall(prefill.isCall);
    if (prefill.notional !== undefined) setNotionalM(String(prefill.notional / 1e6));      // exact, so a leg books exactly as planned
    if (prefill.expiry) {
      setTenorMode("expiry");
      setExpiry(prefill.expiry);
    }
    if (prefill.tenorDays) {
      let label = null;
      for (const [name, days] of TENORS) {
        if (days === prefill.tenorDays) label = name;
      }
      if (label) {
        setTenorMode(label);
      } else {
        setTenorMode("custom");
        setCustomDays(String(prefill.tenorDays));
      }
    }
    if (prefill.strike !== undefined) {
      setStrikeMode("strike");
      setCustomStrike(String(prefill.strike));
    }
    if (prefill.strikeLabel) setStrikeMode(prefill.strikeLabel);
    if (prefill.tag === "hedge") {
      setPriceMode("mid");
      setSpotPriceMode("mid");
    }
    setWhen("now");
  }, [prefill]);

  // A later order books at that fixing's price, so today's bid / ask can't be locked in
  useEffect(() => {
    if (when !== "now" && priceMode === "our") setPriceMode("mid");
  }, [when, priceMode]);

  // A chosen later fixing that the clock has reached: back to "now"
  useEffect(() => {
    if (when !== "now" && when <= state.time) setWhen("now");
  }, [when, state.time]);

  // ---- building what gets sent ----

  function strikeValue() {
    if (strikeMode === "delta") return `${customDelta}D`;
    if (strikeMode === "strike") return Number(customStrike);
    return strikeMode;
  }

  // The option order without a price (the quote decides the price). An agreed vol is part
  // of it, so the quote also shows the premium that vol gives.
  function baseOrder() {
    const order = {
      tag,
      side,
      is_call: isCall,
      notional: Number(notionalM) * 1e6,
      strike: strikeValue(),
    };
    if (tenorMode === "expiry") {
      order.expiry = expiry;
    } else if (tenorMode === "custom") {
      order.tenor_days = Number(customDays);
    } else {
      for (const [name, days] of TENORS) {
        if (name === tenorMode) order.tenor_days = days;
      }
    }
    if (when !== "now") order.time = when;
    if (priceMode === "vol" && Number(customVolPct) > 0) order.vol = Number(customVolPct) / 100;
    return order;
  }

  function optionReady() {
    if (!(Number(notionalM) > 0)) return false;
    if (strikeMode === "strike" && !(Number(customStrike) > 0)) return false;
    if (strikeMode === "delta" && !(Number(customDelta) > 0 && Number(customDelta) < 100)) return false;
    if (tenorMode === "custom" && !(Number(customDays) > 0)) return false;
    if (tenorMode === "expiry" && !expiry) return false;
    return true;
  }

  // Live quote: asked again whenever the order (or the clock) changes.
  // A quote only counts for the exact order it was made for: while a new one is on its
  // way, there is no quote (so an old price can never be booked for a changed order).
  const orderKey = JSON.stringify(optionReady() ? baseOrder() : null) + state.time + state.index;
  const quote = quoted && quoted.key === orderKey && quoted.data ? quoted.data : null;
  const quoteError = quoted && quoted.key === orderKey && quoted.error ? quoted.error : null;
  useEffect(() => {
    if (kind !== "option" || !optionReady()) return;
    const key = orderKey;
    let cancelled = false;
    const timer = setTimeout(() => {
      api.quote(sessionId, baseOrder())
        .then((q) => {
          if (!cancelled) setQuoted({ key, data: q });
        })
        .catch((error) => {
          if (!cancelled) setQuoted({ key, error: error.message });
        });
    }, QUOTE_DELAY_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderKey, kind, sessionId]);

  // Our side, worked out the same way as the server does
  let ourSide = side;
  if (tag === "client") ourSide = side === "buy" ? "sell" : "buy";
  const ourSign = ourSide === "buy" ? 1 : -1;
  const notional = Number(notionalM) * 1e6;

  // Option price that will be sent (undefined = mid at booking)
  // An agreed premium typed in (CNY per USD), or null when the box is empty
  function customPrice() {
    if (customPips === "") return null;
    return Number(customPips) / 1e4;
  }

  // "Our quote" books at the bid or ask VOL (the server prices it), so the order records
  // its vol; an agreed premium is sent as a price; "Agreed vol" is already in baseOrder()
  function submitOption(event) {
    event.preventDefault();
    const order = baseOrder();
    if (priceMode === "our" && quote) order.vol = quote.our_vol;
    if (priceMode === "custom") order.price = customPrice();
    onSubmitOption(order);
  }

  // ---- spot ----

  // The spot rate this trade would book at (NaN while an agreed rate is still empty)
  function spotPrice() {
    if (spotPriceMode === "custom") return customRate === "" ? NaN : Number(customRate);
    if (spotPriceMode === "spread") {
      // With a client the spread is ours: we sell above spot and buy below it.
      // In the market (a hedge) we pay it: we buy above spot and sell below it.
      const pips = Number(spreadPips) / 1e4;
      let above = ourSide === "sell";
      if (tag === "hedge") above = !above;
      return above ? state.spot + pips : state.spot - pips;
    }
    return state.spot;
  }

  function submitSpot(event) {
    event.preventDefault();
    const trade = { tag, side, notional };
    if (spotPriceMode !== "mid") trade.price = spotPrice();
    onSubmitSpot(trade);
  }

  // Later fixings an option order can wait for: the next 15, within this session
  // (the session's fixings are the data's fixings from its start to its end, in a row)
  const left = state.fixings - state.index - 1;
  const later = [];
  for (const t of fixings) {
    if (t > state.time && later.length < Math.min(15, left)) later.push(t);
  }

  // ---- what to show ----

  const clientLabel = kind === "spot" ? "USD" : isCall ? "USD calls" : "USD puts";
  const sideButtons = tag === "client"
    ? [["buy", `Client buys`], ["sell", `Client sells`]]
    : [["buy", "We buy"], ["sell", "We sell"]];

  let impact = null;
  if (kind === "option" && quote) {
    const g = quote.greeks_per_usd;
    impact = {
      delta: ourSign * notional * g.delta,
      gamma: ourSign * notional * gamma100(g.gamma),
      vega: ourSign * notional * vegaPt(g.vega),
      theta: ourSign * notional * thetaDay(g.theta),
    };
  }
  // The premium and vol this order would book at, for the quote box (null = not known yet)
  let sentPrice = null;
  let sentVol = null;
  if (quote) {
    if (priceMode === "our") {
      sentPrice = quote.our_price;
      sentVol = quote.our_vol;
    } else if (priceMode === "mid") {
      sentPrice = quote.mid;
      sentVol = quote.vol;
    } else if (priceMode === "vol" && quote.agreed_price !== null) {
      sentPrice = quote.agreed_price;
      sentVol = quote.agreed_vol;
    } else if (priceMode === "custom") {
      sentPrice = customPrice();
    }
  }
  const optionEdge = quote && sentPrice !== null ? -ourSign * notional * (sentPrice - quote.mid) : null;
  // Edge in vol points: selling above mid vol, or buying below it, is in our favour
  let edgeVol = null;
  if (quote && sentVol !== null) edgeVol = ourSign === -1 ? sentVol - quote.vol : quote.vol - sentVol;

  const spotEdge = (ourSign * notional) * (state.spot - spotPrice());

  return (
    <div className="card ticket">
      <div className="card-head">
        <h2>Order ticket</h2>
        <div className="seg">
          <button className={kind === "option" ? "on" : ""} onClick={() => setKind("option")} type="button">Option</button>
          <button className={kind === "spot" ? "on" : ""} onClick={() => setKind("spot")} type="button">Spot</button>
        </div>
      </div>

      {state.done && (
        <p className="hint">The session has reached its last fixing: there is no next move for a trade to matter, so booking is off.</p>
      )}

      <div className="field">
        <span className="label">Who is it with?</span>
        <div className="seg wide">
          <button type="button" className={tag === "client" ? "on" : ""} onClick={() => setTag("client")}>Client order</button>
          <button type="button" className={tag === "hedge" ? "on" : ""} onClick={() => setTag("hedge")}>Our hedge (market)</button>
        </div>
      </div>

      <div className="field">
        <span className="label">Side</span>
        <div className="seg wide">
          {sideButtons.map(([value, label]) => (
            <button type="button" key={value} className={side === value ? "on" : ""} onClick={() => setSide(value)}>
              {label}
            </button>
          ))}
        </div>
        <div className={"our-side " + ourSide}>
          {tag === "client"
            ? `Client ${side}s ${clientLabel} → we ${ourSide}`
            : `We ${ourSide} ${clientLabel} in the market`}
        </div>
      </div>

      {kind === "option" ? (
        <form onSubmit={submitOption}>
          <div className="row">
            <div className="field">
              <span className="label">Type</span>
              <div className="seg">
                <button type="button" className={isCall ? "on" : ""} onClick={() => setIsCall(true)}>Call</button>
                <button type="button" className={!isCall ? "on" : ""} onClick={() => setIsCall(false)}>Put</button>
              </div>
            </div>
            <label>
              Notional (USD m)
              <input type="number" min="0" step="any" value={notionalM} onChange={(e) => setNotionalM(e.target.value)} />
            </label>
          </div>

          <div className="row">
            <label>
              Tenor
              <select value={tenorMode} onChange={(e) => setTenorMode(e.target.value)}>
                {TENORS.map(([name]) => (
                  <option key={name} value={name}>{name}</option>
                ))}
                <option value="custom">Days…</option>
                {tenorMode === "expiry" && <option value="expiry">Exact expiry</option>}
              </select>
            </label>
            {tenorMode === "custom" && (
              <label>
                Days
                <input type="number" min="1" value={customDays} onChange={(e) => setCustomDays(e.target.value)} />
              </label>
            )}
            {tenorMode === "expiry" && (
              <label>
                Expiry
                <input value={expiry.replace("T", " ").slice(0, 16)} readOnly />
              </label>
            )}
          </div>

          <div className="row">
            <label>
              Strike
              <select value={strikeMode} onChange={(e) => setStrikeMode(e.target.value)}>
                {STRIKES.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>
            {strikeMode === "delta" && (
              <label>
                Delta (1–99)
                <input type="number" min="1" max="99" value={customDelta} onChange={(e) => setCustomDelta(e.target.value)} />
              </label>
            )}
            {strikeMode === "strike" && (
              <label>
                Strike (CNY per USD)
                <input type="number" step="any" value={customStrike} onChange={(e) => setCustomStrike(e.target.value)} placeholder={fmtSpot(state.spot)} />
              </label>
            )}
          </div>

          <div className="row">
            <label>
              When
              <select value={when} onChange={(e) => setWhen(e.target.value)}>
                <option value="now">Now ({fmtClock(state.time)})</option>
                {later.map((t) => (
                  <option key={t} value={t}>
                    {dateOf(t) === dateOf(state.time) ? `Today ${fmtClock(t)}` : `${fmtDay(t)} ${fmtClock(t)}`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Price
              <select value={priceMode} onChange={(e) => setPriceMode(e.target.value)}>
                <option value="our" disabled={when !== "now"}>
                  {tag === "client" ? "Our quote (bid / ask)" : "Market (bid / ask)"}
                </option>
                <option value="mid">Mid at booking</option>
                <option value="vol">Agreed vol…</option>
                <option value="custom">Agreed premium (pips)…</option>
              </select>
            </label>
            {priceMode === "vol" && (
              <label>
                Vol (%)
                <input type="number" step="any" min="0" value={customVolPct} onChange={(e) => setCustomVolPct(e.target.value)} placeholder={quote ? (quote.vol * 100).toFixed(3) : ""} />
              </label>
            )}
            {priceMode === "custom" && (
              <label>
                Premium (pips)
                <input type="number" step="any" min="0" value={customPips} onChange={(e) => setCustomPips(e.target.value)} placeholder={quote ? (quote.mid * 1e4).toFixed(1) : ""} />
              </label>
            )}
          </div>
          {when !== "now" && (
            <p className="hint">
              Waits until {fmtDay(when)} {fmtClock(when)}.{" "}
              {priceMode === "vol"
                ? "Books at this vol, priced with that fixing's spot and rates."
                : priceMode === "custom" ? "Books at this premium." : "Books at that fixing's mid."}
            </p>
          )}

          <QuoteBox ready={optionReady()} quote={quote} error={quoteError} impact={impact} state={state} edge={optionEdge} edgeVol={edgeVol} sentPrice={sentPrice} sentVol={sentVol} notional={notional} />

          {state.policy !== "manual" && tag === "client" && (
            <p className="hint">With {state.policy.split("_").join(" ")} hedging, the book may hedge straight after a client trade.</p>
          )}
          <button className="primary full" type="submit" disabled={busy || state.done || !optionReady() || quoteError !== null || (priceMode === "our" && !quote) || (priceMode === "custom" && !(customPrice() > 0)) || (priceMode === "vol" && !(Number(customVolPct) > 0))}>
            {when === "now" ? "Book option" : "Place order"}
          </button>
        </form>
      ) : (
        <form onSubmit={submitSpot}>
          <div className="row">
            <label>
              Notional (USD m)
              <input type="number" min="0" step="any" value={notionalM} onChange={(e) => setNotionalM(e.target.value)} />
            </label>
            <label>
              Price
              <select value={spotPriceMode} onChange={(e) => setSpotPriceMode(e.target.value)}>
                <option value="mid">Spot (mid)</option>
                <option value="spread">{tag === "client" ? "Spot ± spread (ours)" : "Spot ± spread (we pay)"}</option>
                <option value="custom">Agreed rate…</option>
              </select>
            </label>
            {spotPriceMode === "spread" && (
              <label>
                Spread (pips)
                <input type="number" min="0" step="any" value={spreadPips} onChange={(e) => setSpreadPips(e.target.value)} />
              </label>
            )}
            {spotPriceMode === "custom" && (
              <label>
                Rate
                <input type="number" step="any" value={customRate} onChange={(e) => setCustomRate(e.target.value)} placeholder={fmtSpot(state.spot)} />
              </label>
            )}
          </div>
          <table className="kv">
            <tbody>
              <tr><td>Spot now</td><td className="mono">{fmtSpot(state.spot)}</td></tr>
              <tr><td>Trade rate</td><td className="mono">{fmtSpot(spotPrice())}</td></tr>
              <tr><td>Our USD</td><td className="mono">{fmtSigned(ourSign * notional)}</td></tr>
              <tr><td>CNY paid (−) / received (+)</td><td className="mono">{fmtSigned(-ourSign * notional * spotPrice())}</td></tr>
              <tr><td>{tag === "client" ? "Edge vs spot" : "Cost vs spot"}</td><td className={"mono " + pnlClass(spotEdge)}>{fmtSigned(spotEdge)} CNY</td></tr>
              <tr><td>Book delta</td><td className="mono">{fmtSigned(state.greeks.delta)} → {fmtSigned(state.greeks.delta + ourSign * notional)} USD</td></tr>
            </tbody>
          </table>
          <button className="primary full" type="submit" disabled={busy || state.done || !(notional > 0) || !(spotPrice() > 0)}>
            Book spot
          </button>
        </form>
      )}
    </div>
  );
}

// A warning when the quote's vol comes from beyond the quoted market: past the 10D
// pillars (the SABR wing) or past the 1Y tenor (the 1Y smile held flat)
function extrapolationNote(quote) {
  const delta = Math.abs(quote.greeks_per_usd.delta);
  const notes = [];
  if (delta < 0.05 || delta > 0.95) notes.push("deep in the wing, beyond the 10D quotes (vol from the SABR fit)");
  if (quote.days_to_expiry > 366) notes.push("beyond the 1Y quotes (the 1Y smile is held flat)");
  if (notes.length === 0) return null;
  return "Extrapolated: " + notes.join("; ") + ".";
}

// The live quote for an option order, and what it does to the book
function QuoteBox({ ready, quote, error, impact, state, edge, edgeVol, sentPrice, sentVol, notional }) {
  if (!ready) return <div className="quote muted">Fill in notional, tenor and strike for a quote.</div>;
  if (error) return <div className="quote error">{error}</div>;
  if (!quote) return <div className="quote muted">Quoting…</div>;
  const g = state.greeks;
  const spot = state.spot;
  return (
    <div className="quote">
      <div className="quote-head">
        <span>Strike <b className="mono">{fmtSpot(quote.strike)}</b></span>
        <span>Fwd <b className="mono">{fmtSpot(quote.forward)}</b></span>
        <span>{quote.days_to_expiry < 10 ? quote.days_to_expiry.toFixed(1) : quote.days_to_expiry.toFixed(0)} days</span>
      </div>
      {extrapolationNote(quote) && <p className="hint warn-text">{extrapolationNote(quote)}</p>}
      <table className="quote-table">
        <thead>
          <tr><th></th><th>Bid</th><th>Mid</th><th>Ask</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>Vol</td>
            <td className="mono">{fmtVol(quote.bid_vol, 3)}</td>
            <td className="mono">{fmtVol(quote.vol, 3)}</td>
            <td className="mono">{fmtVol(quote.ask_vol, 3)}</td>
          </tr>
          <tr>
            <td>Pips</td>
            <td className="mono">{(quote.bid * 1e4).toFixed(1)}</td>
            <td className="mono">{(quote.mid * 1e4).toFixed(1)}</td>
            <td className="mono">{(quote.ask * 1e4).toFixed(1)}</td>
          </tr>
        </tbody>
      </table>
      <table className="kv">
        <tbody>
          {sentPrice === null ? (
            <tr><td colSpan="2" className="muted">Enter the agreed vol or premium to see the premium and edge.</td></tr>
          ) : (
            <>
              <tr>
                <td>Premium at {sentVol !== null && `${fmtVol(sentVol, 3)} = `}{(sentPrice * 1e4).toFixed(1)} pips</td>
                <td className="mono">{fmtNum(sentPrice * notional)} CNY {quote.our_side === "sell" ? "received" : "paid"}</td>
              </tr>
              <tr>
                <td>Edge vs mid{edgeVol !== null && ` (${fmtSigned(edgeVol * 100, 3)} vol pt)`}</td>
                <td className={"mono " + pnlClass(edge)}>{fmtSigned(edge)} CNY</td>
              </tr>
            </>
          )}
        </tbody>
      </table>
      <table className="impact">
        <thead>
          <tr><th>Book</th><th>Now</th><th>This trade</th><th>After</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>Delta (USD)</td>
            <td className="mono">{fmtSigned(g.delta)}</td>
            <td className="mono">{fmtSigned(impact.delta)}</td>
            <td className="mono">{fmtSigned(g.delta + impact.delta)}</td>
          </tr>
          <tr>
            <td>Gamma (USD / 100 pips)</td>
            <td className="mono">{fmtSigned(gamma100(g.gamma))}</td>
            <td className="mono">{fmtSigned(impact.gamma)}</td>
            <td className="mono">{fmtSigned(gamma100(g.gamma) + impact.gamma)}</td>
          </tr>
          <tr>
            <td>Vega (CNY / vol pt)</td>
            <td className="mono">{fmtSigned(vegaPt(g.vega))}</td>
            <td className="mono">{fmtSigned(impact.vega)}</td>
            <td className="mono">{fmtSigned(vegaPt(g.vega) + impact.vega)}</td>
          </tr>
          <tr>
            <td>Theta (CNY / day)</td>
            <td className="mono">{fmtSigned(thetaDay(g.theta))}</td>
            <td className="mono">{fmtSigned(impact.theta)}</td>
            <td className="mono">{fmtSigned(thetaDay(g.theta) + impact.theta)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}