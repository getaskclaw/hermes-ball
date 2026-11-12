"""Ball state machine + hook/command handlers. Every entry point swallows exceptions."""
from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import judge
from .store import Store, StoreWriteError

logger = logging.getLogger("hermes_plugins.ball")


def safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except StoreWriteError:  # recorded + logged once per episode by the Store; shown on !ball
            logger.debug("ball: %s write failed", fn.__name__)
            return None
        except Exception:  # observe-only: never raise into Hermes
            logger.warning("ball: %s failed", fn.__name__, exc_info=True)
            return None
    return wrapper


def _s(v: Any) -> str:
    v = getattr(v, "value", v)
    return "" if v is None else str(v)


def _hermes_home() -> Path:
    """Profile-aware home: contextvar override (multiplex gateway) -> HERMES_HOME -> platform default.
    A plain ``os.environ`` read returns the launch profile under a multiplex gateway, so profile-aware
    paths must go through ``hermes_constants.get_hermes_home()``; the fallback keeps tests runnable
    without hermes-agent installed."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def default_profile() -> str:
    name = _hermes_home().name
    if name and name != ".hermes":
        return name
    return "default"


def thread_key(source) -> str:
    profile = _s(getattr(source, "profile", None)) or default_profile()
    return ":".join([profile, _s(getattr(source, "platform", None)),
                     _s(getattr(source, "chat_id", None)), _s(getattr(source, "thread_id", None))])


def _short_hash(v: str) -> str:
    return "#" + hashlib.sha1(v.encode("utf-8", "replace")).hexdigest()[:4]


def thread_label(source, title_hint: str = "") -> str:
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
        hint = " ".join(_s(title_hint).split())
        if topic:
            label += f" · {_trunc(topic, 40)}"
        elif hint:  # Hermes' own session title, used when the platform gives no topic name
            label += " · " + _trunc(hint, 40)
        else:
            label += f" · thread {_short_hash(tid)}"
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


_ZH_UNIT = {"s": "秒", "m": "分钟", "h": "小时", "d": "天"}


def zh_age(age: str) -> str:
    """'13m' -> '13分钟' (Chinese boards read better with words than unit letters)."""
    return age[:-1] + _ZH_UNIT[age[-1]] if age[-1:] in _ZH_UNIT and age[:-1].isdigit() else age


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


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_KANA = re.compile(r"[\u3040-\u30ff]")  # Japanese text also uses Han; kana means "not Chinese"
_PLATFORM_SUFFIX = re.compile(r" \((?:[a-z_]+)\)(?= ·|$)")
TOP = 3
BALL_CMD = re.compile(r"^\s*[!/]ball(?=$|\s)(.*)$", re.I | re.S)

L = {
    "zh": dict(your="🏀 轮到你", working="🤖 AI 在干活", stalled="⚠️ AI 卡住了", calm="不用管",
               asks="它问你：", replied="它已回复，等你看", interrupted="已中断，等你决定",
               stall_why="说了「{p}」但没动静 · 回一句 go 催它", none="🏀 这个聊天还没有记录：Hermes 回过话之后才开始记。想看等你的对话，发 !ball",
               nothread="🏀 这里不是对话。试试 !ball", head="🏀 {n} 个对话在等你", empty="✅ 没有对话在等你",
               more="…还有 {n} 个：!ball all", stall_head="⚠️ {n} 个 AI 卡住了 — 去那里回 go", stall_more="…还有 {n} 个卡住的：!ball all",
               work_line="🤖 {n} 个 AI 在干活", sep="、", chat="对话 {c}", short_replied="已回复",
               done_one="✅ 已答完 · {age} — 不用你回", done_line="💤 {n} 个已答完，不用你回",
               done_head="💤 {n} 个已答完，不用你回", done_age="{age}前", done_empty="💤 没有已答完的对话",
               done_more="…还有 {n} 个更早的：!ball done all",
               quiet="⚠️ AI {age} 没动静 — 可能已停止（重启？）· 回一句 go 催它",
               write_fail="⚠️ Ball 自 {t} UTC 起无法保存（{err}，磁盘满了？）— 看板可能过时",
               read_fail="⚠️ Ball 读不到它的数据库（{err}）",
               origin_fail="⚠️ 自 {t} UTC 起 Ball 无法读取 Hermes 会话来源（{err}），部分回合可能漏记",
               short_interrupted="已中断", cli="自动任务", short_ask="在等你回复", forgot_stale="🗑 已删除 {n} 个隐藏的闲置对话", forget_stale_dm="请在私聊里发 !ball forget stale", stale_line="🗄 {n} 个 {d} 天无动静的对话已隐藏（!ball forget stale 可删除）",
               hidden_here="🔒 这里隐藏了 {n} 个其他对话 — 在私聊里用 !ball all 查看",
               forgot="🗑 已从看板删除这个对话", forgot_none="这个对话没有记录",
               help="球在谁手里？— 告诉你哪些 AI 对话在等你。\n"
                    "!ball — 等你的前 3 个和各项计数\n!ball all — 全部等你的对话\n!ball here — 这个对话轮到谁\n"
                    "!ball done — 最近 10 个已答完的对话（!ball done all 看全部）\n"
                    "!ball all hide — 隐藏对话名（方便截图）\n!ball forget — 从看板删除这个对话\n!ball forget stale — 删除所有已隐藏的闲置对话"),
    "en": dict(your="🏀 Your move", working="🤖 AI is working", stalled="⚠️ AI stalled", calm="nothing to do",
               asks="It asks: ", replied="It replied — your turn to read", interrupted="Interrupted — your call",
               stall_why="said \u201c{p}\u201d but went quiet · send \"go\" to nudge it",
               none="🏀 Nothing tracked here yet: Ball starts once Hermes replies in this chat. To see chats waiting on you, send !ball", nothread="🏀 Not a chat. Try !ball",
               head="🏀 {n} chat{s} waiting on you", empty="✅ No chats waiting on you",
               more="…{n} more: !ball all", stall_head="⚠️ {n} AI stalled — send \"go\" there", stall_more="…{n} more stalled: !ball all",
               work_line="🤖 {n} AI working", sep=", ", chat="Chat {c}", short_replied="replied",
               done_one="✅ Answered · {age} — nothing for you to do", done_line="💤 {n} answered — nothing for you to do",
               done_head="💤 {n} answered — nothing for you to do", done_age="{age} ago", done_empty="💤 No answered chats",
               done_more="…{n} older: !ball done all",
               quiet="⚠️ AI quiet {age} — may have stopped (restart?) · send \"go\" to nudge it",
               write_fail="⚠️ Ball couldn't save since {t} UTC ({err}, disk full?) — board may be out of date",
               read_fail="⚠️ Ball couldn't read its database ({err})",
               origin_fail="⚠️ Since {t} UTC Ball can't read Hermes session origins ({err}); some turns may be missed",
               short_interrupted="interrupted", cli="auto job", short_ask="waiting for your reply", forgot_stale="🗑 Deleted {n} hidden idle chats", forget_stale_dm="Run !ball forget stale in your DM", stale_line="🗄 {n} idle >{d}d hidden (!ball forget stale deletes them)",
               hidden_here="🔒 {n} other chats hidden here — use !ball all in your DM",
               forgot="🗑 Removed this chat from the board", forgot_none="Nothing tracked for this chat",
               help="Whose turn is it? — shows which AI chats are waiting on you.\n"
                    "!ball — top 3 waiting on you, plus counts\n!ball all — every chat waiting on you\n!ball here — this chat\n"
                    "!ball done — the 10 latest answered chats (!ball done all: every one)\n"
                    "!ball all hide — hide chat names (for screenshots)\n!ball forget — remove this chat from the board\n!ball forget stale — delete all hidden idle chats"),
}


def has_cjk(s: Any) -> bool:
    return bool(isinstance(s, str) and _CJK.search(s) and not _KANA.search(s))


def row_lang(row: Optional[dict]) -> Optional[str]:
    if not row:
        return None
    if row.get("lang") in ("zh", "en"):
        return row["lang"]
    return "zh" if has_cjk(row.get("note")) or has_cjk(row.get("promise")) else None


_SUBS = r"(?: (?:all|done|more|here|help|forget|stale|hide))*"
_CMD = re.compile(r"(?<![`\w])!ball" + _SUBS + r"(?![\w`])")


def _code_cmds(text: str) -> str:
    """Hints show commands as code: on Telegram a bare `/ball all` makes only `/ball` tappable (sending the
    wrong command); a code span is tap-to-copy and keeps the whole command."""
    return _CMD.sub(lambda m: f"`{m.group(0)}`", text)


for _lang in L.values():
    for _k, _v in list(_lang.items()):
        if isinstance(_v, str):
            _lang[_k] = _code_cmds(_v)


def pick_lang(setting: Any, rows: list, here: Optional[dict] = None) -> str:
    """language: zh|en forces; auto = this chat's language (user's last message / note), else zh if any chat is."""
    setting = str(setting or "auto").lower()
    if setting in ("zh", "en"):
        return setting
    lang = row_lang(here)
    if lang:
        return lang
    return "zh" if any(row_lang(r) == "zh" for r in rows) else "en"


def _is_dm_topic(key: str) -> bool:
    """Telegram DM topic: thread set and a positive (user) chat id; groups/supergroups are negative."""
    parts = (key or "").split(":")
    return len(parts) >= 4 and parts[1] == "telegram" and bool(parts[-1]) and parts[-2].isdigit()


# Hermes' gateway strips bare local file paths (``~/x.yaml``, ``/a/b.png``) out of replies and tries to send
# them as attachments (gateway/platforms/base.py extract_local_files); it skips paths inside code spans.
_PATHLIKE = re.compile(r"(?<![/:\w.`])(?:~/|/|[A-Za-z]:[/\\])[A-Za-z0-9_.~/\\-]*[A-Za-z0-9_/]")  # ASCII path chars only


def _protect_paths(label: str) -> str:
    return _PATHLIKE.sub(lambda m: "`" + m.group(0).replace("`", "") + "`", label)


def display_name(row: dict) -> str:
    label = row.get("label") or label_from_key(row["key"])
    if _is_dm_topic(row.get("key", "")) and " · " in label:
        label = label.split(" · ", 1)[1].replace("`", "")  # every DM topic is with you: the "<your name> ·" prefix is noise
    return _protect_paths(_PLATFORM_SUFFIX.sub("", label).strip())


def row_state(row: dict) -> str:
    """Stored classification (decided from the full reply before truncation); legacy rows fall back."""
    st = row.get("state")
    return st if st in ("asked", "replied", "working", "interrupted") else judge.legacy_state(row)


def _asked_note(row: dict) -> str:
    """The stored question. Never re-classified from the (possibly truncated) note."""
    note = judge._LEGACY.sub("", (row.get("note") or "").strip())
    q = judge.extract_note(note)
    return q if q != judge.REPLIED else (judge._cut(judge._clean(note)) or "?")


_LEAD_IN = re.compile(r"^\s*(?:下一步|接下来|next(?: step)?)\s*[:：]\s*", re.IGNORECASE)


def _strip_lead_in(note: str) -> str:
    """'下一步： 需你拍板' -> '需你拍板': the lead-in carries no information on a one-line board."""
    out = _LEAD_IN.sub("", note or "")
    return out or note


def short_note(row: dict, lang: str) -> str:
    """What the waiting chat wants, always short."""
    t = L[lang]
    st = row_state(row)
    if st == "interrupted":
        return t["short_interrupted"]
    if st == "asked":
        n = _strip_lead_in(_asked_note(row))
        # legacy rows: an ask with no extractable question was stored with the 'replied' sentinel
        return t["short_ask"] if n in (judge.REPLIED, "?", "") else n
    return t["short_replied"]


def is_done(row: dict) -> bool:
    """ball=human but the AI just answered and asked nothing: nothing is waiting on the user."""
    return row.get("ball") == "human" and row_state(row) == "replied"


def _parse(ts: Any) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def split_stale(rows: list, stale_days: float, now: Optional[datetime] = None) -> tuple:
    """(fresh, stale): rows whose last update is older than stale_days are hidden (never deleted)."""
    if not stale_days or stale_days <= 0:
        return list(rows), []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=stale_days)
    fresh, stale = [], []
    for r in rows:
        t = _parse(r.get("updated")) or _parse(r.get("since"))
        (stale if t is not None and t < cutoff else fresh).append(r)
    return fresh, stale


WORKING_TIMEOUT_MIN = 30


def is_quiet(row: dict, timeout_min: float = WORKING_TIMEOUT_MIN, now: Optional[datetime] = None) -> bool:
    """A 'working' row (ball ai, no promise) untouched for longer than the timeout: the turn probably died
    (e.g. gateway restart mid-turn, so post_llm_call never fired). Display only; the row is not changed."""
    if not timeout_min or timeout_min <= 0 or row.get("ball") != "ai" or row.get("promise"):
        return False
    t = _parse(row.get("updated")) or _parse(row.get("since"))
    return t is not None and t < (now or datetime.now(timezone.utc)) - timedelta(minutes=timeout_min)


PROMISE_GRACE_MIN = 2


def in_promise_grace(row: dict, grace_min: float = PROMISE_GRACE_MIN, now: Optional[datetime] = None) -> bool:
    """A promise row younger than the grace period is the AI still working on it, not stalled. Display only."""
    if not grace_min or grace_min <= 0 or row.get("ball") != "ai" or not row.get("promise"):
        return False
    t = _parse(row.get("updated")) or _parse(row.get("since"))
    return t is not None and t > (now or datetime.now(timezone.utc)) - timedelta(minutes=grace_min)


def is_stalled(row: dict, grace_min: float = PROMISE_GRACE_MIN, now: Optional[datetime] = None) -> bool:
    return row.get("ball") == "ai" and bool(row.get("promise")) and not in_promise_grace(row, grace_min, now)


def render_one(row: Optional[dict], now: Optional[datetime] = None, lang: Optional[str] = None,
               working_timeout_min: float = WORKING_TIMEOUT_MIN, promise_grace_min: float = PROMISE_GRACE_MIN) -> str:
    lang = lang or pick_lang("auto", [], row)
    t = L[lang]
    if not row:
        return t["none"]
    age = fmt_age(row["since"], now)
    if is_stalled(row, promise_grace_min, now):
        return f"{t['stalled']} · {age}\n" + t["stall_why"].format(p=judge._cut(row["promise"], 20))
    if is_quiet(row, working_timeout_min, now):
        return t["quiet"].format(age=fmt_age(row.get("updated") or row["since"], now))
    if row["ball"] == "ai":
        return f"{t['working']} · {age} — {t['calm']}"
    if is_done(row):
        return t["done_one"].format(age=age)
    line2 = t["interrupted"] if row_state(row) == "interrupted" else t["asks"] + _asked_note(row)
    return f"{t['your']} · {age}\n{line2}"


DONE_SHOWN = 10  # /ball done lists the latest 10; `/ball done all` lists every one


def render_board(rows: list[dict], now: Optional[datetime] = None, lang: Optional[str] = None,
                 full: bool = False, hide: bool = False, done: bool = False,
                 show_done: bool = False, stale: int = 0, stale_days: float = 0,
                 working_timeout_min: float = WORKING_TIMEOUT_MIN, promise_grace_min: float = PROMISE_GRACE_MIN,
                 hidden: int = 0, bold: bool = False) -> str:
    """hidden: rows from other chats withheld in a group room (privacy); a pointer line is appended.
    done=True: only the answered-nothing-needed chats; show_done=True: also list them after the waiting ones."""
    lang = lang or pick_lang("auto", rows)
    t = L[lang]
    human = sorted((r for r in rows if r["ball"] == "human"), key=lambda r: r["since"])
    waiting = [r for r in human if not is_done(r)]
    finished = sorted((r for r in human if is_done(r)), key=lambda r: r["since"], reverse=True)  # latest first
    stalled = sorted((r for r in rows if is_stalled(r, promise_grace_min, now)
                      or is_quiet(r, working_timeout_min, now)), key=lambda r: r["since"])
    working = [r for r in rows if r["ball"] == "ai" and r not in stalled]
    names: dict[str, str] = {}

    def name(r):
        if not hide:
            x = display_name(r)
            if r.get("key", "").split(":")[1:2] == ["local"] and x.startswith("local"):
                x = t["cli"] + x[len("local"):]  # only seen with show_local or from a local run
            return f"**{x.replace('*', '')}**" if bold else x
        if r["key"] not in names:
            i = len(names)
            names[r["key"]] = t["chat"].format(c=chr(65 + i) if i < 26 else str(i + 1))
        return names[r["key"]]

    def _age(r):
        a = fmt_age(r["since"], now)
        return zh_age(a) if t is L["zh"] else a

    tail = [t["hidden_here"].format(n=hidden)] if hidden else []
    if done:
        if not finished:
            return "\n".join([t["done_empty"]] + tail)
        shown = finished if full else finished[:DONE_SHOWN]  # latest first, so the cut drops the oldest
        more = [t["done_more"].format(n=len(finished) - len(shown))] if len(shown) < len(finished) else []
        return "\n".join([t["done_head"].format(n=len(finished))] +
                          [f"{i}. {name(r)} · {t['done_age'].format(age=_age(r))}"
                           for i, r in enumerate(shown, 1)] + more + tail)

    out = [t["head"].format(n=len(waiting), s="" if len(waiting) == 1 else "s") if waiting else t["empty"]]
    shown = waiting if full else waiting[:TOP]
    for i, r in enumerate(shown, 1):
        out.append(f"{i}. {name(r)} · {fmt_age(r['since'], now)} — {short_note(r, lang)}")
    if len(waiting) > len(shown):
        out.append(t["more"].format(n=len(waiting) - len(shown)))
    if stalled:  # one numbered line each: names contain "·" and "/", so a comma-joined list was unreadable
        out.append(t["stall_head"].format(n=len(stalled)))
        st_shown = stalled if full else stalled[:TOP]
        out.extend(f"{i}. {name(r)} · {fmt_age(r['since'], now)}" for i, r in enumerate(st_shown, 1))
        if len(stalled) > len(st_shown):
            out.append(t["stall_more"].format(n=len(stalled) - len(st_shown)))
    if working:
        out.append(t["work_line"].format(n=len(working)))
    if finished and show_done:
        out.append(t["done_head"].format(n=len(finished)))
        out.extend(f"{i}. {name(r)} · {t['done_age'].format(age=_age(r))}"
                   for i, r in enumerate(finished, 1))
    elif finished:
        out.append(t["done_line"].format(n=len(finished)))
    if stale:
        out.append(t["stale_line"].format(n=stale, d=f"{stale_days:g}"))
    return "\n".join(out + tail)


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

    @staticmethod
    def _state_db_path() -> Path:
        return _hermes_home() / "state.db"

    def _title_from_state_db(self, session_key) -> str:
        """Hermes' session title for a thread whose platform gave no topic name (Telegram DM topics arrive
        with chat_topic=None). Read-only; any failure just means no hint."""
        p = self._state_db_path()
        if not session_key or not p.exists():
            return ""
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("SELECT title FROM sessions WHERE session_key = ? AND title IS NOT NULL "
                                  "AND title != '' ORDER BY started_at DESC LIMIT 1", (str(session_key),)).fetchone()
            finally:
                con.close()
            return str(row[0]) if row and row[0] else ""
        except Exception:
            logger.debug("ball: state.db title lookup failed", exc_info=True)
            return ""

    def _with_titles(self, rows: list) -> list:
        """Telegram DM topic rows stored before a title was known show only `thread #hash`. Look the
        Hermes session title up by (chat_id, thread_id) — read-only, cached per thread — and use it
        for display. Nothing is written back."""
        cache = self.__dict__.setdefault("_titles", {})
        out = []
        for r in rows:
            lab = r.get("label") or ""
            parts = r.get("key", "").split(":")
            local = len(parts) >= 3 and parts[1] == "local" and not lab  # CLI sessions: only `local · #hash`
            if local or (_is_dm_topic(r.get("key", "")) and (not lab or "thread #" in lab)):
                k = (":".join(parts[2:-1]) if local else parts[-2], None if local else parts[-1])
                if k not in cache:
                    cache[k] = self._title_by_thread(*k)
                if cache[k]:
                    r = dict(r, label=f"local · {_trunc(cache[k], 40)}" if local
                             else f"{lab.split(' · ')[0] if lab else 'telegram'} · {_trunc(cache[k], 40)}")
            out.append(r)
        return out

    def _title_by_thread(self, chat_id: str, thread_id: str) -> str:
        p = self._state_db_path()
        if not p.exists():
            return ""
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("SELECT title FROM sessions WHERE chat_id = ? AND COALESCE(thread_id, '') = ? "
                                  "AND title IS NOT NULL AND title != '' ORDER BY started_at DESC LIMIT 1",
                                  (chat_id, thread_id or "")).fetchone()
            finally:
                con.close()
            return str(row[0]) if row and row[0] else ""
        except Exception:
            logger.debug("ball: state.db title-by-thread lookup failed", exc_info=True)
            return ""

    def _from_state_db(self, session_id) -> Optional[str]:
        """Fallback when no live session_store maps ``session_id``: after a plugin hot-reload the new Ball
        has none until the next pre_gateway_dispatch, and Telegram turns woken by internal notifiers
        (kanban / watch-pattern) never fire it. The persisted session row's ``origin_json`` is the same
        SessionSource the gateway recorded, so it yields the same thread key. Read-only; non-chat
        sessions (subagent/cron: no origin) map to nothing."""
        p = self._state_db_path()
        if not session_id or not p.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("SELECT origin_json FROM sessions WHERE id = ?", (str(session_id),)).fetchone()
            finally:
                con.close()
            o = json.loads(row[0]) if row and row[0] else None
        except Exception as e:  # e.g. a future Hermes schema change: say so on the board, don't fail silently
            if getattr(self, "origin_failure", None) is None:
                logger.warning("ball: state.db origin lookup failed (%s); some turn-ends may be missed", e)
                self.origin_failure = (datetime.now(timezone.utc), type(e).__name__)
            return None
        if not isinstance(o, dict) or not o.get("platform") or not o.get("chat_id"):
            return None
        tkey = thread_key(SimpleNamespace(profile=o.get("profile"), platform=o.get("platform"),
                                          chat_id=o.get("chat_id"), thread_id=o.get("thread_id")))
        self._learn(tkey, session_id=session_id)
        return tkey

    def _resolve(self, session_key=None, session_id=None) -> Optional[str]:
        t = (self.by_session_key.get(str(session_key)) if session_key else None) \
            or self._lookup("lookup_by_session_key", session_key) \
            or (self.by_session_id.get(str(session_id)) if session_id else None) \
            or self._lookup("lookup_by_session_id", session_id) \
            or self._from_state_db(session_id)
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
        text = getattr(event, "text", None)
        m = BALL_CMD.match(text) if isinstance(text, str) else None
        if m and not (text.startswith("/") and text == text.lstrip()):
            # A raw `!ball` (adapter did not rewrite it) or ` /ball` would reach the pending clarify
            # text-intercept as free text and be consumed as the ANSWER (gateway/run_inbound.py:390-394 only
            # skips text starting with "/"). Rewrite to the canonical slash form so the prompt stays pending
            # and the command dispatches. State is untouched.
            return {"action": "rewrite", "text": "/ball" + (m.group(1) or "")}
        # This hook runs BEFORE authorization: anything beyond observing would let unauthorized
        # senders write board rows. Board writes live in ``post_gateway_admission`` instead.
        return None  # never rewrite/skip the message

    @safe
    def on_post_gateway_admission(self, session_key=None, source=None, text=None, **_):
        """Fires once per message that survived authorization — the safe place to mark 'you spoke'.
        ``source`` arrives as a plain dict (``SessionSource.to_dict()``), so wrap it before the
        getattr-based helpers or every field reads empty and all chats collapse to one row."""
        if not isinstance(source, dict):
            return None
        src = SimpleNamespace(**source)
        tkey = thread_key(src)
        self._learn(tkey, session_key=session_key)
        if is_command(text):
            return None  # commands are control traffic — learn the mapping but never move the ball
        lang = ("zh" if has_cjk(text) else "en") if isinstance(text, str) and text.strip() else None
        hint = self._title_from_state_db(session_key) if source.get("thread_id") and not source.get("chat_topic") else ""
        self.store.set(tkey, "ai", note="you spoke", label=thread_label(src, hint), lang=lang,
                       state="working")
        return None

    @safe
    def on_human_input_request(self, kind=None, prompt=None, session_key=None, session_id=None, **_):
        tkey = self._resolve(session_key, session_id)
        if not tkey:
            return None
        q = judge.extract_note(prompt or "")
        note = q if q != judge.REPLIED else judge._cut(f"{kind or 'input'}: {(prompt or '').strip()}".strip(": "))
        self.store.set(tkey, "human", note=note, state="asked")  # a human-input prompt always asks
        return None

    @safe
    def on_human_input_resolved(self, kind=None, outcome=None, session_key=None, session_id=None, **_):
        tkey = self._resolve(session_key, session_id)
        if not tkey:
            return None
        self.store.set(tkey, "ai", note=f"{kind or 'input'} {outcome or 'resolved'}", state="working")
        return None

    @safe
    def on_agent_loop_stopped(self, session_key=None, reason=None, **_):
        tkey = self._resolve(session_key=session_key)
        if not tkey:
            return None
        self.store.set(tkey, "human", note=f"interrupted: {reason or 'unknown'}", state="interrupted")
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
        row = self.store.set(tkey, verdict["ball"], promise=verdict["promise"], note=verdict["note"],
                             state=verdict["state"])
        if self._cfg("llm_judge", False) and getattr(self.ctx, "llm", None) is not None:
            # A plain Thread does not inherit contextvars: the profile-scope override
            # (get_hermes_home()'s first answer under multiplexing) would be lost in the judge
            # thread. Carry it explicitly — the same shape Hermes uses for executor hops.
            cctx = contextvars.copy_context()
            t = threading.Thread(target=lambda: cctx.run(self._run_llm_judge, tkey,
                                 assistant_response or "", row["updated"], verdict),
                                 name="ball-judge", daemon=True)
            t.start()
            return t  # return value of post_llm_call is ignored by Hermes; handy for tests
        return None

    def _stale_days(self) -> float:
        try:
            return max(0.0, float(self._cfg("stale_days", 7)))
        except (TypeError, ValueError):
            return 7.0

    def _working_timeout(self) -> float:
        try:
            return max(0.0, float(self._cfg("working_timeout_min", WORKING_TIMEOUT_MIN)))
        except (TypeError, ValueError):
            return float(WORKING_TIMEOUT_MIN)

    def _promise_grace(self) -> float:
        try:
            return max(0.0, float(self._cfg("promise_grace_min", PROMISE_GRACE_MIN)))
        except (TypeError, ValueError):
            return float(PROMISE_GRACE_MIN)

    def _group_scoped(self, source) -> bool:
        """Board in a group/channel/thread room shows only that room unless board_in_groups is 'all'.
        DMs and sources without a chat_type (CLI, tests) see the full board."""
        chat_type = _s(getattr(source, "chat_type", None)).lower() if source is not None else ""
        if chat_type in ("", "dm"):
            return False
        return str(self._cfg("board_in_groups", "here")).strip().lower() != "all"

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
            self.store.set_if(tkey, stamp, fallback["ball"], promise=fallback["promise"], note="judge failed",
                              state=fallback["state"])
            return
        # newer state (user spoke, interrupt...) wins: set_if is a no-op if the row moved on
        if res["ball"] == "ai":
            state = "working"
        else:  # asked vs replied from the FULL reply text, never from the (short) note
            state = "asked" if fallback["state"] == "asked" or judge.extract_note(text) != judge.REPLIED else "replied"
        self.store.set_if(tkey, stamp, res["ball"], promise=res["promise"] if res["ball"] == "ai" else None,
                          note=(fallback["note"] if state == "asked" and fallback["state"] == "asked"
                                else res["note"] or fallback["note"]), state=state)

    # ── command ──
    @safe
    def command(self, raw_args: str = "", source=None, session_id=None, session_key=None, **_):
        words = (raw_args or "").strip().lower().split()
        sub = words[0] if words else ""
        try:
            out = self._command(words, source, session_id, session_key)
        except Exception as e:  # even reading failed: say so instead of returning nothing
            logger.warning("ball: command failed", exc_info=True)
            lang = str(self._cfg("language", "auto")).lower()
            err = " ".join(str(e).split())[:60] or type(e).__name__
            return L[lang if lang in ("zh", "en") else "en"]["read_fail"].format(err=err)
        fail = getattr(self.store, "write_failure", None)
        if fail and out and sub in ("", "here", "all", "board", "more", "done"):
            lang = "zh" if has_cjk(out) else "en"
            out = L[lang]["write_fail"].format(t=fail[0].strftime("%H:%M"), err=fail[1]) + "\n" + out
        ofail = getattr(self, "origin_failure", None)
        if ofail and out and sub in ("", "here", "all", "board", "more", "done"):
            lang = "zh" if has_cjk(out) else "en"
            out = L[lang]["origin_fail"].format(t=ofail[0].strftime("%H:%M"), err=ofail[1]) + "\n" + out
        plat = source.get("platform") if isinstance(source, dict) else getattr(source, "platform", None)
        plat = getattr(plat, "value", plat)
        if out and plat is not None and str(plat).lower() != "matrix":
            out = out.replace("!ball", "/ball")  # `!ball` is Matrix-only; elsewhere hints must name /ball
        return out

    def _command(self, words, source, session_id, session_key):
        sub = words[0] if words else ""
        hide = "hide" in words[1:]
        here = thread_key(source) if source is not None else self._resolve(session_key, session_id)
        if source is not None and here:
            self._learn(here, session_key=session_key, session_id=session_id)
        setting = self._cfg("language", "auto")
        if sub in ("help", "?"):
            row = self.store.get(here) if here else None
            return L[pick_lang(setting, self.store.all(), row)]["help"]
        if sub == "forget" and "stale" in words[1:]:  # explicit: delete every row hidden by stale_days
            lang = pick_lang(setting, self.store.all(), self.store.get(here) if here else None)
            if self._group_scoped(source):  # never touch other rooms' rows from a group room
                return L[lang]["forget_stale_dm"]
            _, stale = split_stale(self.store.all(), self._stale_days())
            n = sum(1 for r in stale if self.store.delete(r["key"]))
            return L[lang]["forgot_stale"].format(n=n)
        if sub == "forget":  # explicit user command: delete this chat's row
            if not here:
                return L[pick_lang(setting, self.store.all())]["nothread"]
            lang = pick_lang(setting, self.store.all(), self.store.get(here))
            return L[lang]["forgot"] if self.store.delete(here) else L[lang]["forgot_none"]
        if sub in ("", "all", "board", "more", "done"):  # bare /ball = the summary board; /ball here = this chat
            all_rows = self._with_titles(self.store.all())
            plat = source.get("platform") if isinstance(source, dict) else getattr(source, "platform", None)
            plat = str(getattr(plat, "value", plat) or "").lower()
            if plat != "local" and not self._cfg("show_local", False):
                # platform 'local' = cron jobs / API / delegated runs on the host, not a chat you can answer;
                # their asks already reach you through the job's own delivery. Keep them off chat boards.
                all_rows = [r for r in all_rows if r.get("key", "").split(":")[1:2] != ["local"]]
            here_row = next((r for r in all_rows if r["key"] == here), None)
            hidden = 0
            if self._group_scoped(source):  # privacy: other rooms' labels/questions never shown in a group
                hidden = sum(1 for r in all_rows if r["key"] != here)
                all_rows = [here_row] if here_row else []
            stale_days = self._stale_days()
            rows, stale = split_stale(all_rows, stale_days)
            return render_board(rows, lang=pick_lang(setting, all_rows, here_row), full=sub in ("all", "more") or (sub == "done" and "all" in words[1:]), hide=hide,
                                done=sub == "done", show_done=sub == "more" and "all" in words[1:],
                                stale=len(stale), stale_days=stale_days,
                                working_timeout_min=self._working_timeout(),
                                promise_grace_min=self._promise_grace(), hidden=hidden, bold=True)
        if not here:
            return L[pick_lang(setting, self.store.all())]["nothread"]
        row = self.store.get(here)
        return render_one(row, lang=pick_lang(setting, self.store.all() if not row else [], row),
                          working_timeout_min=self._working_timeout(), promise_grace_min=self._promise_grace())
