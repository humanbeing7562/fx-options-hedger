"""
Checks for saving sessions in SQLite (api/journal.py + the SessionStore in api/app.py).

  1. Restart: a new app on the same database gives back every session exactly - state,
     history (with P&L explains), blotter, orders and P&L - after every kind of action
  2. A restored session carries on exactly as the original would
  3. Only successful changes are saved: refused requests and read-only requests add nothing
  4. Idle sessions are unloaded from memory and come back by themselves on the next request
  5. The memory cap keeps only the most recently used sessions loaded
  6. A session busy with a request is never unloaded under it
  7. Many requests at once for an unloaded session rebuild it exactly once
  8. Admin listing shows saved sessions whether loaded or not
  9. Delete removes a session for good; a broken saved action gives a clear error, not a crash
 10. Failures can't put memory ahead of the database; NaN/Infinity and timezones over HTTP;
     delete racing a waiting request; the cap while creating; reused ids start clean
 11. A save that fails half-way is rolled back, never committed by a later save
"""
import sys
import os
import json
import tempfile
import threading
sys.path.insert(0, "build")

from fastapi.testclient import TestClient

from simulation.market_loader import MarketLoader
from api.app import create_app, apply_action
from api.journal import Journal

market = MarketLoader()
problems = 0
ADMIN = "admin-test"
TMP = tempfile.mkdtemp()
DB = os.path.join(TMP, "sessions.db")


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


def same(x, y):
    return json.dumps(x, sort_keys=True) == json.dumps(y, sort_keys=True)


def snapshot_of(client, session_id):
    """Everything a page could read about a session."""
    base = f"/sessions/{session_id}"
    return {
        "state": client.get(base).json(),
        "history": client.get(f"{base}/history").json(),
        "blotter": client.get(f"{base}/blotter").json(),
        "orders": client.get(f"{base}/orders").json(),
        "pnl": client.get(f"{base}/pnl").json(),
    }


def make_app(**settings):
    return create_app(market, admin_key=ADMIN, db_path=DB, **settings)


# ---- 1. Restart ----
app1 = make_app()
c1 = TestClient(app1)
s1 = c1.post("/sessions", json={"name": "busy desk", "start": "2026-09-14 10:00", "policy": "manual"}).json()["session_id"]
s2 = c1.post("/sessions", json={"name": "quiet desk", "start": "2026-09-21 10:00", "policy": "end_of_day"}).json()["session_id"]
S1 = f"/sessions/{s1}"
S2 = f"/sessions/{s2}"

# every kind of action on s1
c1.post(f"{S1}/orders", json={"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30, "strike": "25D"})
c1.post(f"{S1}/orders", json={"side": "sell", "is_call": False, "notional": 6e6, "tenor_days": 14, "strike": "ATMF",
                              "time": "2026-09-14 12:30"})
pending = c1.post(f"{S1}/orders", json={"side": "buy", "is_call": True, "notional": 3e6, "tenor_days": 7,
                                        "time": "2026-09-15 10:00"}).json()
c1.delete(f"{S1}/orders/{pending['order_id']}")                                   # cancel
c1.post(f"{S1}/next")                                                             # 11:00
plan = c1.get(f"{S1}/hedge/recommend").json()
c1.post(f"{S1}/hedge", json={"plan": plan})                                       # hedge with a plan
c1.post(f"{S1}/run", json={"until": "2026-09-15 16:00"})                          # run
c1.post(f"{S1}/orders", json={"tag": "hedge", "side": "buy", "is_call": False, "notional": 4e6,
                              "tenor_days": 21, "strike": "ATM"})                 # hedge order
c1.post(f"{S1}/hedge", json={})                                                   # hedge without a plan
c1.post(f"{S1}/run", json={"steps": 3})
c1.post(f"{S1}/hedge/delta")                                                      # delta hedge
c1.post(f"{S2}/orders", json={"side": "buy", "is_call": False, "notional": 8e6, "tenor_days": 30, "strike": "10D"})
c1.post(f"{S2}/run", json={"until": "2026-09-23 16:00"})

before = {s1: snapshot_of(c1, s1), s2: snapshot_of(c1, s2)}

app2 = make_app()                       # a brand-new server on the same database: nothing in memory
c2 = TestClient(app2)
check(len(app2.state.store.loaded_ids()) == 0, "a new server starts with nothing loaded")
after = {s1: snapshot_of(c2, s1), s2: snapshot_of(c2, s2)}
for session_id in [s1, s2]:
    for part in ["state", "history", "blotter", "orders", "pnl"]:
        check(same(before[session_id][part], after[session_id][part]),
              f"session {session_id}: {part} differs after the restart")
check(app2.state.store.replays == 2, f"each session rebuilt once, got {app2.state.store.replays}")
saved_actions = len(app2.state.store.journal.actions(s1))
print(f"1. restart: 2 sessions rebuilt from {saved_actions} + {len(app2.state.store.journal.actions(s2))} saved actions; "
      f"state, history ({after[s1]['history']['total']} records), blotter ({len(after[s1]['blotter'])} trades), "
      f"orders and P&L identical")

# ---- 2. Carry on exactly as the original would ----
# Reference without any server: a plain Session given the same saved actions, then 4 steps.
# (Only ONE server may write to a database file, so app1 is not used from here on.)
from simulation.session import Session
saved = app2.state.store.journal.session_row(s1)["settings"]
reference = Session(start=saved["start"], end=saved["end"], policy=saved["policy"], limits=saved["limits"],
                    market=market)
for kind, payload in app2.state.store.journal.actions(s1):
    apply_action(reference, kind, payload)
reference_steps = reference.run(steps=4)
r_rest = c2.post(f"{S1}/run", json={"steps": 4}).json()
check(same(reference_steps, r_rest["records"]), "the restored session takes the same next steps as a plain replay")
check(same(reference.pnl_summary(), c2.get(f"{S1}/pnl").json()), "and ends with the same P&L")
check(len(app2.state.store.journal.actions(s1)) == saved_actions + 1, "the 4 steps were saved as one run action")
print(f"2. restored session stepped 4 more fixings exactly like a plain Python replay; NAV "
      f"{c2.get(S1).json()['nav_cny']:,.0f} CNY in both")

# ---- 3. Only successful changes are saved ----
journal = app2.state.store.journal
count = len(journal.actions(s2))
c2.post(f"{S2}/orders", json={"side": "long", "is_call": True, "notional": 1e6, "tenor_days": 7})          # 400
c2.post(f"{S2}/orders", json={"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7,
                              "time": "2026-07-01 10:00"})                                                  # 400
c2.delete(f"{S2}/orders/999")                                                                               # 400
c2.post(f"{S2}/hedge", json={"plan": plan})                                                                # stale: 400
c2.post(f"{S2}/quote", json={"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7})
c2.get(S2)
c2.get(f"{S2}/hedge/recommend")
c2.get(f"{S2}/history")
check(len(journal.actions(s2)) == count, "refused and read-only requests must not be saved")
c2.post(f"{S2}/next")
check(len(journal.actions(s2)) == count + 1, "a successful next is saved")
print(f"3. 4 refused + 4 read-only requests saved nothing; one 'next' saved one action")

# ---- 4. Idle sessions unload and come back by themselves ----
app3 = make_app(idle_seconds=0.0)
c3 = TestClient(app3)
store3 = app3.state.store
state_before = c3.get(S2).json()
check(s2 in store3.loaded_ids(), "loaded after use")
unloaded = store3.sweep()
check(s2 in unloaded and s2 not in store3.loaded_ids(), "an idle session is unloaded")
replays_before = store3.replays
state_after = c3.get(S2).json()
check(same(state_before, state_after) and store3.replays == replays_before + 1,
      "the next request rebuilds it, unchanged")
print("4. idle session unloaded from memory, then rebuilt unchanged by the next request")

# ---- 5. Memory cap ----
app4 = make_app(max_loaded=1)
c4 = TestClient(app4)
store4 = app4.state.store
c4.get(S1)
c4.get(S2)                        # most recent
store4.sweep()
check(store4.loaded_ids() == [s2], f"cap of 1 keeps only the most recently used, got {store4.loaded_ids()}")
print("5. with a cap of 1 loaded session, only the most recently used stays in memory")

# ---- 6. A busy session is never unloaded ----
app5 = make_app(idle_seconds=0.0)
c5 = TestClient(app5)
store5 = app5.state.store
c5.get(S2)
entry = store5.get(s2)
entry["lock"].acquire()           # as if a request were running
unloaded = store5.sweep()
entry["lock"].release()
check(s2 not in unloaded and s2 in store5.loaded_ids(), "a session in use is skipped by the sweep")
check(len(store5.sweep()) == 1, "and unloaded once it is free")
print("6. a session busy with a request is skipped by the sweep, unloaded once free")

# ---- 7. Many requests at once for an unloaded session: one rebuild ----
app6 = make_app()
store6 = app6.state.store
results = []
errors = []


def read_state():
    try:
        with TestClient(app6) as c:
            results.append(c.get(S1).json())
    except Exception as error:
        errors.append(error)


threads = []
for i in range(6):
    threads.append(threading.Thread(target=read_state))
for t in threads:
    t.start()
for t in threads:
    t.join()
all_same = True
for r in results:
    if not same(r, results[0]):
        all_same = False
check(len(errors) == 0 and len(results) == 6 and all_same, "6 simultaneous requests all get the same session")
check(store6.replays == 1, f"the session must be rebuilt exactly once, got {store6.replays}")
print(f"7. 6 simultaneous requests for an unloaded session: rebuilt once, all 6 got the same state")

# ---- 8. Admin listing includes unloaded sessions ----
app7 = make_app()
c7 = TestClient(app7)
c7.get(S2)                        # load only s2
listing = c7.get("/sessions", headers={"X-Admin-Key": ADMIN}).json()
by_id = {}
for row in listing:
    by_id[row["session_id"]] = row
check(set(by_id) == {s1, s2}, "both saved sessions listed")
check(by_id[s2]["loaded"] and "nav_cny" in by_id[s2], "loaded session listed with live details")
check(not by_id[s1]["loaded"] and by_id[s1]["actions"] > 0, "unloaded session listed from the database")
check(len(app7.state.store.loaded_ids()) == 1, "listing must not load sessions")
print(f"8. admin list: {len(listing)} sessions, loaded one with live NAV, unloaded one with its "
      f"{by_id[s1]['actions']} saved actions; listing loaded nothing")

# ---- 9. Delete, and a broken saved action ----
check(c7.delete(S2).status_code == 200, "delete")
app8 = make_app()
c8 = TestClient(app8)
check(c8.get(S2).status_code == 404, "a deleted session stays gone after a restart")
check(app8.state.store.journal.session_row(s2) is None and len(app8.state.store.journal.actions(s2)) == 0,
      "its rows are removed")
broken = c8.post("/sessions", json={"name": "broken"}).json()["session_id"]
Journal(DB).add_action(broken, "order", {"side": "sideways", "is_call": True, "notional": 1.0, "tenor_days": 7},
                       "2026-10-09T00:00:00")
app9 = make_app()
c9 = TestClient(app9)
r = c9.get(f"/sessions/{broken}")
check(r.status_code == 500 and "could not restore" in r.json()["detail"], f"broken replay -> clear 500: {r.text[:120]}")
check(c9.get(S1).status_code == 200, "other sessions still work")
try:
    apply_action(None, "teleport", {})
    unknown_ok = False
except ValueError:
    unknown_ok = True
check(unknown_ok, "an unknown saved action is refused")
print("9. deleted session gone after restart; a broken saved action gives a clear 500 and other sessions still work")

# ---- 10. Failures can't leave memory ahead of the database (found in review) ----
import time as _time
app10 = make_app(idle_seconds=0.0)
c10 = TestClient(app10, raise_server_exceptions=False)
store10 = app10.state.store
sid = c10.post("/sessions", json={"name": "fragile", "start": "2026-09-14 10:00", "policy": "manual"}).json()["session_id"]
SID = f"/sessions/{sid}"
c10.post(f"{SID}/orders", json={"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
c10.post(f"{SID}/run", json={"steps": 2})
saved_view = snapshot_of(c10, sid)

# (a) an action that changes the session and THEN fails: 500, and the next request sees the saved state
entry = store10.get(sid)
real_next = entry["session"].next


def next_then_crash():
    real_next()
    raise RuntimeError("simulated crash after the step")


entry["session"].next = next_then_crash
replays = store10.replays
r = c10.post(f"{SID}/next")
check(r.status_code == 500, f"a crash inside an action is a 500, got {r.status_code}")
after_crash = snapshot_of(c10, sid)
check(store10.replays == replays + 1, "the half-changed copy was dropped and the session rebuilt")
for part in ["state", "history", "blotter", "orders", "pnl"]:
    check(same(saved_view[part], after_crash[part]), f"after a crash, {part} must equal the saved session")
check(c10.post(f"{SID}/next").status_code == 200, "and it carries on normally")

# (b) the review's corrupting case over HTTP: a pending order 10 ms from expiry at its booking fixing
sid_b = c10.post("/sessions", json={"start": "2026-09-14 10:00"}).json()["session_id"]
B = f"/sessions/{sid_b}"
r = c10.post(f"{B}/orders", json={"side": "buy", "is_call": True, "notional": 1e6, "time": "2026-09-14 10:30",
                                  "expiry": "2026-09-14 11:00:00.010"})
check(r.status_code == 200 and r.json()["status"] == "pending", "near-expiry pending order accepted for later")
r = c10.post(f"{B}/run", json={"steps": 3})
check(r.status_code == 200, f"the run completes: {r.text[:120]}")
view_b = snapshot_of(c10, sid_b)
store10.sweep()                                                          # unload everything
check(same(view_b, snapshot_of(c10, sid_b)), "and the session still rebuilds exactly")

# (c) NaN / Infinity over HTTP are refused before they reach the session; a timezone works
r = c10.post(f"{B}/orders", content='{"side": "buy", "is_call": true, "notional": NaN, "tenor_days": 7}',
             headers={"content-type": "application/json"})
check(r.status_code == 422, f"NaN notional -> 422, got {r.status_code}")
r = c10.post(f"{B}/orders", content='{"side": "buy", "is_call": true, "notional": 1e6, "tenor_days": Infinity}',
             headers={"content-type": "application/json"})
check(r.status_code == 422, f"Infinity tenor -> 422, got {r.status_code}")
r = c10.post(f"{B}/run", json={"until": "2026-09-14T08:00:00Z"})           # 16:00 Beijing
check(r.status_code == 200 and r.json()["time"] == "2026-09-14T16:00:00", f"UTC 'until' works: {r.text[:100]}")
r = c10.post("/sessions", json={"start": "2026-09-14T02:00:00+00:00"})
check(r.status_code == 200 and r.json()["time"] == "2026-09-14T10:00:00", "a session start with a timezone works")

# (d) delete racing a waiting request: the session never comes back, and no rows are left behind
races = 0
for attempt in range(15):
    rid = c10.post("/sessions", json={"name": "race"}).json()["session_id"]
    c10.post(f"/sessions/{rid}/next")
    held = store10.get(rid)
    held["lock"].acquire()                            # a request is busy with it
    out = {}

    def reader():
        out["get"] = c10.get(f"/sessions/{rid}").status_code

    def deleter():
        out["delete"] = c10.delete(f"/sessions/{rid}").status_code

    t_read = threading.Thread(target=reader)
    t_del = threading.Thread(target=deleter)
    t_read.start()
    t_del.start()
    _time.sleep(0.05)                                 # both are now waiting
    held["lock"].release()
    t_read.join()
    t_del.join()
    gone = (rid not in store10.loaded_ids() and store10.journal.session_row(rid) is None
            and len(store10.journal.actions(rid)) == 0 and c10.get(f"/sessions/{rid}").status_code == 404)
    if gone and out.get("delete") == 200 and out.get("get") in (200, 404):
        races += 1
check(races == 15, f"delete must win cleanly every time ({races} of 15)")

# (e) the cap holds as sessions are created, not only at the next sweep
app11 = make_app(max_loaded=2)
c11 = TestClient(app11)
for i in range(4):
    c11.post("/sessions", json={"name": f"cap {i}"})
check(len(app11.state.store.loaded_ids()) <= 2, f"cap of 2 while creating, loaded {len(app11.state.store.loaded_ids())}")

# (f) a new session never inherits old rows under the same id
j = Journal(os.path.join(TMP, "stale.db"))
j.add_session("deadbeef", "old", {"start": None, "end": None, "policy": "manual", "limits": None}, "2026-10-01T00:00:00")
j.add_action("deadbeef", "next", {}, "2026-10-01T00:00:01")
j.delete("deadbeef")
j.db().execute("INSERT INTO actions (session_id, seq, kind, payload, at) VALUES ('deadbeef', 1, 'next', '{}', 'x')")
j.db().commit()
j.add_session("deadbeef", "new", {"start": None, "end": None, "policy": "manual", "limits": None}, "2026-10-02T00:00:00")
check(len(j.actions("deadbeef")) == 0, "stale actions under a reused id are cleared")
print("10. a crash after a change drops the in-memory copy and the session comes back exactly as saved; "
      "the near-expiry case runs and rebuilds; NaN/Infinity 422; timezones work; delete beats a waiting request "
      "15/15; the cap holds while creating; a reused id starts clean")

# ---- 11. A save that fails half-way is rolled back, never committed later (found in review) ----
app12 = make_app()
c12 = TestClient(app12, raise_server_exceptions=False)
store12 = app12.state.store
x = c12.post("/sessions", json={"name": "save fails", "start": "2026-09-14 10:00"}).json()["session_id"]
y = c12.post("/sessions", json={"name": "bystander", "start": "2026-09-14 10:00"}).json()["session_id"]
c12.post(f"/sessions/{x}/next")
view_x = snapshot_of(c12, x)
actions_x = len(store12.journal.actions(x))
# make the SECOND statement of x's next save fail (its INSERT has already run when this fires)
with store12.journal.lock:
    store12.journal.db().execute(
        f"CREATE TRIGGER fail_x BEFORE UPDATE ON sessions WHEN NEW.session_id = '{x}' "
        f"BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END")
    store12.journal.db().commit()
r = c12.post(f"/sessions/{x}/next")
check(r.status_code == 500, f"a failed save is a 500, got {r.status_code}")
check(c12.post(f"/sessions/{y}/next").status_code == 200, "another session's save afterwards succeeds")
with store12.journal.lock:
    store12.journal.db().execute("DROP TRIGGER fail_x")
    store12.journal.db().commit()
check(len(store12.journal.actions(x)) == actions_x,
      "the failed save must not be committed by the next session's save")
after_x = snapshot_of(c12, x)
for part in ["state", "history", "blotter", "orders", "pnl"]:
    check(same(view_x[part], after_x[part]), f"after a failed save, {part} must equal the saved session")
print("11. a save that fails half-way is rolled back: another session's save afterwards doesn't commit it, "
      "and the session comes back exactly as last saved")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")