"""Ball state machine + hook/command handlers. Every entry point swallows exceptions."""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
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


def _short_hash(v: str) -> str:
    return "#" + hashlib.sha1(v.encode("utf-8", "replace")).hexdigest()[:4]


def thread_label(source) -> str:
    """Human-readable name from public SessionSource fields; never the raw chat id.

    chat_name (room/channel/group display name) > DM (user_name or 'DM') > platform · #hash(chat_id).
    Thread suffix: chat_topic (e.g. Telegram forum topic name) if known, else thread #hash.
    """
    platform = _s(getattr(source, "platform", None)) or "?"
    name = _s(getattr(source, "chat_name", None)).strip()
    chat_type = _s(getattr(source, "chat_type", None))
    if name:
        label = f"{name} ({platform})"
    elif chat_type == "dm":
        user = _s(getattr(source, "user_name", None)).strip()
        label = f"DM {user} ({platform})" if user else f"DM ({platform})"
    else:
        label = f"{platform} · {_short_hash(_s(getattr(source, 'chat_id', None)))}"
    tid = _s(getattr(source, "thread_id", None))
    if tid:
        # chat_topic is untrusted display text: collapse newlines/whitespace runs.
        topic = " ".join(_s(getattr(source, "chat_topic", None)).split())
        label += f" · {_trunc(topic, 40)}" if topic else f" · thread {_short_hash(tid)}"
    return label


def label_from_key(key: str) -> str:
    """Fallback for rows stored before labels existed: ``profile:platform:chat_id:thread_id``."""
    parts = (key or "").split(":")
    if len(parts) < 3:
        return _short_hash(key or "")
    platform, chat, tid = parts[1] or "?", ":".join(parts[2:-1]) if len(parts) > 3 else parts[2], \
        parts[-1] if len(parts) > 3 else ""
    out = f"{platform} · {_short_hash(chat)}"
    return out + (f" · thread {_short_hash(tid)}" if tid else "")


def _trunc(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def is_command(text: Any) -> bool:
    """True for gateway commands (`/x ...`, or a raw `!x` if a platform did not rewrite it)."""
    t = (text or "").lstrip() if isinstance(text, str) else ""
    return len(t) > 1 and t[0] in "/!" and (t[1].isalpha() or t[1] == "_")


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


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff]")
_PLATFORM_SUFFIX = re.compile(r" \((?:[a-z_]+)\)(?= ·|$)")
TOP = 3

L = {
    "zh": dict(your="🏀 轮到你", working="🤖 AI 在干活", stalled="⚠️ AI 卡住了", calm="不用管",
               asks="它问你：", replied="它已回复，等你看", interrupted="已中断，等你决定",
               stall_why="说了「{p}」但没动静 · 回一句 go 催它", none="🏀 这个对话还没有记录。",
               nothread="🏀 这里不是对话。试试 !ball all", head="🏀 {n} 个对话在等你", empty="✅ 没有对话在等你",
               more="…还有 {n} 个：!ball more", stall_line="⚠️ {n} 个 AI 卡住了：{names} — 去那里回 go",
               work_line="🤖 {n} 个 AI 在干活", sep="、", chat="对话 {c}", short_replied="已回复",
               done_one="✅ 已答完 · {age} — 无需你处理", done_line="💤 {n} 个已答完，无需你处理",
               done_head="💤 {n} 个已答完，无需你处理", done_empty="💤 没有已答完的对话",
               short_interrupted="已中断",
               help="球在谁手里？— 告诉你哪些 AI 对话在等你。\n"
                    "!ball — 这个对话轮到谁\n!ball all — 等你的前 3 个\n!ball more — 全部等你的对话\n"
                    "!ball done — 已答完、无需你处理的对话\n"
                    "!ball all hide — 隐藏对话名（方便截图）"),
    "en": dict(your="🏀 Your move", working="🤖 AI is working", stalled="⚠️ AI stalled", calm="nothing to do",
               asks="It asks: ", replied="It replied — your turn to read", interrupted="Interrupted — your call",
               stall_why="said \u201c{p}\u201d but went quiet · reply go to nudge it",
               none="🏀 No turns tracked in this chat yet.", nothread="🏀 Not a chat. Try !ball all",
               head="🏀 {n} chat{s} waiting on you", empty="✅ No chats waiting on you",
               more="…{n} more: !ball more", stall_line="⚠️ {n} AI stalled: {names} — reply go there",
               work_line="🤖 {n} AI working", sep=", ", chat="Chat {c}", short_replied="replied",
               done_one="✅ Done · {age} — nothing needed", done_line="💤 {n} done, nothing needed",
               done_head="💤 {n} done, nothing needed", done_empty="💤 No done chats",
               short_interrupted="interrupted",
               help="Whose turn is it? — shows which AI chats are waiting on you.\n"
                    "!ball — this chat\n!ball all — top 3 waiting on you\n!ball more — every chat waiting on you\n"
                    "!ball done — chats the AI answered, nothing needed from you\n"
                    "!ball all hide — hide chat names (for screenshots)"),
}


def has_cjk(s: Any) -> bool:
    return bool(isinstance(s, str) and _CJK.search(s))


def row_lang(row: Optional[dict]) -> Optional[str]:
    if not row:
        return None
    if row.get("lang") in ("zh", "en"):
        return row["lang"]
    return "zh" if has_cjk(row.get("note")) or has_cjk(row.get("promise")) else None


def pick_lang(setting: Any, rows: list, here: Optional[dict] = None) -> str:
    """language: zh|en forces; auto = this chat's language (user's last message / note), else zh if any chat is."""
    setting = str(setting or "auto").lower()
    if setting in ("zh", "en"):
        return setting
    lang = row_lang(here)
    if lang:
        return lang
    return "zh" if any(row_lang(r) == "zh" for r in rows) else "en"


def display_name(row: dict) -> str:
    label = row.get("label") or label_from_key(row["key"])
    return _PLATFORM_SUFFIX.sub("", label).strip()


def short_note(row: dict, lang: str) -> str:
    """What the waiting chat wants, always short. Old long notes go through the same extractor."""
    note = (row.get("note") or "").strip()
    t = L[lang]
    low = note.lower()
    if low.startswith("interrupted"):
        return t["short_interrupted"]
    q = judge.extract_note(note)
    return t["short_replied"] if q == judge.REPLIED else q


def is_done(row: dict) -> bool:
    """ball=human but the AI just answered and asked nothing: nothing is waiting on the user."""
    if row.get("ball") != "human":
        return False
    note = (row.get("note") or "").strip()
    return not note.lower().startswith("interrupted") and judge.extract_note(note) == judge.REPLIED


def render_one(row: Optional[dict], now: Optional[datetime] = None, lang: Optional[str] = None) -> str:
    lang = lang or pick_lang("auto", [], row)
    t = L[lang]
    if not row:
        return t["none"]
    age = fmt_age(row["since"], now)
    if row["ball"] == "ai" and row.get("promise"):
        return f"{t['stalled']} · {age}\n" + t["stall_why"].format(p=judge._cut(row["promise"], 20))
    if row["ball"] == "ai":
        return f"{t['working']} · {age} — {t['calm']}"
    if is_done(row):
        return t["done_one"].format(age=age)
    note = (row.get("note") or "").strip()
    line2 = t["interrupted"] if note.lower().startswith("interrupted") else t["asks"] + judge.extract_note(note)
    return f"{t['your']} · {age}\n{line2}"


def render_board(rows: list[dict], now: Optional[datetime] = None, lang: Optional[str] = None,
                 full: bool = False, hide: bool = False, done: bool = False,
                 show_done: bool = False) -> str:
    """done=True: only the answered-nothing-needed chats; show_done=True: also list them after the waiting ones."""
    lang = lang or pick_lang("auto", rows)
    t = L[lang]
    human = sorted((r for r in rows if r["ball"] == "human"), key=lambda r: r["since"])
    waiting = [r for r in human if not is_done(r)]
    finished = [r for r in human if is_done(r)]
    stalled = sorted((r for r in rows if r["ball"] == "ai" and r.get("promise")), key=lambda r: r["since"])
    working = [r for r in rows if r["ball"] == "ai" and not r.get("promise")]
    names: dict[str, str] = {}

    def name(r):
        if not hide:
            return display_name(r)
        if r["key"] not in names:
            i = len(names)
            names[r["key"]] = t["chat"].format(c=chr(65 + i) if i < 26 else str(i + 1))
        return names[r["key"]]

    if done:
        if not finished:
            return t["done_empty"]
        return "\n".join([t["done_head"].format(n=len(finished))] +
                         [f"{i}. {name(r)} · {fmt_age(r['since'], now)}" for i, r in enumerate(finished, 1)])

    out = [t["head"].format(n=len(waiting), s="" if len(waiting) == 1 else "s") if waiting else t["empty"]]
    shown = waiting if full else waiting[:TOP]
    for i, r in enumerate(shown, 1):
        out.append(f"{i}. {name(r)} · {fmt_age(r['since'], now)} — {short_note(r, lang)}")
    if len(waiting) > len(shown):
        out.append(t["more"].format(n=len(waiting) - len(shown)))
    if stalled:
        out.append(t["stall_line"].format(n=len(stalled), names=t["sep"].join(name(r) for r in stalled)))
    if working:
        out.append(t["work_line"].format(n=len(working)))
    if finished and show_done:
        out.append(t["done_head"].format(n=len(finished)))
        out.extend(f"{i}. {name(r)} · {fmt_age(r['since'], now)}" for i, r in enumerate(finished, 1))
    elif finished:
        out.append(t["done_line"].format(n=len(finished)))
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
        if is_command(getattr(event, "text", None)):
            # `/ball`, Matrix `!ball` (rewritten to `/ball` by the adapter) and every other slash/bang command
            # are control traffic, not a conversational turn: they must not move the ball, so `/ball` reports
            # the state as it was *before* the query.
            return None
        text = getattr(event, "text", None)
        lang = ("zh" if has_cjk(text) else "en") if isinstance(text, str) and text.strip() else None
        self.store.set(thread_key(source), "ai", note="you spoke", label=thread_label(source), lang=lang)
        return None  # never rewrite/skip the message

    @safe
    def on_human_input_request(self, kind=None, prompt=None, session_key=None, session_id=None, **_):
        tkey = self._resolve(session_key, session_id)
        if not tkey:
            return None
        q = judge.extract_note(prompt or "")
        note = q if q != judge.REPLIED else judge._cut(f"{kind or 'input'}: {(prompt or '').strip()}".strip(": "))
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
            # INFO, not DEBUG: an unmapped turn end silently leaves the row stale (e.g. right after a hot
            # load, before any inbound message has handed Ball the gateway's session_store).
            logger.info("ball: post_llm_call for unmapped session_id %s (session_store %s) — turn not recorded",
                        session_id, "known" if self._session_store is not None else "not yet seen")
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
        words = (raw_args or "").strip().lower().split()
        sub = words[0] if words else ""
        hide = "hide" in words[1:]
        here = thread_key(source) if source is not None else self._resolve(session_key, session_id)
        if source is not None and here:
            self._learn(here, session_key=session_key, session_id=session_id)
        setting = self._cfg("language", "auto")
        if sub in ("help", "?"):
            row = self.store.get(here) if here else None
            return L[pick_lang(setting, self.store.all(), row)]["help"]
        if sub in ("all", "board", "more", "done"):
            rows = self.store.all()
            here_row = next((r for r in rows if r["key"] == here), None)
            return render_board(rows, lang=pick_lang(setting, rows, here_row), full=sub == "more", hide=hide,
                                done=sub == "done", show_done=sub == "more" and "all" in words[1:])
        if not here:
            return L[pick_lang(setting, self.store.all())]["nothread"]
        row = self.store.get(here)
        return render_one(row, lang=pick_lang(setting, self.store.all() if not row else [], row))
