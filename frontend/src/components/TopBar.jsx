// The bar at the top: where the clock is, spot, NAV, and the buttons that move time.

import { fmtClock, fmtSigned, fmtSpot, fmtTime, pnlClass } from "../format.js";

export default function TopBar({ themeToggle, name, sessionId, state, pnl, busy, onNext, onRunDay, onLeave }) {
  const nav = state.nav_cny;
  return (
    <header className="topbar">
      <div className="brand">
        <button className="link" onClick={onLeave} title="Back to sessions">← Sessions</button>
        <div>
          <div className="session-name">{name}</div>
          <div className="muted small-text">{sessionId} · {state.policy.split("_").join(" ")} hedging</div>
        </div>
      </div>

      <div className="stat">
        <div className="label">Beijing time</div>
        <div className="value">{fmtTime(state.time)}</div>
        <div className="muted small-text">fixing {state.index + 1} of {state.fixings}</div>
      </div>

      <div className="stat">
        <div className="label">USD/CNY spot</div>
        <div className="value mono">{fmtSpot(state.spot)}</div>
      </div>

      <div className="stat">
        <div className="label">NAV (P&amp;L since start)</div>
        <div className={"value mono " + pnlClass(nav)}>{fmtSigned(nav)} CNY</div>
        {pnl && <div className="muted small-text">{pnl.steps} fixings run</div>}
      </div>

      <div className="clock-buttons">
        {themeToggle}
        {state.done ? (
          <div className="done-note">Session finished: no more fixings.</div>
        ) : (
          <>
            <button className="primary" onClick={onNext} disabled={busy}>
              Next fixing → {fmtClock(state.next_time)}
            </button>
            <button onClick={onRunDay} disabled={busy} title="Run every fixing up to the close of the next fixing's day">
              Run to day's close
            </button>
          </>
        )}
      </div>
    </header>
  );
}