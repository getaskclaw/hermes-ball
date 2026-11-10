"""Ball state machine + hook/command handlers. Every entry point swallows exceptions."""
from __future__ import annotations

import functools
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import judge
from .store import Store

logger = logging.getLogger("hermes_plugins.ball")


def safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:  # observe-only: never raise into Hermes
            logger.warning("ball: %s failed", fn.__name__, exc_info=True)
            return None
    return wrapper


def _s(v: Any) -> str:
    v = getattr(v, "value", v)
    return "" if v is None else str(v)


def default_profile() -> str:
    home = os.environ.get("HERMES_HOME", "")
    name = Path(home).name if home else ""
    if name and name != ".hermes":
        return name
    return "default"


def thread_key(source) -> str:
    profile = _s(getattr(source, "profile", None)) or default_profile()
    return ":".join([profile, _s(getattr(source, "platform", None)),
                     _s(getattr(source, "chat_id", None)), _s(getattr(source, "thread_id", None))])


def fmt_age(since: str, now: Optional[datetime] = None) -> str:
    try:
        t = datetime.fromisoformat(since)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = max(0, int(((now or datetime.now(timezone.utc)) - t).total_seconds()))
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _who(ball: str) -> str:
    return "you" if ball == "human" else "AI"


def render_one(row: Optional[dict], now: Optional[datetime] = None) -> str:
    if not row:
        return "🏀 Ball: no turns tracked in this thread yet."
    s = f"🏀 Ball: {_who(row['ball'])} · {fmt_age(row['since'], now)}"
    if row["ball"] == "ai" and row.get("promise"):
        s += " ⚠ empty promise"
    if row.get("note"):
        s += f" — {row['note']}"
    return s


def render_board(rows: list[dict], now: Optional[datetime] = None) -> str:
    if not rows:
        return "🏀 Ball board is empty — no threads tracked yet."
    rows = sorted(rows, key=lambda r: (0 if r["ball"] == "human" else 1, r["since"]))
    waiting = sum(1 for r in rows if r["ball"] == "human")
    out = [f"🏀 Ball board — {waiting} waiting on you, {len(rows) - waiting} with AI"]
    for r in rows:
        flag = " ⚠" if r["ball"] == "ai" and r.get("promise") else ""
        note = f" — {r['note']}" if r.get("note") else ""
        out.append(f"{'you' if r['ball'] == 'human' else 'AI '} · {fmt_age(r['since'], now):>4}{flag} · {r['key']}{note}")
    return "\n".join(out)


class Ball:
    """Session -> thread mapping, public SessionStore API only (no private gateway attributes):

    * pre_gateway_dispatch gives ``session_store`` (the gateway's SessionStore); Ball keeps a reference.
    * post_llm_call gives only ``session_id`` -> ``session_store.lookup_by_session_id(sid)`` returns the
      SessionEntry whose ``origin`` is the SessionSource (platform/chat_id/thread_id/profile) -> thread key.
    * on_human_input_* / agent_loop_stopped give ``session_key`` -> ``lookup_by_session_key(key).origin``.
    * Results are cached in memory (session_key/session_id -> thread key); the /ball command also teaches
      the cache from its ``source``/``session_key``/``session_id`` kwargs. Nothing about sessions is persisted.
    """

    def __init__(self, store: Store, ctx: Any = None):
        self.store = store
        self.ctx = ctx
        self.by_session_key: dict[str, str] = {}
        self.by_session_id: dict[str, str] = {}
        self._session_store = None
        self._lock = threading.Lock()

    # ── mapping ──
    def _learn(self, tkey: str, session_key: Optional[str] = None, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_key:
                self.by_session_key[str(session_key)] = tkey
            if session_id:
                self.by_session_id[str(session_id)] = tkey

    def _from_entry(self, entry) -> Optional[str]:
        origin = getattr(entry, "origin", None)
        if entry is None or origin is None:
            return None
        tkey = thread_key(origin)
        self._learn(tkey, getattr(entry, "session_key", None), getattr(entry, "session_id", None))
        return tkey

    def _lookup(self, method: str, value) -> Optional[str]:
        fn = getattr(self._session_store, method, None)
        if not value or not callable(fn):
            return None
        try:
            return self._from_entry(fn(str(value)))
        except Exception:
            logger.debug("ball: %s failed", method, exc_info=True)
            return None

    def _resolve(self, session_key=None, session_id=None) -> Optional[str]:
        t = (self.by_session_key.get(str(session_key)) if session_key else None) \
            or self._lookup("lookup_by_session_key", session_key) \
            or (self.by_session_id.get(str(session_id)) if session_id else None) \
            or self._lookup("lookup_by_session_id", session_id)
        if t:
            self._learn(t, session_key=session_key, session_id=session_id)
        return t

    # ── hooks ──
    @safe
    def on_pre_gateway_dispatch(self, event=None, gateway=None, session_store=None, **_):
        if session_store is not None:
            self._session_store = session_store
        source = getattr(event, "source", None)
        if source is None:
            return None
        self.store.set(thread_key(source), "ai", note="you spoke")
        return None  # never rewrite/skip the message

    @safe
    def on_human_input_request(self, kind=None, prompt=None, session_key=None, session_id=None, **_):
        tkey = self._resolve(session_key, session_id)
        if not tkey:
            return None
        note = f"{kind or 'input'}: {(prompt or '')[:80]}".strip().rstrip(":")
        self.store.set(tkey, "human", note=note)
        return None

    @safe
    def on_human_input_resolved(self, kind=None, outcome=None, session_key=None, session_id=None, **_):
        tkey = self._resolve(session_key, session_id)
        if not tkey:
            return None
        self.store.set(tkey, "ai", note=f"{kind or 'input'} {outcome or 'resolved'}")
        return None

    @safe
    def on_agent_loop_stopped(self, session_key=None, reason=None, **_):
        tkey = self._resolve(session_key=session_key)
        if not tkey:
            return None
        self.store.set(tkey, "human", note=f"interrupted: {reason or 'unknown'}")
        return None

    @safe
    def on_post_llm_call(self, session_id=None, assistant_response=None, **_):
        tkey = self._resolve(session_id=session_id)
        if not tkey:
            logger.debug("ball: post_llm_call for unmapped session_id %s", session_id)
            return None
        verdict = judge.heuristic(assistant_response or "")
        row = self.store.set(tkey, verdict["ball"], promise=verdict["promise"], note=verdict["note"])
        if self._cfg("llm_judge", False) and getattr(self.ctx, "llm", None) is not None:
            t = threading.Thread(target=self._run_llm_judge, name="ball-judge", daemon=True,
                                 args=(tkey, assistant_response or "", row["updated"], verdict))
            t.start()
            return t  # return value of post_llm_call is ignored by Hermes; handy for tests
        return None

    def _cfg(self, key, default):
        try:
            v = self.ctx.get_config(key, default) if self.ctx is not None else default
            return default if v is None else v
        except Exception:
            return default

    @safe
    def _run_llm_judge(self, tkey: str, text: str, stamp: str, fallback: dict):
        try:
            timeout = float(self._cfg("llm_judge_timeout", 20))
        except (TypeError, ValueError):
            timeout = 20.0
        try:
            res = judge.llm_judge(self.ctx.llm, text, timeout)
        except Exception:
            logger.info("ball: llm judge error", exc_info=True)
            res = None
        if res is None:
            self.store.set_if(tkey, stamp, fallback["ball"], promise=fallback["promise"], note="judge failed")
            return
        # newer state (user spoke, interrupt...) wins: set_if is a no-op if the row moved on
        self.store.set_if(tkey, stamp, res["ball"], promise=res["promise"] if res["ball"] == "ai" else None,
                          note=res["note"] or fallback["note"])

    # ── command ──
    @safe
    def command(self, raw_args: str = "", source=None, session_id=None, session_key=None, **_):
        args = (raw_args or "").strip().lower()
        if args in ("all", "board"):
            return render_board(self.store.all())
        tkey = None
        if source is not None:
            tkey = thread_key(source)
            self._learn(tkey, session_key=session_key, session_id=session_id)
        else:
            tkey = self._resolve(session_key, session_id)
        if not tkey:
            return "🏀 Ball: no chat thread here. Try `/ball all` for the board."
        return render_one(self.store.get(tkey))
