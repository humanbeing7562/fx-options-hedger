// The whole page. Holds which session is open and everything fetched for it, and
// passes pieces down to the panels. Every change (an order, a hedge, the next fixing)
// goes to the server first; the page then fetches the session again, so what is on
// screen is always what the server has.

import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { dateOf, fmtTime } from "./format.js";
import SessionPicker from "./components/SessionPicker.jsx";
import TopBar from "./components/TopBar.jsx";
import OrderTicket from "./components/OrderTicket.jsx";
import RiskPanel from "./components/RiskPanel.jsx";
import HedgePanel from "./components/HedgePanel.jsx";
import PnlPanel from "./components/PnlPanel.jsx";
import Charts from "./components/Charts.jsx";
import PositionsTable from "./components/PositionsTable.jsx";
import OrdersTable from "./components/OrdersTable.jsx";
import BlotterTable from "./components/BlotterTable.jsx";
import MarketScreen from "./components/MarketScreen.jsx";
import StepLog from "./components/StepLog.jsx";
import ThemeToggle from "./components/ThemeToggle.jsx";

const SESSIONS_KEY = "fx-desk-sessions";      // [{id, name, created}] opened in this browser
const CURRENT_KEY = "fx-desk-current";         // the id open last time
const LIMITS_KEY = "fx-desk-limits-";          // + session id -> the limits set on screen
const THEME_KEY = "fx-desk-theme";             // "auto", "light" or "dark"

// Browser storage can be missing or throw (private windows, blocked storage): the page
// then simply forgets between visits.
function readStored(key, fallback) {
  try {
    const text = window.localStorage.getItem(key);
    if (text === null) return fallback;
    return JSON.parse(text);
  } catch (error) {
    return fallback;
  }
}

function writeStored(key, value) {
  try {
    if (value === null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, JSON.stringify(value));
    }
  } catch (error) {
    // nothing to do: storage is a convenience only
  }
}

const TABS = [
  ["positions", "Positions"],
  ["orders", "Orders"],
  ["blotter", "Blotter"],
  ["market", "Market"],
  ["log", "Step log"],
];

export default function App() {
  const [sessions, setSessions] = useState(() => readStored(SESSIONS_KEY, []));
  const [sessionId, setSessionId] = useState(() => readStored(CURRENT_KEY, null));
  const [fixings, setFixings] = useState([]);

  // Everything about the open session, as the server last sent it
  const [state, setState] = useState(null);
  const [market, setMarket] = useState(null);
  const [pnl, setPnl] = useState(null);
  const [orders, setOrders] = useState([]);
  const [blotter, setBlotter] = useState([]);
  const [recommend, setRecommend] = useState(null);
  const [history, setHistory] = useState([]);
  const historyRef = useRef([]);       // the same list, readable inside refresh() without waiting a render
  const openIdRef = useRef(sessionId); // the session open right now, so late replies for another one are dropped

  const [limits, setLimits] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);      // {kind: "ok" | "error", text}
  const [prefill, setPrefill] = useState(null);      // fills the order ticket (from a hedge leg or a market pillar)
  const [tab, setTab] = useState("positions");
  const [theme, setTheme] = useState(() => {
    const saved = readStored(THEME_KEY, "auto");
    if (saved === "light" || saved === "dark") return saved;
    return "auto";
  });

  // The theme lives on <html data-theme="...">; styles.css does the rest. Auto = no attribute.
  useEffect(() => {
    if (theme === "auto") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = theme;
    }
    writeStored(THEME_KEY, theme);
  }, [theme]);

  // The list of fixings (for picking dates and for "run to end of day"), once
  useEffect(() => {
    api.fixings()
      .then((data) => setFixings(data.times))
      .catch((error) => setMessage({ kind: "error", text: "Could not load the fixings: " + error.message }));
  }, []);

  useEffect(() => {
    writeStored(SESSIONS_KEY, sessions);
  }, [sessions]);

  useEffect(() => {
    writeStored(CURRENT_KEY, sessionId);
  }, [sessionId]);

  // A different session: start from nothing and fetch it
  useEffect(() => {
    setState(null);
    setMarket(null);
    setPnl(null);
    setOrders([]);
    setBlotter([]);
    setRecommend(null);
    setHistory([]);
    historyRef.current = [];
    openIdRef.current = sessionId;
    setPrefill(null);
    if (sessionId) {
      refresh(sessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Limits come from the session when it is created; changes on screen are kept per session
  useEffect(() => {
    if (state && limits === null) {
      const saved = readStored(LIMITS_KEY + sessionId, null);
      const start = {
        delta_usd: state.limits.delta_usd,
        vega_cny_per_volpt: state.limits.vega_cny_per_volpt,
        gamma_usd_per_100pips: 1000000,
      };
      if (saved) {
        for (const name in start) {
          if (typeof saved[name] === "number") start[name] = saved[name];
        }
      }
      setLimits(start);
    }
  }, [state, limits, sessionId]);

  function changeLimits(next) {
    setLimits(next);
    writeStored(LIMITS_KEY + sessionId, next);
  }

  async function refresh(id) {
    try {
      const historyStart = historyRef.current.length;
      const results = await Promise.all([
        api.state(id),
        api.market(id),
        api.pnl(id),
        api.orders(id),
        api.blotter(id),
        api.recommend(id),
        api.history(id, historyStart),
      ]);
      if (openIdRef.current !== id) return;      // the user has moved to another session meanwhile
      setState(results[0]);
      setMarket(results[1]);
      setPnl(results[2]);
      setOrders(results[3]);
      setBlotter(results[4]);
      setRecommend(results[5]);
      // Append only if nothing else has added records since this request was sent
      // (two refreshes at once would otherwise add the same records twice)
      const page = results[6];
      if (page.start === historyRef.current.length) {
        if (page.records.length > 0) {
          historyRef.current = historyRef.current.concat(page.records);
          setHistory(historyRef.current);
        }
      } else if (page.start > historyRef.current.length) {
        // Out of step: fetch the whole history again
        const all = await api.history(id, 0);
        if (openIdRef.current !== id) return;
        historyRef.current = all.records;
        setHistory(historyRef.current);
      }
    } catch (error) {
      if (openIdRef.current !== id) return;
      if (error.status === 404) {
        // Deleted on the server (or never existed): forget it here too
        forgetSession(id);
        setMessage({ kind: "error", text: "That session no longer exists." });
      } else {
        setMessage({ kind: "error", text: error.message });
      }
    }
  }

  // Runs one change on the server, then fetches the session again.
  // Returns true if the server accepted it.
  async function act(work, successText) {
    setBusy(true);
    setMessage(null);
    let ok = false;
    try {
      const result = await work();
      ok = true;
      if (successText) {
        const text = typeof successText === "function" ? successText(result) : successText;
        setMessage({ kind: "ok", text });
      }
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
    await refresh(sessionId);
    setBusy(false);
    return ok;
  }

  // ---- sessions ----

  async function createSession(settings) {
    setBusy(true);
    setMessage(null);
    try {
      const created = await api.createSession(settings);
      const entry = { id: created.session_id, name: created.name || "Untitled", created: created.created };
      setSessions([entry].concat(sessions));
      setLimits(null);
      setSessionId(created.session_id);
    } catch (error) {
      setMessage({ kind: "error", text: error.message });
    }
    setBusy(false);
  }

  function openSession(id) {
    setLimits(null);
    setSessionId(id);
  }

  function forgetSession(id) {
    setSessions((list) => list.filter((s) => s.id !== id));
    writeStored(LIMITS_KEY + id, null);
    if (id === sessionId) {
      setSessionId(null);
    }
  }

  async function deleteSession(id) {
    try {
      await api.deleteSession(id);
    } catch (error) {
      if (error.status !== 404) {
        setMessage({ kind: "error", text: error.message });
        return;
      }
    }
    forgetSession(id);
  }

  // ---- the clock ----

  function nextFixing() {
    act(() => api.next(sessionId), (record) => `Moved to ${fmtTime(record.time)}`);
  }

  // Runs to the last fixing of the day the NEXT fixing falls on: during the day that is
  // today's close; at the close it is tomorrow's close.
  function runToDayEnd() {
    const next = state.next_time;
    if (!next) return;
    const day = dateOf(next);
    let until = next;
    for (const t of fixings) {
      if (dateOf(t) === day) until = t;
    }
    act(() => api.run(sessionId, until), (result) => `Ran ${result.steps} fixing${result.steps === 1 ? "" : "s"} to ${fmtTime(result.time)}`);
  }

  // ---- trading and hedging ----

  function submitOption(order) {
    return act(() => api.submitOrder(sessionId, order), (row) => {
      if (row.status === "pending") return `Order #${row.order_id} waits for ${fmtTime(row.time)}`;
      if (row.status === "rejected") return `Order #${row.order_id} rejected: ${row.reason}`;
      return `Order #${row.order_id} booked`;
    });
  }

  function submitSpot(trade) {
    return act(() => api.spot(sessionId, trade), (row) => `Spot: we ${row.our_side} ${(row.notional / 1e6).toFixed(2)}m USD at ${row.price.toFixed(4)}`);
  }

  function cancelOrder(orderId) {
    act(() => api.cancelOrder(sessionId, orderId), `Order #${orderId} cancelled`);
  }

  function deltaHedge() {
    act(() => api.hedgeDelta(sessionId), (result) => (result.spot ? "Delta flattened with spot" : "Delta was already flat"));
  }

  function bookAllHedges() {
    act(() => api.hedge(sessionId, recommend), (result) => `Booked ${result.packages.length} hedge packages and flattened delta`);
  }

  function sendToTicket(fill) {
    setPrefill({ ...fill, stamp: Date.now() });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---- the page ----

  if (!sessionId) {
    return (
      <div className="page">
        <SessionPicker
          themeToggle={<ThemeToggle theme={theme} onChange={setTheme} />}
          fixings={fixings}
          sessions={sessions}
          busy={busy}
          message={message}
          onCreate={createSession}
          onOpen={openSession}
          onDelete={deleteSession}
        />
      </div>
    );
  }

  if (!state || !limits) {
    return (
      <div className="page">
        <div className="loading">
          Loading session…
          {message && <div className={"message " + message.kind}>{message.text}</div>}
          <button className="link" onClick={() => setSessionId(null)}>Back to sessions</button>
        </div>
      </div>
    );
  }

  const name = (sessions.find((s) => s.id === sessionId) || {}).name || state.name || sessionId;

  return (
    <div className="page">
      <TopBar
        themeToggle={<ThemeToggle theme={theme} onChange={setTheme} />}
        name={name}
        sessionId={sessionId}
        state={state}
        pnl={pnl}
        busy={busy}
        onNext={nextFixing}
        onRunDay={runToDayEnd}
        onLeave={() => setSessionId(null)}
      />

      {message && (
        <div className={"message " + message.kind}>
          {message.text}
          <button className="close" onClick={() => setMessage(null)} aria-label="Dismiss">×</button>
        </div>
      )}

      <div className="grid">
        <div className="col">
          <OrderTicket
            sessionId={sessionId}
            state={state}
            fixings={fixings}
            prefill={prefill}
            busy={busy}
            onSubmitOption={submitOption}
            onSubmitSpot={submitSpot}
          />
        </div>
        <div className="col">
          <RiskPanel state={state} limits={limits} onLimitsChange={changeLimits} />
          <HedgePanel
            state={state}
            recommend={recommend}
            limits={limits}
            busy={busy}
            onDeltaHedge={deltaHedge}
            onBookAll={bookAllHedges}
            onSendToTicket={sendToTicket}
          />
        </div>
        <div className="col">
          <PnlPanel history={history} pnl={pnl} />
          <Charts history={history} />
        </div>
      </div>

      <div className="card">
        <div className="tabs">
          {TABS.map(([key, label]) => (
            <button key={key} className={tab === key ? "tab active" : "tab"} onClick={() => setTab(key)}>
              {label}
              {key === "positions" && ` (${state.positions.length})`}
              {key === "orders" && state.pending_orders.length > 0 && ` (${state.pending_orders.length} pending)`}
            </button>
          ))}
        </div>
        {tab === "positions" && <PositionsTable state={state} />}
        {tab === "orders" && <OrdersTable orders={orders} busy={busy} onCancel={cancelOrder} />}
        {tab === "blotter" && <BlotterTable blotter={blotter} />}
        {tab === "market" && <MarketScreen market={market} onUse={sendToTicket} />}
        {tab === "log" && <StepLog history={history} />}
      </div>
    </div>
  );
}