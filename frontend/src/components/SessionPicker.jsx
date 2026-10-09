// The first screen: open a session started earlier in this browser, or start a new one.
// Session ids are random and only this browser remembers which ones it made.

import { useEffect, useState } from "react";
import { dateOf, fmtDate, fmtDay } from "../format.js";

const POLICY_HELP = {
  manual: "You do all the hedging. The book only trades when you tell it to.",
  end_of_day: "Delta flattened automatically at every fixing and after every client trade; smile (vega, vanna, volga) hedged at each day's last fixing.",
  threshold: "Hedged automatically whenever delta or a bucket's vega goes over the limits below.",
};

// The distinct dates in the fixings list, in order
function datesOf(fixings) {
  const dates = [];
  for (const t of fixings) {
    const d = dateOf(t);
    if (dates.length === 0 || dates[dates.length - 1] !== d) dates.push(d);
  }
  return dates;
}

export default function SessionPicker({ themeToggle, fixings, sessions, busy, message, onCreate, onOpen, onDelete }) {
  const dates = datesOf(fixings);
  const [name, setName] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [policy, setPolicy] = useState("manual");
  const [deltaLimit, setDeltaLimit] = useState("1");        // USD millions
  const [vegaLimit, setVegaLimit] = useState("50000");     // CNY per vol point

  useEffect(() => {
    if (dates.length > 0 && startDate === "") {
      setStartDate(dates[0]);
      setEndDate(dates[dates.length - 1]);
    }
  }, [dates, startDate]);

  function changeStart(value) {
    setStartDate(value);
    if (endDate < value) setEndDate(value);
  }

  function submit(event) {
    event.preventDefault();
    // First fixing of the start day, last fixing of the end day
    let start = null;
    let end = null;
    for (const t of fixings) {
      if (start === null && dateOf(t) === startDate) start = t;
      if (dateOf(t) === endDate) end = t;
    }
    onCreate({
      name: name.trim() || `Desk ${fmtDate(start || "")}`,
      start,
      end,
      policy,
      limits: {
        delta_usd: Number(deltaLimit) * 1e6,
        vega_cny_per_volpt: Number(vegaLimit),
      },
    });
  }

  const endChoices = dates.filter((d) => d >= startDate);

  return (
    <div className="picker">
      <div className="picker-head">
        <h1>USD/CNY Options Desk</h1>
        {themeToggle}
      </div>
      <p className="intro">
        You run a sell-side FX options desk over real CFETS fixings (five a day, July–September 2026).
        Book what your clients ask for, then hedge the risk yourself with spot and options
        before the market moves at the next fixing.
      </p>

      {message && <div className={"message " + message.kind}>{message.text}</div>}

      <div className="picker-grid">
        <form className="card" onSubmit={submit}>
          <h2>New session</h2>
          <label>
            Name
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My desk" maxLength={60} />
          </label>
          <div className="row">
            <label>
              From
              <select value={startDate} onChange={(e) => changeStart(e.target.value)}>
                {dates.map((d) => (
                  <option key={d} value={d}>{fmtDay(d)}</option>
                ))}
              </select>
            </label>
            <label>
              To
              <select value={endDate} onChange={(e) => setEndDate(e.target.value)}>
                {endChoices.map((d) => (
                  <option key={d} value={d}>{fmtDay(d)}</option>
                ))}
              </select>
            </label>
          </div>
          <label>
            Hedging
            <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
              <option value="manual">Manual (you hedge)</option>
              <option value="end_of_day">Automatic at end of day</option>
              <option value="threshold">Automatic over limits</option>
            </select>
          </label>
          <p className="hint">{POLICY_HELP[policy]}</p>
          <div className="row">
            <label>
              Delta limit (USD m)
              <input type="number" min="0" step="any" required value={deltaLimit} onChange={(e) => setDeltaLimit(e.target.value)} />
            </label>
            <label>
              Vega limit per bucket (CNY / vol pt)
              <input type="number" min="0" step="any" required value={vegaLimit} onChange={(e) => setVegaLimit(e.target.value)} />
            </label>
          </div>
          <button className="primary" type="submit" disabled={busy || fixings.length === 0}>
            {busy ? "Starting…" : "Start session"}
          </button>
        </form>

        <div className="card">
          <h2>Your sessions</h2>
          {sessions.length === 0 && <p className="hint">None yet in this browser.</p>}
          <ul className="session-list">
            {sessions.map((s) => (
              <li key={s.id}>
                <button className="link strong" onClick={() => onOpen(s.id)}>{s.name}</button>
                <span className="muted"> {s.id}</span>
                <button
                  className="small danger"
                  onClick={() => {
                    if (window.confirm(`Delete "${s.name}" for good?`)) onDelete(s.id);
                  }}
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}