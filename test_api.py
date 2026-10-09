"""
Checks for the web API (api/app.py).

  1. Sessions: create two with different policies, read their state; listing all sessions
     needs the admin key
  2. They are independent: orders and clock moves in one never show in the other
  3. A full round trip: quote -> order -> next -> state / history / blotter / pnl,
     and the API gives exactly the same numbers as using Session directly
  4. Manual hedging over HTTP: recommend -> hedge -> delta flat; a stale plan is refused
  5. Errors: unknown session 404; bad orders 400 with the Session's own message; end of data 400
  6. Concurrency on a real server: two sessions stepped at the same time from many threads;
     double requests to ONE session take turns (no lost or repeated steps)
  7. History paging and delete
  8. Settings from a .env file
  9. For the web page: spot trades (client / hedge sides, edge, NAV), the market screen,
     the list of fixings, and spot trades surviving a restart (replayed from SQLite)
"""
import sys
import json
import time
import socket
import threading
sys.path.insert(0, "build")

import os
import tempfile
import httpx
import uvicorn
from fastapi.testclient import TestClient

from simulation.market_loader import MarketLoader
from simulation.session import Session
from api.app import create_app

market = MarketLoader()
problems = 0


def check(ok, message):
    global problems
    if not ok:
        problems += 1
        print("   FAIL:", message)


ADMIN = "test-admin-key"
# Every app in this test gets its own throwaway database, never the real sessions.db
TMP = tempfile.mkdtemp()


def temp_db(name):
    return os.path.join(TMP, name + ".db")


app = create_app(market, admin_key=ADMIN, db_path=temp_db("main"))
client = TestClient(app)

# ---- 1. Sessions ----
r = client.get("/health")
check(r.status_code == 200 and r.json()["ok"], "health")
a = client.post("/sessions", json={"name": "desk A", "policy": "end_of_day"}).json()
b = client.post("/sessions", json={"name": "desk B", "policy": "manual", "start": "2026-08-03 10:00"}).json()
check(a["session_id"] != b["session_id"], "each session gets its own id")
check(a["time"] == "2026-07-07T10:00:00" and b["time"] == "2026-08-03T10:00:00", "start times as asked")
listed = client.get("/sessions", headers={"X-Admin-Key": ADMIN}).json()
ids = []
for row in listed:
    ids.append(row["session_id"])
check(a["session_id"] in ids and b["session_id"] in ids and len(listed) == 2, "both sessions listed")
state_a = client.get(f"/sessions/{a['session_id']}").json()
check(state_a["policy"] == "end_of_day" and state_a["name"] == "desk A" and state_a["nav_cny"] == 0.0, "state of A")
check(client.get("/sessions").status_code == 403, "listing without the admin key -> 403")
check(client.get("/sessions", headers={"X-Admin-Key": "guess"}).status_code == 403, "wrong admin key -> 403")
no_key_client = TestClient(create_app(market, admin_key="", db_path=temp_db("nokey")))
r = no_key_client.get("/sessions", headers={"X-Admin-Key": ""})
check(r.status_code == 403 and "switched off" in r.json()["detail"], "no key on the server -> listing switched off")
check(client.get(f"/sessions/{a['session_id']}").status_code == 200, "a session's own id still opens it, no key needed")
print(f"1. two sessions: {a['session_id']} (end_of_day, from {a['time']}) and {b['session_id']} (manual, from {b['time']}); "
      f"listing needs the admin key (none / wrong / unset on server -> 403)")

# ---- 2. Independent ----
A = f"/sessions/{a['session_id']}"
B = f"/sessions/{b['session_id']}"
order = {"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30, "strike": "25D"}
client.post(f"{A}/orders", json=order)
client.post(f"{A}/next")
client.post(f"{A}/next")
state_a = client.get(A).json()
state_b = client.get(B).json()
check(len(state_a["positions"]) > 0 and len(state_b["positions"]) == 0, "an order in A never appears in B")
check(state_a["index"] == 2 and state_b["index"] == 0, "moving A's clock leaves B's alone")
check(len(client.get(f"{B}/orders").json()) == 0 and len(client.get(f"{A}/orders").json()) == 1, "order lists are separate")
print(f"2. independent: A at {state_a['time']} with {len(state_a['positions'])} positions; B still at {state_b['time']} with none")

# ---- 3. Round trip, and the same numbers as Session used directly ----
c = client.post("/sessions", json={"start": "2026-09-21 10:00"}).json()
C = f"/sessions/{c['session_id']}"
q = client.post(f"{C}/quote", json={"side": "buy", "is_call": True, "notional": 5e6, "tenor_days": 7, "strike": "ATMF"}).json()
check(q["bid"] < q["mid"] < q["ask"] and q["our_price"] == q["ask"], "quote over HTTP")
orders = [
    {"side": "buy", "is_call": True, "notional": 5e6, "tenor_days": 7, "strike": "ATMF", "price": q["ask"]},
    {"side": "sell", "is_call": False, "notional": 8e6, "tenor_days": 30, "strike": "25D", "time": "2026-09-21 12:30"},
    {"side": "buy", "is_call": False, "notional": 2e6, "tenor_days": 14, "strike": "ATM", "tag": "hedge"},
]
for o in orders:
    r = client.post(f"{C}/orders", json=o)
    check(r.status_code == 200, f"order accepted: {r.text[:120]}")
run = client.post(f"{C}/run", json={"until": "2026-09-30 16:00"}).json()
check(run["done"] and run["steps"] > 0, "run to the end")
api_state = client.get(C).json()
api_pnl = client.get(f"{C}/pnl").json()
api_history = client.get(f"{C}/history").json()
api_blotter = client.get(f"{C}/blotter").json()

direct = Session(start="2026-09-21 10:00", market=market)
direct.quote({"side": "buy", "is_call": True, "notional": 5e6, "tenor_days": 7, "strike": "ATMF"})
for o in orders:
    direct.submit_order(o)
direct.run(until="2026-09-30 16:00")
check(abs(api_state["nav_cny"] - direct.book.nav(direct.snapshot)) < 1e-9, "API NAV = direct Session NAV")
check(abs(api_pnl["total"] - api_pnl["nav_cny"]) < 1e-4, "P&L summary adds up to NAV over HTTP")
check(api_history["total"] == len(direct.history()) and len(api_blotter) == len(direct.blotter()), "history and blotter match")
steps_with_explain = 0
for record in api_history["records"]:
    if record["type"] == "step" and "pnl_explain" in record:
        steps_with_explain += 1
check(steps_with_explain == run["steps"], "every step carries its P&L explain")
print(f"3. round trip: {run['steps']} steps, NAV {api_state['nav_cny']:,.0f} CNY = direct Session; "
      f"P&L: carry {api_pnl['carry']:,.0f}, time {api_pnl['time']:,.0f}, spot {api_pnl['spot']:,.0f}, "
      f"rates {api_pnl['rates']:,.0f}, vol {api_pnl['vol']:,.0f}, trading {api_pnl['trading']:,.0f}")

# ---- 4. Manual hedging ----
client.post(f"{B}/orders", json={"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
client.post(f"{B}/orders", json={"side": "buy", "is_call": False, "notional": 10e6, "tenor_days": 91, "strike": "25D"})
plan = client.get(f"{B}/hedge/recommend").json()
done = client.post(f"{B}/hedge", json={"plan": plan}).json()
state_b = client.get(B).json()
check(len(done["packages"]) > 0 and abs(state_b["greeks"]["delta"]) < 1e-3, "recommended hedge booked, delta flat")
client.post(f"{B}/next")
r = client.post(f"{B}/hedge", json={"plan": plan})
check(r.status_code == 400 and "another fixing" in r.json()["detail"], "a plan from an earlier fixing is refused")
r = client.post(f"{B}/hedge/delta")
check(r.status_code == 200 and abs(client.get(B).json()["greeks"]["delta"]) < 1e-3, "delta hedge over HTTP")
print(f"4. manual hedging: {len(done['packages'])} packages booked from the plan, delta flat; stale plan -> 400")

# ---- 5. Errors ----
r = client.get("/sessions/nope")
check(r.status_code == 404, "unknown session -> 404")
r = client.post(f"{B}/orders", json={"side": "long", "is_call": True, "notional": 1e6, "tenor_days": 7})
check(r.status_code == 400 and "side" in r.json()["detail"], f"bad side -> 400 with reason: {r.text[:100]}")
r = client.post(f"{B}/orders", json={"side": "buy", "is_call": True, "notional": 1e6, "tenor_days": 7, "time": "2026-07-01 10:00"})
check(r.status_code == 400 and "clock" in r.json()["detail"], "order in the past -> 400")
r = client.post(f"{B}/orders", json={"side": "buy", "is_call": "yes", "notional": 1e6, "tenor_days": 7})
check(r.status_code == 422, "is_call must be a real true/false -> 422")
r = client.post(f"{C}/next")
check(r.status_code == 400 and "no more fixings" in r.json()["detail"], "next at the end -> 400")
r = client.post("/sessions", json={"policy": "yolo"})
check(r.status_code == 400 and "policy" in r.json()["detail"], "unknown policy -> 400")
print("5. errors: unknown session 404; bad side / past time / end of data / bad policy 400 with the Session's message; "
      "non-boolean is_call 422")

# ---- 6. Concurrency on a real server ----
def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


port = free_port()
server = uvicorn.Server(uvicorn.Config(create_app(market, db_path=temp_db("server")), host="127.0.0.1", port=port,
                                        log_level="warning"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
base = f"http://127.0.0.1:{port}"
for i in range(100):
    try:
        httpx.get(f"{base}/health", timeout=1.0)
        break
    except httpx.HTTPError:
        time.sleep(0.05)

s1 = httpx.post(f"{base}/sessions", json={"name": "one"}).json()["session_id"]
s2 = httpx.post(f"{base}/sessions", json={"name": "two", "policy": "manual"}).json()["session_id"]
httpx.post(f"{base}/sessions/{s1}/orders", json={"side": "buy", "is_call": True, "notional": 10e6, "tenor_days": 30})
httpx.post(f"{base}/sessions/{s2}/orders", json={"side": "sell", "is_call": False, "notional": 7e6, "tenor_days": 14, "tag": "client"})

STEPS_PER_THREAD = 6
THREADS_PER_SESSION = 3
errors = []


def stepper(session_id):
    with httpx.Client(timeout=60.0) as http:
        for i in range(STEPS_PER_THREAD):
            r = http.post(f"{base}/sessions/{session_id}/next")
            if r.status_code != 200:
                errors.append(r.text)


workers = []
for session_id in [s1, s2]:
    for i in range(THREADS_PER_SESSION):
        workers.append(threading.Thread(target=stepper, args=(session_id,)))
started = time.time()
for w in workers:
    w.start()
for w in workers:
    w.join()
elapsed = time.time() - started

expected_steps = STEPS_PER_THREAD * THREADS_PER_SESSION
for session_id in [s1, s2]:
    st = httpx.get(f"{base}/sessions/{session_id}").json()
    hist = httpx.get(f"{base}/sessions/{session_id}/history").json()["records"]
    step_indices = []
    for record in hist:
        if record["type"] == "step":
            step_indices.append(record["index"])
    check(st["index"] == expected_steps, f"session {session_id}: index {st['index']}, expected {expected_steps}")
    check(step_indices == list(range(1, expected_steps + 1)), f"session {session_id}: steps out of order or repeated")
    # every step still explains its P&L exactly
    worst = 0.0
    for record in hist:
        if record["type"] == "step":
            worst = max(worst, abs(record["pnl_explain"]["total"] - record["market_pnl_cny"]))
    check(worst < 1e-6, f"session {session_id}: explain gap {worst}")
check(len(errors) == 0, f"request errors: {errors[:2]}")
server.should_exit = True
thread.join(timeout=10)
print(f"6. real server: {2 * THREADS_PER_SESSION} threads x {STEPS_PER_THREAD} 'next' on two sessions at once "
      f"in {elapsed:.1f}s: each session exactly {expected_steps} steps, in order, none lost or repeated")

# ---- 7. History paging and delete ----
full = client.get(f"{A}/history").json()
client.post(f"{A}/next")
newer = client.get(f"{A}/history", params={"start": full["total"]}).json()
check(len(newer["records"]) == 1 and newer["records"][0]["type"] == "step", "history?start=N returns only new records")
check(client.get(f"{A}/history", params={"start": -1}).status_code == 400, "negative start refused")
check(client.delete(A).status_code == 200 and client.get(A).status_code == 404, "deleted session is gone")
check(client.get(B).status_code == 200, "deleting A leaves B")
try:
    json.dumps(client.get(f"{B}/history").json(), allow_nan=False)
    ok = True
except ValueError:
    ok = False
check(ok, "responses are plain JSON")
print("7. history?start=N gives only new records; delete removes one session and leaves the others")

# ---- 8. .env loading ----
from api.app import load_env_file
env_path = os.path.join(TMP, "test.env")
with open(env_path, "w") as f:
    f.write("# a comment\n\nFX_TEST_PLAIN=abc\nFX_TEST_QUOTED=\"with spaces\"\nFX_TEST_EMPTY=\nnot a setting\n"
            "FX_TEST_SHELL=from-file\n")
os.environ["FX_TEST_SHELL"] = "from-shell"
added = load_env_file(env_path)
check(os.environ.get("FX_TEST_PLAIN") == "abc" and os.environ.get("FX_TEST_QUOTED") == "with spaces",
      ".env values loaded, quotes removed")
check(os.environ.get("FX_TEST_EMPTY") == "", "an empty value is allowed")
check(os.environ["FX_TEST_SHELL"] == "from-shell" and "FX_TEST_SHELL" not in added, "the shell wins over .env")
check(load_env_file(os.path.join(TMP, "missing.env")) == [], "no .env file is fine")
for key in ["FX_TEST_PLAIN", "FX_TEST_QUOTED", "FX_TEST_EMPTY", "FX_TEST_SHELL"]:
    os.environ.pop(key, None)
print("8. .env: values and quotes read, comments skipped, shell variables win, a missing file is fine")

# ---- 9. Spot trades, market screen, fixings, replay ----
fix = client.get("/fixings").json()
check(fix["count"] == len(market.snapshot_times()) and fix["times"][0] == "2026-07-07T10:00:00", "fixings listed")

spot_db = temp_db("spot")
app9 = create_app(market, admin_key=ADMIN, db_path=spot_db)
c9 = TestClient(app9)
s9 = c9.post("/sessions", json={"name": "spot desk", "policy": "manual"}).json()
S = f"/sessions/{s9['session_id']}"
spot0 = c9.get(f"{S}/market").json()["spot"]

# client buys 1m USD 5 pips above spot: we sell, and earn 1m x 0.0005 = 500 CNY
t1 = c9.post(f"{S}/spot", json={"side": "buy", "notional": 1e6, "price": spot0 + 0.0005}).json()
check(t1["our_side"] == "sell" and t1["usd_amount"] == -1e6, "client buy -> we sell")
check(abs(t1["edge_cny"] - 500.0) < 1e-6, f"client edge 500 CNY, got {t1['edge_cny']}")
st = c9.get(S).json()
check(abs(st["nav_cny"] - 500.0) < 1e-6, f"NAV after the client trade is the edge, got {st['nav_cny']}")
check(abs(st["greeks"]["delta"] + 1e6) < 1e-6, "we are short 1m USD")

# hedge: WE buy 1m at mid -> flat, no P&L
t2 = c9.post(f"{S}/spot", json={"side": "buy", "notional": 1e6, "tag": "hedge"}).json()
check(t2["our_side"] == "buy" and t2["edge_cny"] == 0.0, "hedge buy is our buy, at mid")
st = c9.get(S).json()
check(abs(st["greeks"]["delta"]) < 1e-6 and abs(st["nav_cny"] - 500.0) < 1e-6, "flat after the hedge, NAV kept")

# refusals: bad side / zero notional (400), NaN (422); nothing saved for them
check(c9.post(f"{S}/spot", json={"side": "long", "notional": 1e6}).status_code == 400, "bad side -> 400")
check(c9.post(f"{S}/spot", json={"side": "buy", "notional": 0}).status_code == 400, "zero notional -> 400")
check(c9.post(f"{S}/spot", content='{"side": "buy", "notional": NaN}',
              headers={"Content-Type": "application/json"}).status_code == 422, "NaN notional -> 422")
check(c9.post(f"{S}/spot", json={"side": "buy", "notional": 1e6, "price": -1}).status_code == 400, "negative price -> 400")

# trading P&L shows in the summary
pnl9 = c9.get(f"{S}/pnl").json()
check(abs(pnl9["trading"] - 500.0) < 1e-6 and abs(pnl9["total"] - pnl9["nav_cny"]) < 1e-6, "pnl: trading 500, sums to NAV")

# market screen: every tenor has 5 pillars, deltas at the quoted pillars, finite numbers
m9 = c9.get(f"{S}/market").json()
check(len(m9["tenors"]) > 0, "market screen has tenors")
for tenor in m9["tenors"]:
    names = []
    for pillar in tenor["pillars"]:
        names.append(pillar["pillar"])
    check(names == ["10P", "25P", "ATM", "25C", "10C"], f"{tenor['tenor']} pillars")
    by_name = {}
    for pillar in tenor["pillars"]:
        by_name[pillar["pillar"]] = pillar
    check(abs(by_name["25C"]["delta"] - 0.25) < 1e-3 and abs(by_name["25P"]["delta"] + 0.25) < 1e-3,
          f"{tenor['tenor']} 25D pillars have 25 delta")
    check(abs(by_name["10C"]["delta"] - 0.10) < 1e-3 and abs(by_name["10P"]["delta"] + 0.10) < 1e-3,
          f"{tenor['tenor']} 10D pillars have 10 delta")
    check(abs(tenor["rr25"] - (by_name["25C"]["vol"] - by_name["25P"]["vol"])) < 1e-12, f"{tenor['tenor']} RR")
try:
    json.dumps(m9, allow_nan=False)
    ok = True
except ValueError:
    ok = False
check(ok, "market screen is plain JSON")

# a step, then a restart: the session comes back from SQLite with the spot trades replayed
c9.post(f"{S}/next")
c9.post(f"{S}/spot", json={"side": "sell", "notional": 2e6})      # client sells 2m: we buy, long 2m
before = c9.get(S).json()
blotter_before = c9.get(f"{S}/blotter").json()
app9b = create_app(market, admin_key=ADMIN, db_path=spot_db)
c9b = TestClient(app9b)
after = c9b.get(S).json()
check(app9b.state.store.replays == 1, "rebuilt by replay")
check(after["nav_cny"] == before["nav_cny"] and after["greeks"]["delta"] == before["greeks"]["delta"]
      and after["time"] == before["time"], "same NAV, delta and clock after the restart")
check(c9b.get(f"{S}/blotter").json() == blotter_before, "same blotter after the restart")
check(abs(after["greeks"]["delta"] - 2e6) < 1e-6, "long 2m after the client sold to us")
print(f"9. spot: client buy +5 pips -> edge {t1['edge_cny']:.0f} CNY, hedge flattens it; market screen "
      f"{len(m9['tenors'])} tenors x 5 pillars at their deltas; {fix['count']} fixings; spot trades replay after a restart")

print()
print("ALL CHECKS PASSED" if problems == 0 else f"{problems} PROBLEMS")