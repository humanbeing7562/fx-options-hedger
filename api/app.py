"""
Web API for the desk: many independent Sessions, each driven over HTTP, saved in SQLite.

Run from the project root:
    FX_ADMIN_KEY=some-long-random-string uvicorn api.app:app --reload
then open http://127.0.0.1:8000/docs for every endpoint with a "try it" form.

Settings (environment variables, all optional; also read from <project>/.env, where
variables already set in the shell win over the file):
    FX_DB_PATH         SQLite file for saved sessions       default: <project>/sessions.db
                       (a relative path counts from the project folder)
    FX_ADMIN_KEY       key for GET /sessions                 default: none (listing off)
    FX_IDLE_MINUTES    unload a session after this idle time default: 30
    FX_MAX_LOADED      most sessions kept in memory at once  default: 200

How it is put together:
  - SessionStore keeps the sessions that are in use in memory, by id. Sessions share
    nothing except the market data (one MarketLoader, read-only, loaded once).
  - Every session is also saved in SQLite (api/journal.py): its settings, and every action
    that changed it (order, spot trade, cancel, next, run, hedge, delta hedge), written only after the
    action succeeded. Sessions are deterministic, so replaying those actions on a fresh
    Session rebuilds it exactly.
  - A session not in memory (after a restart, or unloaded while idle) is rebuilt by replay
    the first time a request asks for it. Nothing is loaded at startup.
  - Each session has its own lock: two requests to the SAME session take turns, so a
    double-clicked "next" can't interleave; requests to DIFFERENT sessions don't wait.
  - The Session does all checking. A bad order raises ValueError inside the Session and
    comes back as HTTP 400 with the Session's own message; nothing is saved for it.
  - GET /sessions (every session, ids included) is for the admin only: header X-Admin-Key
    must match FX_ADMIN_KEY. With no key set on the server, listing is switched off.
  - Run ONE worker: sessions in memory live inside the server process.
  - If frontend/dist exists (the React app, built with `npm run build`), it is served at "/".
"""
import os
import sys
import time
import secrets
import threading
import uuid
from contextlib import contextmanager
from typing import Optional, Union, Dict, Any, Annotated

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "build"))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from simulation.market_loader import MarketLoader
from simulation.session import Session
from api.journal import Journal

# How often (seconds) requests check for idle sessions to unload
SWEEP_EVERY = 60.0


def load_env_file(path):
    """
    Reads KEY=value lines from a .env file into the environment.
    Blank lines and lines starting with # are skipped; quotes around a value are removed.
    A variable already set (e.g. in the shell or by systemd) is never overwritten.
    Returns the keys it set.
    """
    if not os.path.exists(path):
        return []
    added = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line == "" or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                added.append(key)
    return added


load_env_file(os.path.join(PROJECT_ROOT, ".env"))


# ----------------------------------------------------------------------
# Request bodies (the Session checks the values; these only describe the shape)
# ----------------------------------------------------------------------

# A number that must be finite: NaN / Infinity in the JSON -> 422. (Set on each field: FastAPI
# validates fields one by one, so a model-wide setting alone doesn't reach them.)
Finite = Annotated[float, Field(allow_inf_nan=False)]

class SessionIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: Optional[str] = None
    start: Optional[str] = None          # e.g. "2026-07-07 10:00"; default: first fixing
    end: Optional[str] = None            # default: last fixing
    policy: str = "end_of_day"           # "end_of_day", "threshold" or "manual"
    limits: Optional[Dict[str, Finite]] = None   # threshold policy: delta_usd, vega_cny_per_volpt


class OrderIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)       # NaN / Infinity -> 422
    side: str                            # client order: the client's side; hedge order: ours
    is_call: StrictBool
    notional: Finite                     # USD, positive
    strike: Union[Finite, str] = "ATM"   # number, "ATM", "ATMF" or "25D"-style
    expiry: Optional[str] = None         # a timestamp ...
    tenor_days: Optional[Finite] = None  # ... or days from the order time
    time: Optional[str] = None           # default: now
    price: Optional[Finite] = None       # agreed premium, CNY per USD ...
    vol: Optional[Finite] = None         # ... or agreed vol (0.0215 = 2.15%); neither: mid at booking
    tag: str = "client"                  # "client" or "hedge"


class RunIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    steps: Optional[int] = None
    until: Optional[str] = None


class SpotIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    side: str                            # client trade: the client's side; hedge trade: ours
    notional: Finite                     # USD, positive
    price: Optional[Finite] = None       # CNY per USD; default: this fixing's spot
    tag: str = "client"                  # "client" or "hedge"


class HedgeIn(BaseModel):
    plan: Optional[Dict[str, Any]] = None    # from GET .../hedge/recommend; None = a fresh one


def order_payload(order):
    """OrderIn -> the plain dict Session expects (fields left out are not sent)."""
    return order.model_dump(exclude_none=True)


def now_text():
    return pd.Timestamp.now().isoformat(timespec="seconds")


def fingerprint(session):
    """Changes whenever an action changes the session (used to spot a half-done action)."""
    return (session.index, len(session.history_log), len(session.book.trades), session.order_count,
            len(session.pending), session.book.hedge_usd, session.book.cash_cny)


# ----------------------------------------------------------------------
# Replay: one saved action applied to a Session, exactly as the endpoint did it
# ----------------------------------------------------------------------

def apply_action(session, kind, payload):
    if kind == "order":
        session.submit_order(payload)
    elif kind == "cancel":
        session.cancel_order(payload["order_id"])
    elif kind == "next":
        session.next()
    elif kind == "run":
        session.run(steps=payload.get("steps"), until=payload.get("until"))
    elif kind == "hedge":
        session.hedge(payload.get("plan"))
    elif kind == "hedge_delta":
        session.hedge_delta()
    elif kind == "spot":
        session.trade_spot(payload)
    else:
        raise ValueError(f"unknown saved action {kind!r}")


# ----------------------------------------------------------------------
# Sessions: in memory while in use, in SQLite always
# ----------------------------------------------------------------------

class SessionStore:
    def __init__(self, journal, market=None, idle_seconds=1800.0, max_loaded=200):
        self.journal = journal
        self.entries = {}                 # session_id -> entry, for sessions loaded in memory
        self.lock = threading.Lock()      # guards self.entries and self.restore_locks
        self.restore_locks = {}           # session_id -> lock, so a session is replayed only once
        self.market = market
        self.market_lock = threading.Lock()
        self.idle_seconds = idle_seconds
        self.max_loaded = max_loaded
        self.last_sweep = time.monotonic()
        self.replays = 0                  # how many sessions have been rebuilt (for tests / health)

    def get_market(self):
        # Loaded once, on the first session, then shared (it is only ever read)
        with self.market_lock:
            if self.market is None:
                self.market = MarketLoader()
            return self.market

    def new_entry(self, session, name, created):
        return {
            "session": session,
            "lock": threading.Lock(),
            "name": name,
            "created": created,
            "last_used": time.monotonic(),
            "unloaded": False,
        }

    def create(self, body):
        settings = {"start": body.start, "end": body.end, "policy": body.policy, "limits": body.limits}
        session = Session(start=body.start, end=body.end, policy=body.policy,
                          limits=body.limits, market=self.get_market())
        session_id = uuid.uuid4().hex[:8]
        while self.journal.session_row(session_id) is not None:
            session_id = uuid.uuid4().hex[:8]
        created = now_text()
        self.journal.add_session(session_id, body.name, settings, created)
        entry = self.new_entry(session, body.name, created)
        with self.lock:
            self.entries[session_id] = entry
            over_cap = len(self.entries) > self.max_loaded
        if over_cap:
            self.sweep()          # unload the least recently used now, not at the next sweep
        return session_id, entry

    def restore(self, session_id, row):
        """Rebuilds a saved session by replaying its actions on a fresh Session."""
        settings = row["settings"]
        session = Session(start=settings["start"], end=settings["end"], policy=settings["policy"],
                          limits=settings["limits"], market=self.get_market())
        actions = self.journal.actions(session_id)
        for i in range(len(actions)):
            kind, payload = actions[i]
            try:
                apply_action(session, kind, payload)
            except ValueError as error:
                raise HTTPException(status_code=500,
                                    detail=f"could not restore session {session_id}: action {i + 1} "
                                           f"({kind}) failed on replay: {error}")
        self.replays += 1
        return self.new_entry(session, row["name"], row["created"])

    def get(self, session_id):
        """The loaded entry for a session, rebuilding it from SQLite if needed (404 if unknown)."""
        with self.lock:
            entry = self.entries.get(session_id)
        if entry is not None:
            return entry

        if self.journal.session_row(session_id) is None:
            raise HTTPException(status_code=404, detail=f"no session {session_id!r}")

        # Only one request rebuilds a given session; others wait and then use it.
        # Delete takes the same lock, so a session can't be rebuilt while it is being deleted.
        with self.restore_lock(session_id):
            with self.lock:
                entry = self.entries.get(session_id)
            if entry is not None:
                return entry
            row = self.journal.session_row(session_id)        # read again: it may have just been deleted
            if row is None:
                raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
            entry = self.restore(session_id, row)
            with self.lock:
                self.entries[session_id] = entry
            return entry

    def restore_lock(self, session_id):
        with self.lock:
            if session_id not in self.restore_locks:
                self.restore_locks[session_id] = threading.Lock()
            return self.restore_locks[session_id]

    @contextmanager
    def use(self, session_id):
        """
        Hands out a session with its lock held (for reading; changes go through act()):
            with store.use(session_id) as entry:
                entry["session"].state()
        If the session was unloaded between finding it and locking it, it is fetched again.
        """
        self.maybe_sweep()
        while True:
            entry = self.get(session_id)
            entry["lock"].acquire()
            if not entry["unloaded"]:
                break
            entry["lock"].release()
        try:
            entry["last_used"] = time.monotonic()
            yield entry
        finally:
            entry["lock"].release()

    def record(self, session_id, kind, payload):
        """Saves an action that just succeeded. Call it while still inside store.use()."""
        self.journal.add_action(session_id, kind, payload, now_text())

    def act(self, session_id, kind, payload, action):
        """
        Runs one action that changes a session, then saves it:
            result = store.act(session_id, "next", {}, do_next)    # do_next(session) -> result
        The saved actions are the truth. If the action fails after it has already changed the
        session (or the save itself fails), the copy in memory is dropped, and the next request
        rebuilds the session from the saved actions - memory never gets ahead of the database.
        A refusal (ValueError) that changed nothing just comes back as a 400.
        """
        with self.use(session_id) as entry:
            session = entry["session"]
            before = fingerprint(session)
            try:
                result = action(session)
                self.record(session_id, kind, payload)
            except Exception as error:
                if not isinstance(error, ValueError) or fingerprint(session) != before:
                    self.drop(session_id, entry)
                raise
            return result

    def drop(self, session_id, entry):
        """Forgets the in-memory copy (call with the entry's lock held); the saved session stays."""
        entry["unloaded"] = True
        with self.lock:
            if self.entries.get(session_id) is entry:
                del self.entries[session_id]

    def maybe_sweep(self):
        now = time.monotonic()
        if now - self.last_sweep >= SWEEP_EVERY:
            self.last_sweep = now
            self.sweep(now)

    def sweep(self, now=None):
        """
        Unloads sessions from memory (they stay saved): every session idle for longer than
        idle_seconds, and then the least recently used ones while more than max_loaded are
        loaded. A session busy with a request is skipped. Returns the ids unloaded.
        """
        if now is None:
            now = time.monotonic()
        with self.lock:
            items = list(self.entries.items())
        # oldest first
        items.sort(key=last_used_of)

        chosen = []
        loaded = len(items)
        for session_id, entry in items:
            idle_too_long = now - entry["last_used"] > self.idle_seconds
            over_cap = loaded > self.max_loaded
            if idle_too_long or over_cap:
                chosen.append((session_id, entry))
                loaded -= 1

        unloaded = []
        for session_id, entry in chosen:
            if not entry["lock"].acquire(blocking=False):
                continue          # in use right now; try again next sweep
            try:
                entry["unloaded"] = True
                with self.lock:
                    if self.entries.get(session_id) is entry:
                        del self.entries[session_id]
                unloaded.append(session_id)
            finally:
                entry["lock"].release()
        return unloaded

    def remove(self, session_id):
        """
        Deletes a session from memory and the database. Holds the session's restore lock (so it
        can't be rebuilt meanwhile) and, if loaded, its own lock (so no request is half-way
        through it); the saved rows are gone before either lock is released, so a request that
        was waiting finds nothing and gets a 404.
        """
        with self.restore_lock(session_id):
            with self.lock:
                entry = self.entries.get(session_id)
            if entry is not None:
                entry["lock"].acquire()
            try:
                if self.journal.session_row(session_id) is None:
                    raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
                if entry is not None:
                    self.drop(session_id, entry)
                self.journal.delete(session_id)
            finally:
                if entry is not None:
                    entry["lock"].release()
        with self.lock:
            self.restore_locks.pop(session_id, None)

    def loaded_ids(self):
        with self.lock:
            return list(self.entries.keys())


def last_used_of(item):
    return item[1]["last_used"]


def summary(session_id, entry):
    """One line per loaded session."""
    s = entry["session"]
    return {
        "session_id": session_id,
        "name": entry["name"],
        "created": entry["created"],
        "loaded": True,
        "policy": s.policy,
        "start": s.times[0].isoformat(),
        "end": s.times[-1].isoformat(),
        "time": s.now().isoformat(),
        "index": s.index,
        "fixings": len(s.times),
        "done": s.done(),
        "nav_cny": s.book.nav(s.snapshot),
        "live_positions": len(s.book.portfolio.get_options()),
        "orders": len(s.orders),
    }


def check_admin(admin_key, given):
    """403 unless the server has an admin key and the request sent the same one."""
    if not admin_key:
        raise HTTPException(status_code=403, detail="listing sessions is switched off: set FX_ADMIN_KEY on the server")
    if given is None or not secrets.compare_digest(given, admin_key):
        raise HTTPException(status_code=403, detail="admin key missing or wrong")


# ----------------------------------------------------------------------
# The app
# ----------------------------------------------------------------------

def create_app(market=None, admin_key=None, db_path=None, idle_seconds=None, max_loaded=None):
    """Each argument left as None is read from the environment (see the top of this file)."""
    if admin_key is None:
        admin_key = os.environ.get("FX_ADMIN_KEY", "")
    if db_path is None:
        db_path = os.environ.get("FX_DB_PATH", "") or "sessions.db"
        if not os.path.isabs(db_path):
            db_path = os.path.join(PROJECT_ROOT, db_path)
    if idle_seconds is None:
        idle_seconds = float(os.environ.get("FX_IDLE_MINUTES", "30")) * 60.0
    if max_loaded is None:
        max_loaded = int(os.environ.get("FX_MAX_LOADED", "200"))

    store = SessionStore(Journal(db_path), market, idle_seconds, max_loaded)
    app = FastAPI(title="FX Options Hedger",
                  description="USD/CNY options desk simulator over CFETS fixings, Jul-Sep 2026")
    app.state.store = store

    # The front end may be served from another port while developing
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(RequestValidationError)
    def bad_shape(request: Request, error: RequestValidationError):
        # A body that doesn't fit the shapes above -> 422, listing where and why. The offending
        # value itself is left out: a NaN can't be written as JSON, which would break the reply.
        details = []
        for problem in error.errors():
            details.append({"where": list(problem.get("loc", [])), "problem": problem.get("msg", "")})
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(ValueError)
    def value_error(request: Request, error: ValueError):
        # Anything the Session refuses (bad order, clock at the end, stale plan, ...)
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.get("/health")
    def health():
        return {"ok": True, "loaded": len(store.loaded_ids()), "saved": store.journal.count()}

    @app.get("/fixings")
    def fixings():
        # Every fixing in the data, for picking a session's start and end
        times = []
        for t in store.get_market().snapshot_times():
            times.append(t.isoformat())
        return {"count": len(times), "times": times}

    # ---- sessions ----

    @app.post("/sessions")
    def create_session(body: SessionIn):
        session_id, entry = store.create(body)
        return summary(session_id, entry)

    @app.get("/sessions")
    def list_sessions(x_admin_key: Optional[str] = Header(default=None)):
        # Admin only: every saved session, ids included; live details for the loaded ones
        check_admin(admin_key, x_admin_key)
        loaded = set(store.loaded_ids())
        rows = []
        for row in store.journal.all_sessions():
            session_id = row["session_id"]
            if session_id in loaded:
                try:
                    with store.use(session_id) as entry:
                        line = summary(session_id, entry)
                except HTTPException:
                    continue          # deleted while we were listing
                line["last_activity"] = row["last_activity"]
                line["actions"] = row["actions"]
            else:
                line = {"session_id": session_id, "name": row["name"], "created": row["created"],
                        "loaded": False, "policy": row["settings"]["policy"],
                        "last_activity": row["last_activity"], "actions": row["actions"]}
            rows.append(line)
        return rows

    @app.get("/sessions/{session_id}")
    def get_state(session_id: str):
        with store.use(session_id) as entry:
            state = entry["session"].state()
            state["session_id"] = session_id
            state["name"] = entry["name"]
            return state

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str):
        store.remove(session_id)
        return {"deleted": session_id}

    # ---- the clock ----

    @app.post("/sessions/{session_id}/next")
    def step(session_id: str):
        def do_next(session):
            return session.next()
        return store.act(session_id, "next", {}, do_next)

    @app.post("/sessions/{session_id}/run")
    def run(session_id: str, body: RunIn):
        def do_run(session):
            records = session.run(steps=body.steps, until=body.until)
            return {"steps": len(records), "records": records,
                    "time": session.now().isoformat(), "done": session.done()}
        return store.act(session_id, "run", body.model_dump(), do_run)

    # ---- orders ----

    @app.post("/sessions/{session_id}/quote")
    def quote(session_id: str, order: OrderIn):
        with store.use(session_id) as entry:
            return entry["session"].quote(order_payload(order))

    @app.post("/sessions/{session_id}/orders")
    def submit_order(session_id: str, order: OrderIn):
        payload = order_payload(order)

        def do_order(session):
            return session.submit_order(payload)
        return store.act(session_id, "order", payload, do_order)

    @app.get("/sessions/{session_id}/orders")
    def list_orders(session_id: str):
        with store.use(session_id) as entry:
            return entry["session"].order_rows()

    @app.delete("/sessions/{session_id}/orders/{order_id}")
    def cancel_order(session_id: str, order_id: int):
        def do_cancel(session):
            return session.cancel_order(order_id)
        return store.act(session_id, "cancel", {"order_id": order_id}, do_cancel)

    @app.post("/sessions/{session_id}/spot")
    def trade_spot(session_id: str, trade: SpotIn):
        payload = trade.model_dump(exclude_none=True)

        def do_spot(session):
            return session.trade_spot(payload)
        return store.act(session_id, "spot", payload, do_spot)

    @app.get("/sessions/{session_id}/market")
    def market(session_id: str):
        with store.use(session_id) as entry:
            return entry["session"].market_view()

    # ---- hedging ----

    @app.get("/sessions/{session_id}/hedge/recommend")
    def recommend_hedge(session_id: str):
        with store.use(session_id) as entry:
            return entry["session"].recommend_hedge()

    @app.post("/sessions/{session_id}/hedge")
    def hedge(session_id: str, body: HedgeIn):
        # The Session re-derives the hedge and only uses the plan to confirm it (prices can't be set here)
        def do_hedge(session):
            return session.hedge(body.plan)
        return store.act(session_id, "hedge", {"plan": body.plan}, do_hedge)

    @app.post("/sessions/{session_id}/hedge/delta")
    def hedge_delta(session_id: str):
        def do_delta(session):
            return {"spot": session.hedge_delta()}
        return store.act(session_id, "hedge_delta", {}, do_delta)

    # ---- reports ----

    @app.get("/sessions/{session_id}/history")
    def history(session_id: str, start: int = 0):
        # start = how many records the caller already has, so a page can fetch only new ones
        if start < 0:
            raise HTTPException(status_code=400, detail="start must not be negative")
        with store.use(session_id) as entry:
            records = entry["session"].history()
        return {"total": len(records), "start": start, "records": records[start:]}

    @app.get("/sessions/{session_id}/blotter")
    def blotter(session_id: str):
        with store.use(session_id) as entry:
            return entry["session"].blotter()

    @app.get("/sessions/{session_id}/pnl")
    def pnl(session_id: str):
        with store.use(session_id) as entry:
            return entry["session"].pnl_summary()

    # ---- the web page ----
    # The built React app (frontend/dist, made by `npm run build`) is served at "/".
    # Mounted last, so every API path above wins over a file of the same name.
    dist = os.path.join(PROJECT_ROOT, "frontend", "dist")
    if os.path.isdir(dist):
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()