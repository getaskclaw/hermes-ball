import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from conftest import bp
from ball_plugin import core, judge

# ── fakes ──
class Entry(NS):
    pass


class FakeSessionStore:
    """Mimics the public lookups of gateway.session.SessionStore."""

    def __init__(self):
        self.entries = {}

    def add(self, key, sid, origin):
        self.entries[key] = Entry(session_key=key, session_id=sid, origin=origin)

    def lookup_by_session_id(self, sid):
        return next((e for e in self.entries.values() if e.session_id == sid), None)

    def lookup_by_session_key(self, key):
        return self.entries.get(key)


class FakeLlm:
    def __init__(self, text=None, exc=None, delay=0):
        self.text, self.exc, self.delay, self.calls = text, exc, delay, 0

    def complete(self, messages, **kw):
        self.calls += 1
        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return NS(text=self.text)


class FakeCtx:
    def __init__(self, config=None, llm=None):
        self.config, self.llm, self.hooks, self.commands = config or {}, llm, {}, {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler


SRC = NS(platform="matrix", chat_id="!room", thread_id="t1", profile="vesper")
TKEY = "vesper:matrix:!room:t1"


@pytest.fixture
def env(store):
    ss = FakeSessionStore()
    gw = object()
    skey = "agent:main:matrix:group:!room:t1"
    ss.add(skey, "sid-1", SRC)
    ball = bp.Ball(store, FakeCtx())
    return NS(ball=ball, ss=ss, gw=gw, skey=skey, store=store)


def dispatch(env, text="hi"):
    return env.ball.on_pre_gateway_dispatch(event=NS(text=text, source=SRC), gateway=env.gw, session_store=env.ss)


# ── store ──
def test_store_crud(store):
    assert store.get("k") is None
    r = store.set("k", "ai", note="n")
    assert store.get("k")["ball"] == "ai" and r["note"] == "n"
    since = r["since"]
    store.set("k", "ai", note="again")
    assert store.get("k")["since"] == since  # same holder keeps since
    store.set("k", "human", promise=None, note="q")
    assert store.get("k")["ball"] == "human"
    assert [x["key"] for x in store.all()] == ["k"]
    assert store.delete("k") and store.get("k") is None
    with pytest.raises(ValueError):
        store.set("k", "nobody")


def test_store_columns_only(store):
    store.set("k", "ai")
    cols = [c[1] for c in store._db().execute("PRAGMA table_info(threads)")]
    assert cols == ["key", "ball", "since", "promise", "note", "updated", "label", "lang", "state"]


def test_store_concurrency(dbpath):
    stores = [bp.Store(lambda: sqlite3.connect(dbpath, timeout=10, check_same_thread=False)) for _ in range(2)]
    errs = []

    def work(i):
        try:
            for j in range(50):
                stores[i % 2].set(f"k{j % 10}", "ai" if (i + j) % 2 else "human", note=f"{i}-{j}")
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len(stores[0].all()) == 10


# ── hooks ──
def test_dispatch_sets_ai_and_returns_none(env):
    assert dispatch(env) is None
    assert env.store.get(TKEY)["ball"] == "ai"
    assert env.ball._session_store is env.ss


def test_profile_fallback(env, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/x/.hermes/profiles/coder")
    assert core.thread_key(NS(platform="matrix", chat_id="c", thread_id=None, profile=None)) == "coder:matrix:c:"
    monkeypatch.delenv("HERMES_HOME")
    assert core.thread_key(NS(platform=NS(value="telegram"), chat_id=1, thread_id=2, profile="")) == "default:telegram:1:2"


def test_post_llm_call_question_and_promise(env):
    dispatch(env)
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response="Done. Should I deploy it?")
    r = env.store.get(TKEY)
    assert r["ball"] == "human" and r["promise"] is None
    dispatch(env)
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response="Started the build, I'll report back.")
    r = env.store.get(TKEY)
    assert r["ball"] == "ai" and r["promise"]


def test_post_llm_call_lazy_mapping(env):
    dispatch(env)
    env.ss.add(env.skey, "sid-new", SRC)  # session rotated (e.g. /new, compression) after dispatch
    env.ball.on_post_llm_call(session_id="sid-new", assistant_response="ok")
    assert env.store.get(TKEY)["ball"] == "human"
    assert env.ball.by_session_id["sid-new"] == TKEY and env.ball.by_session_key[env.skey] == TKEY


def test_human_input_before_any_dispatch_cache(env):
    env.ball._session_store = env.ss  # e.g. plugin reloaded mid-session: cache empty, store known
    env.ball.on_human_input_request(kind="clarify", prompt="which db?", session_key=env.skey)
    assert env.store.get(TKEY)["ball"] == "human"


def test_post_llm_call_unmapped_is_noop(env):
    assert env.ball.on_post_llm_call(session_id="ghost", assistant_response="x") is None
    assert env.store.all() == []


def test_human_input_transitions(env):
    dispatch(env)
    env.ball.on_human_input_request(kind="approval", prompt="rm -rf build", session_key=env.skey,
                                    session_id="sid-1", platform="matrix", request_id="r")
    r = env.store.get(TKEY)
    assert r["ball"] == "human" and r["note"].startswith("approval: rm")
    env.ball.on_human_input_resolved(kind="approval", outcome="approved", session_key=env.skey, session_id="sid-1")
    r = env.store.get(TKEY)
    assert r["ball"] == "ai" and "approved" in r["note"]


def test_agent_loop_stopped(env):
    dispatch(env)
    env.ball.on_agent_loop_stopped(session_key=env.skey, platform="matrix", reason="/stop", invalidation_reason=None)
    r = env.store.get(TKEY)
    assert r["ball"] == "human" and r["note"] == "interrupted: /stop"


def test_hook_exceptions_swallowed(env):
    class Boom:
        def __getattr__(self, n):
            raise RuntimeError("boom")

    env.ball.store = Boom()
    assert dispatch(env) is None
    assert env.ball.on_post_llm_call(session_id="sid-1", assistant_response="x") is None
    assert env.ball.on_human_input_request(session_key=env.skey) is None
    assert env.ball.on_human_input_resolved(session_key=env.skey) is None
    assert env.ball.on_agent_loop_stopped(session_key=env.skey) is None
    assert env.ball.command("all") == "⚠️ Ball couldn't read its database (boom)"  # short error, not nothing
    # malformed event
    assert env.ball.on_pre_gateway_dispatch(event=object()) is None
    assert env.ball.on_pre_gateway_dispatch(event=NS(source=NS(platform=None)), gateway=Boom(), session_store=Boom()) is None


# ── LLM judge ──
def _llm_env(env, llm, **cfg):
    env.ball.ctx = FakeCtx(config={"llm_judge": True, **cfg}, llm=llm)
    dispatch(env)
    return env.ball.on_post_llm_call(session_id="sid-1", assistant_response="All done.")


def test_llm_judge_off_by_default(env):
    env.ball.ctx = FakeCtx(llm=FakeLlm('{"ball":"ai"}'))
    dispatch(env)
    assert env.ball.on_post_llm_call(session_id="sid-1", assistant_response="ok") is None
    assert env.ball.ctx.llm.calls == 0


def test_llm_judge_success(env):
    t = _llm_env(env, FakeLlm('{"ball":"ai","since":"2026-01-01T00:00:00+00:00","promise":"will rerun tests","note":"pending rerun"}'))
    t.join(5)
    r = env.store.get(TKEY)
    assert r["ball"] == "ai" and r["promise"] == "will rerun tests" and r["note"] == "pending rerun"


@pytest.mark.parametrize("llm", [FakeLlm("not json"), FakeLlm(exc=TimeoutError("t")), FakeLlm('{"ball":"maybe"}')])
def test_llm_judge_failure_falls_back(env, llm):
    t = _llm_env(env, llm)
    t.join(5)
    r = env.store.get(TKEY)
    assert r["ball"] == "human" and r["note"] == "judge failed"


def test_llm_judge_non_blocking_and_no_clobber(env):
    llm = FakeLlm('{"ball":"ai","promise":"x","note":"late"}', delay=0.5)
    t0 = time.monotonic()
    t = _llm_env(env, llm)
    assert time.monotonic() - t0 < 0.3  # turn not blocked
    dispatch(env, "next")  # user spoke meanwhile
    t.join(5)
    assert env.store.get(TKEY)["note"] == "you spoke"


# ── heuristic fixtures (en + zh) ──
FIXTURES = [
    ("Deployed to staging. Want me to promote it to prod?", "human", False),
    ("Here are the results.\n\nWhich option do you prefer: A or B?", "human", False),
    ("Please confirm the migration window before I proceed.", "human", False),
    ("The fix is merged and CI is green.", "human", False),
    ("I kicked off the backfill; I'll report back when it finishes.", "ai", True),
    ("Will follow up once the vendor replies.", "ai", True),
    ("Let me check the logs and get back to you.", "ai", True),
    ("", "human", False),
    ("已经修好了，测试全部通过。", "human", False),
    ("要不要我顺便把文档也更新一下？", "human", False),
    ("请确认是否可以重启网关", "human", False),
    ("我稍后把结果发给你。", "ai", True),
    ("构建已开始，马上给你结果。", "ai", True),
    ("接下来我会跑完整的回归测试。", "ai", True),
    ("Done (see `report.md`)?", "human", False),
]


@pytest.mark.parametrize("text,ball,promise", FIXTURES)
def test_heuristic(text, ball, promise):
    v = judge.heuristic(text)
    assert v["ball"] == ball, v
    assert bool(v["promise"]) == promise, v


def test_parse_contract():
    assert judge.parse_contract('```json\n{"ball":"human","since":"x","promise":null,"note":"n"}\n```')["ball"] == "human"
    assert judge.parse_contract("") is None
    assert judge.parse_contract('{"ball":"ai","promise":5}') is None


# ── commands ──
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _iso(mins):
    return (NOW - timedelta(minutes=mins)).isoformat()


def test_render_one():
    zh = {"ball": "human", "since": _iso(8), "promise": None, "note": "要我写进哪个文件？", "lang": "zh"}
    assert core.render_one(zh, NOW) == "🏀 轮到你 · 8m\n它问你：要我写进哪个文件？"
    en = {"ball": "human", "since": _iso(3), "promise": None, "note": "asked: ok?"}
    assert core.render_one(en, NOW) == "🏀 Your move · 3m\nIt asks: ok?"
    assert core.render_one({"ball": "ai", "since": _iso(2), "promise": None, "note": "", "lang": "zh"}, NOW) == \
        "🤖 AI 在干活 · 2m — 不用管"
    assert core.render_one({"ball": "ai", "since": _iso(120), "promise": "马上", "note": "马上"}, NOW) == \
        "⚠️ AI 卡住了 · 2h\n说了「马上」但没动静 · 回一句 go 催它"
    assert core.render_one({"ball": "ai", "since": _iso(5), "promise": "I'll check", "note": ""}, NOW) == \
        "⚠️ AI stalled · 5m\nsaid \u201cI'll check\u201d but went quiet · reply go to nudge it"
    assert core.render_one({"ball": "human", "since": _iso(1), "promise": None, "note": "replied", "lang": "zh"},
                           NOW) == "✅ 已答完 · 1m — 无需你处理"
    assert "no turns" in core.render_one(None).lower()


def _rows():
    rows = [{"key": f"p:matrix:!r{i}:", "ball": "human", "since": _iso(10 * i + 5), "promise": None,
             "note": f"问题{i}？", "label": f"Ball Demo {i} (matrix)", "lang": "zh"} for i in range(1, 11)]
    rows += [{"key": "p:tg:x:", "ball": "ai", "since": _iso(30), "promise": "马上", "note": "马上",
              "label": "Ops (telegram)", "lang": "zh"},
             {"key": "p:tg:y:", "ball": "ai", "since": _iso(2), "promise": None, "note": "you spoke",
              "label": "DM (telegram)", "lang": "zh"}]
    return rows


def test_board_top3_oldest_first():
    out = core.render_board(_rows(), NOW)
    print("\n--- !ball all ---\n" + out)
    assert out.splitlines() == [
        "🏀 10 个对话在等你",
        "1. Ball Demo 10 · 1h — 问题10？",
        "2. Ball Demo 9 · 1h — 问题9？",
        "3. Ball Demo 8 · 1h — 问题8？",
        "…还有 7 个：!ball more",
        "⚠️ 1 个 AI 卡住了：Ops — 去那里回 go",
        "🤖 1 个 AI 在干活",
    ]
    assert "(matrix)" not in out and "← here" not in out


def test_board_more_lists_all():
    out = core.render_board(_rows(), NOW, full=True)
    print("\n--- !ball more ---\n" + out)
    lines = out.splitlines()
    assert sum(1 for l in lines if re.match(r"\d+\. ", l)) == 10 and "!ball more" not in out
    assert lines[-3].startswith("10. Ball Demo 1 ·")


def test_board_hide_names():
    out = core.render_board(_rows(), NOW, hide=True)
    print("\n--- !ball all hide ---\n" + out)
    assert "Ball Demo" not in out and "Ops" not in out
    assert "1. 对话 A · 1h — 问题10？" in out and "卡住了：对话 D" in out
    en = core.render_board([dict(r, lang="en", note="Which file?") for r in _rows()], NOW, hide=True, full=True)
    assert "1. Chat A · 1h — Which file?" in en


def test_board_empty_and_english():
    assert core.render_board([], NOW, lang="zh") == "✅ 没有对话在等你"
    assert core.render_board([], NOW) == "✅ No chats waiting on you"
    rows = [{"key": "k", "ball": "human", "since": _iso(60), "promise": None,
             "note": "Done. Should I deploy `main` now?", "label": "Ball (matrix)"},
            {"key": "j", "ball": "ai", "since": _iso(9), "promise": "I'll check", "note": "", "label": "Ops"}]
    out = core.render_board(rows, NOW)
    print("\n--- !ball all (en) ---\n" + out)
    assert out.splitlines() == ["🏀 1 chat waiting on you", "1. Ball · 1h — Should I deploy main now?",
                                "⚠️ 1 AI stalled: Ops — reply go there"]


def test_board_legacy_long_notes_render_short():
    long = "asked: " + "我已经把所有的东西都整理好了。" * 5 + "哪个插件？给我名字或一句话说它干啥，我马上出两条。"
    rows = [{"key": "k", "ball": "human", "since": _iso(60), "promise": None, "note": long, "label": None}]
    out = core.render_board(rows, NOW)
    assert out.splitlines()[1].endswith("— 哪个插件？"), out


def test_command_handler(env):
    assert "no turns" in env.ball.command("", source=SRC).lower()
    dispatch(env)
    assert env.ball.command("", source=SRC).startswith("🤖 AI is working · ")
    assert env.ball.command("all") == "✅ No chats waiting on you\n🤖 1 AI working"
    assert "Not a chat" in env.ball.command("")
    dispatch(env, "你好")
    assert env.ball.command("", source=SRC).startswith("🤖 AI 在干活 · ")
    assert env.ball.command("help").splitlines()[0] == "球在谁手里？— 告诉你哪些 AI 对话在等你。"
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response="改好了。要我写进哪个文件？")
    out = env.ball.command("", source=SRC)
    print("\n--- !ball ---\n" + out)
    assert out.endswith("\n它问你：要我写进哪个文件？")
    assert "对话 A" in env.ball.command("all hide") and "对话 A" in env.ball.command("more hide")


def test_language_config_forces(env):
    dispatch(env, "你好")
    env.ball.ctx.config["language"] = "en"
    assert env.ball.command("", source=SRC).startswith("🤖 AI is working")


def test_note_stored_short(env):
    dispatch(env)
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response="x" * 3000 + ". Which file?")
    assert env.store.get(TKEY)["note"] == "Which file?"
    env.store.set(TKEY, "human", note="y" * 500)
    assert len(env.store.get(TKEY)["note"]) == 80


def test_register_matches_manifest(tmp_path):
    import yaml
    ctx = FakeCtx()
    bp.register(ctx)
    manifest = yaml.safe_load((bp.__file__ and __import__("pathlib").Path(bp.__file__).parent / "plugin.yaml").read_text())
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert list(ctx.commands) == ["ball"]
    assert manifest["version"] == "0.1.0" and isinstance(manifest["version"], str)


# ── regressions: live bugs ──
@pytest.mark.parametrize("text", ["/ball", "/ball all", "!ball", "  /stop", "/new", "!help x"])
def test_commands_do_not_move_ball(env, text):
    """BUG1: `!ball` (-> `/ball`) was counted as 'you spoke' and answered 'AI · 0s'."""
    env.store.set(TKEY, "human", note="replied", since="2026-11-11T00:19:12+00:00")
    before = env.store.get(TKEY)
    res = dispatch(env, text)
    # raw `!ball` is normalized to `/ball` (bug: answered a pending clarify); never skip, never move the ball
    assert res is None or res == {"action": "rewrite", "text": "/ball"}
    assert env.store.get(TKEY) == before
    assert env.ball._session_store is env.ss  # commands still hand Ball the session_store


def test_bang_ball_reports_state_before_query(env):
    env.store.set(TKEY, "human", note="replied", since="2026-11-11T00:19:12+00:00")
    dispatch(env, "/ball")  # what the Matrix adapter turns `!ball` into
    out = env.ball.command("", source=SRC, session_key=env.skey, session_id="sid-1")
    assert out.startswith("✅ Done · ") and "nothing needed" in out and "you spoke" not in out


def test_command_detection_is_strict():
    for t in ("/ball", "!ball", "/x_y"):
        assert core.is_command(t)
    for t in ("", None, "/", "!", "! wow", "/ tmp", "hello /ball", "!!", "?", 42, "/123"):
        assert not core.is_command(t)


def test_plain_messages_still_move_ball(env):
    env.store.set(TKEY, "human", note="replied")
    dispatch(env, "so?")
    assert env.store.get(TKEY)["ball"] == "ai"


def test_same_holder_keeps_since_on_new_user_message(env):
    """Documented: since = when the ball last changed hands, not the latest message."""
    dispatch(env, "first")
    since = env.store.get(TKEY)["since"]
    dispatch(env, "second")
    r = env.store.get(TKEY)
    assert r["since"] == since and r["note"] == "you spoke"


def test_turn_end_after_hot_load_is_logged_not_silent(env, caplog):
    """BUG2: plugin hot-loaded mid-turn -> no session_store yet -> turn end unmapped, previously DEBUG-only."""
    import logging
    with caplog.at_level(logging.INFO, logger="hermes_plugins.ball"):
        env.ball.on_post_llm_call(session_id="sid-1", assistant_response="done")
    assert env.store.all() == []
    assert any("not yet seen" in r.getMessage() for r in caplog.records)
    dispatch(env, "/ball")  # first inbound (even a command) teaches the store; next turn end maps
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response="done")
    assert env.store.get(TKEY)["ball"] == "human"


# ── labels ──
def test_thread_label_sources():
    assert core.thread_label(NS(platform="matrix", chat_id="!YJ9Hide:srv", chat_name="Ball", chat_type="group",
                                thread_id=None)) == "Ball (matrix)"
    assert core.thread_label(NS(platform=NS(value="telegram"), chat_id="123", chat_name=None, chat_type="dm",
                                user_name=None, thread_id=None)) == "DM (telegram)"
    lab = core.thread_label(NS(platform="matrix", chat_id="!YJ9Hide:srv", chat_name=None, chat_type="group",
                               thread_id="$evt"))
    assert lab.startswith("matrix · #") and "YJ9" not in lab and "$evt" not in lab


def test_board_shows_label_not_raw_id(env):
    src = NS(platform="matrix", chat_id="!YJ9Hide:srv", thread_id=None, profile=None, chat_name="Ball",
             chat_type="group")
    env.ball.on_pre_gateway_dispatch(event=NS(text="hi", source=src), session_store=env.ss)
    env.store.set(core.thread_key(src), "human", note="Should I merge?")
    board = env.ball.command("all", source=src)
    assert "1. Ball · " in board and "Should I merge?" in board
    assert "!YJ9Hide" not in board and "!room" not in board and "vesper:" not in board and "(matrix)" not in board


def test_legacy_db_migrates_label_column(dbpath):
    conn = sqlite3.connect(dbpath)
    conn.execute("CREATE TABLE threads (key TEXT PRIMARY KEY, ball TEXT NOT NULL, since TEXT NOT NULL, "
                 "promise TEXT, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL)")
    conn.execute("INSERT INTO threads VALUES('default:matrix:!old:srv:', 'human', '2026-01-01T00:00:00+00:00', "
                 "NULL, 'asked: q', 'u')")
    conn.commit(); conn.close()
    s = bp.Store(lambda: sqlite3.connect(dbpath, check_same_thread=False))
    rows = s.all()
    assert rows[0]["note"] == "asked: q" and rows[0]["label"] is None
    board = core.render_board(rows)
    assert "matrix · #" in board and "!old" not in board and board.endswith("— q")


def test_legacy_db_migrates_lang_column(dbpath):
    conn = sqlite3.connect(dbpath)
    conn.execute("CREATE TABLE threads (key TEXT PRIMARY KEY, ball TEXT NOT NULL, since TEXT NOT NULL, "
                 "promise TEXT, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, label TEXT)")
    conn.execute("INSERT INTO threads VALUES('k', 'human', '2026-01-01T00:00:00+00:00', NULL, 'asked: 行吗？', 'u', 'L')")
    conn.commit(); conn.close()
    s = bp.Store(lambda: sqlite3.connect(dbpath, check_same_thread=False))
    r = s.all()[0]
    assert (r["note"], r["label"], r["lang"]) == ("asked: 行吗？", "L", None)


def _topic_src(topic):
    return NS(platform="telegram", chat_id="-100", chat_name="Grp", chat_type="group", thread_id="42",
              chat_topic=topic)


def test_thread_label_topic_name():
    assert core.thread_label(_topic_src("Release planning")) == "Grp (telegram) · Release planning"
    long = core.thread_label(_topic_src("x" * 80))
    assert long.endswith("…") and len(long.split(" · ")[-1]) == 40


@pytest.mark.parametrize("topic", ["", "   ", "\n\t", None])
def test_thread_label_blank_topic_falls_back_to_hash(topic):
    assert core.thread_label(_topic_src(topic)) == f"Grp (telegram) · thread {core._short_hash('42')}"


def test_thread_label_topic_newline_collapsed():
    lab = core.thread_label(_topic_src("  evil\n\nfake row\r\n  x  "))
    assert lab == "Grp (telegram) · evil fake row x"
    assert "\n" not in lab and "\r" not in lab


# --- regression: question/ask takes precedence over promise ---
import pytest as _pt
_h = judge.heuristic

@_pt.mark.parametrize("text", [
    "哪个插件？给我名字或一句话说它干啥，我马上出两条。",
    "Which repo? I'll check it right after you tell me.",
    "要不要我稍后重跑一遍测试",
    "你选 A 还是 B，我马上开工。",
    "Done with step 1. Should I continue? I will follow up either way.",
    "告诉我目标分支，我会继续。",
])
def test_question_beats_promise(text):
    r = _h(text)
    assert r["ball"] == "human" and r["promise"] is None and r["note"] != "replied" and len(r["note"]) <= 40

@_pt.mark.parametrize("text", ["Tests are running, I'll report back.", "我马上去跑一遍。"])
def test_pure_promise_is_ai(text):
    r = _h(text)
    assert r["ball"] == "ai" and r["promise"]

@_pt.mark.parametrize("text", ["The fix is in judge.py line 34.", "已完成，测试全部通过。"])
def test_plain_answer_replied(text):
    assert _h(text) == {"ball": "human", "promise": None, "note": "replied", "state": "replied"}


EXTRACT = [
    ("哪个插件？给我名字或一句话说它干啥，我马上出两条。", "哪个插件？"),
    ("改好了。要我写进哪个文件？", "要我写进哪个文件？"),
    ("**要不要我顺便更新文档？**", "要不要我顺便更新文档？"),
    ("告诉我目标分支，我会继续。", "告诉我目标分支，我会继续"),
    ("已完成，测试全部通过。", "replied"),
    ("Done. Should I edit `judge.py`? I will follow up.", "Should I edit judge.py?"),
    ("The fix is in judge.py line 34.", "replied"),
    ("Tests pass.\n\n- Want me to open a PR for [the branch](http://x)?", "Want me to open a PR for the branch?"),
    ("Let me know which option you prefer.", "Let me know which option you prefer"),
    ("asked: Should I deploy?", "Should I deploy?"),
    ("", "replied"),
    ("要不要我顺便把文档也更新一下，以及把 README 里的所有示例全部重新跑一遍再贴上来？",
     "以及把 README 里的所有示例全部重新跑一遍再贴上来？"),
    ("Q" * 60 + "?", "Q" * 38 + "…?"),  # truncation keeps the '?' visible
]


@pytest.mark.parametrize("text,note", EXTRACT)
def test_extract_note(text, note):
    out = judge.extract_note(text)
    assert out == note and len(out) <= 40


# ── asked vs replied ──
def _done_rows(n=7):
    return [{"key": f"p:matrix:!d{i}:", "ball": "human", "since": _iso(100 + i), "promise": None,
             "note": "replied", "label": f"Done {i} (matrix)", "lang": "zh"} for i in range(1, n + 1)]


def test_board_waiting_vs_done_counts():
    rows = _rows()[:2] + _done_rows()
    out = core.render_board(rows, NOW)
    print("\n--- !ball all (asked vs replied) ---\n" + out)
    assert out.splitlines() == [
        "🏀 2 个对话在等你",
        "1. Ball Demo 2 · 25m — 问题2？",
        "2. Ball Demo 1 · 15m — 问题1？",
        "💤 7 个已答完，无需你处理",
    ]
    assert "Done 1" not in out and "已回复" not in out


def test_board_interrupted_still_waiting():
    rows = [{"key": "k", "ball": "human", "since": _iso(5), "promise": None, "note": "interrupted: x",
             "label": "A", "lang": "zh"}]
    assert core.render_board(rows, NOW).splitlines()[:2] == ["🏀 1 个对话在等你", "1. A · 5m — 已中断"]


def test_board_all_done_empty_waiting():
    out = core.render_board(_done_rows(7), NOW)
    assert out.splitlines() == ["✅ 没有对话在等你", "💤 7 个已答完，无需你处理"]
    en = core.render_board([dict(r, lang="en") for r in _done_rows(7)], NOW)
    assert en.splitlines() == ["✅ No chats waiting on you", "💤 7 done, nothing needed"]


def test_board_done_view_and_more_all():
    rows = _rows()[:1] + _done_rows(3)
    d = core.render_board(rows, NOW, done=True)
    print("\n--- !ball done ---\n" + d)
    assert d.splitlines() == ["💤 3 个已答完，无需你处理", "1. Done 3 · 1h", "2. Done 2 · 1h", "3. Done 1 · 1h"]
    assert core.render_board(_rows()[:1], NOW, done=True) == "💤 没有已答完的对话"
    m = core.render_board(rows, NOW, full=True, show_done=True).splitlines()
    assert m[0] == "🏀 1 个对话在等你" and m[-4:] == d.splitlines() and "无需你处理" in m[-4]
    hid = core.render_board(rows, NOW, done=True, hide=True)
    assert "Done" not in hid and "1. 对话 A · 1h" in hid
    hid2 = core.render_board(rows, NOW, hide=True)
    assert "Done" not in hid2 and "Ball Demo" not in hid2


def test_single_replied_done_line():
    r = {"ball": "human", "since": _iso(8), "promise": None, "note": "replied"}
    assert core.render_one(dict(r, lang="zh"), NOW) == "✅ 已答完 · 8m — 无需你处理"
    assert core.render_one(dict(r, lang="en"), NOW) == "✅ Done · 8m — nothing needed"


def test_command_done_and_more_all(env):
    env.store.set(TKEY, "human", note="replied", label="Here (matrix)", lang="en")
    env.store.set("p:x:y:", "human", note="Which file?", label="Other (matrix)", lang="en")
    assert env.ball.command("all", source=SRC, session_key=env.skey, session_id="sid-1").splitlines() == [
        "🏀 1 chat waiting on you", "1. Other · " + env.ball.command("all").splitlines()[1].split(" · ")[1],
        "💤 1 done, nothing needed"]
    assert env.ball.command("done", source=SRC, session_key=env.skey, session_id="sid-1").splitlines()[0] == \
        "💤 1 done, nothing needed"
    assert "Here" in env.ball.command("more all", source=SRC, session_key=env.skey, session_id="sid-1")
    assert "Here" not in env.ball.command("more", source=SRC, session_key=env.skey, session_id="sid-1")


# ── regression: live-demo bugs ──
@pytest.mark.parametrize("text,want", [
    ("!ball", "/ball"), ("!ball all hide", "/ball all hide"), ("  /ball more", "/ball more"), ("!BALL", "/ball")])
def test_raw_bang_ball_rewritten_so_clarify_never_consumes_it(env, text, want):
    """BUG(clarify): raw `!ball` reached the clarify text-intercept and was taken as the answer.
    The intercept skips text starting with '/', so Ball rewrites to the canonical slash form."""
    env.store.set(TKEY, "human", note="Which port?", state="asked")
    before = env.store.get(TKEY)
    assert dispatch(env, text) == {"action": "rewrite", "text": want}
    assert env.store.get(TKEY) == before


@pytest.mark.parametrize("text", ["/ball", "/ball all", "!ballpark is fine", "ball?"])
def test_canonical_or_non_ball_text_not_rewritten(env, text):
    r = dispatch(env, text)
    assert r is None


def _age(store, key, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store._db().execute("UPDATE threads SET updated=?, since=? WHERE key=?", (old, old, key))
    store._db().commit()


def test_stale_rows_hidden_not_deleted(env):
    env.store.set("p:matrix:!gone:", "human", note="Deploy now?", state="asked", label="Left room (matrix)")
    env.store.set("p:matrix:!live:", "human", note="Merge it?", state="asked", label="Live room (matrix)")
    _age(env.store, "p:matrix:!gone:", 8)
    out = env.ball.command("all")
    assert "Live room" in out and "Left room" not in out and "1 idle >7d hidden" in out
    assert env.store.get("p:matrix:!gone:") is not None  # expiry only hides


def test_stale_days_config(env):
    env.store.set("p:matrix:!a:", "human", note="X?", state="asked", label="Room A (matrix)")
    _age(env.store, "p:matrix:!a:", 3)
    env.ball.ctx = NS(get_config=lambda k, d=None: {"stale_days": 2}.get(k, d), llm=None)
    assert "Room A" not in env.ball.command("all")
    env.ball.ctx = NS(get_config=lambda k, d=None: {"stale_days": 0}.get(k, d), llm=None)  # 0 = never hide
    assert "Room A" in env.ball.command("all")


def test_forget_deletes_only_this_thread(env):
    env.store.set(TKEY, "human", note="X?", state="asked")
    env.store.set("other", "human", note="Y?", state="asked")
    assert "Removed" in env.ball.command("forget", source=SRC)
    assert env.store.get(TKEY) is None and env.store.get("other") is not None
    assert "Nothing tracked" in env.ball.command("forget", source=SRC)


def test_long_question_stays_asked_after_truncation(env):
    """BUG(truncation): a >40-char question lost its '?' in the note and then rendered as Done."""
    reply = "I finished the analysis. Should I go ahead and also rewrite the migration script for the new schema"
    reply += " and redeploy everything to staging tonight?"
    v = judge.heuristic(reply)
    assert v["state"] == "asked" and len(v["note"]) <= 40
    dispatch(env, "hi")
    env.ball.on_post_llm_call(session_id="sid-1", assistant_response=reply)
    row = env.store.get(TKEY)
    assert row["state"] == "asked" and not core.is_done(row)
    assert "Done" not in core.render_one(row, lang="en")
    assert "1 chat waiting" in core.render_board([row], lang="en")


def test_legacy_rows_migrated_conservatively(dbpath):
    conn = sqlite3.connect(dbpath)
    conn.execute("CREATE TABLE threads (key TEXT PRIMARY KEY, ball TEXT NOT NULL, since TEXT NOT NULL, "
                 "promise TEXT, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, label TEXT, lang TEXT)")
    ts = "2026-11-11T00:00:00+00:00"
    rows = [("trunc", "human", "Should I also rewrite the migration scr…"), ("rep", "human", "replied"),
            ("int", "human", "interrupted: stop"), ("ai", "ai", "you spoke")]
    for k, b, n in rows:
        conn.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)", (k, b, ts, None, n, ts, None, None))
    conn.commit(); conn.close()
    st = bp.Store(lambda: sqlite3.connect(dbpath, check_same_thread=False))
    got = {k: st.get(k)["state"] for k, _, _ in rows}
    assert got == {"trunc": "asked", "rep": "replied", "int": "interrupted", "ai": "working"}
    assert st.get("trunc")["note"] == "Should I also rewrite the migration scr…"  # data untouched
    assert not core.is_done(st.get("trunc"))


@pytest.mark.parametrize("text", [
    "I'll report the findings here.",
    "Starting the scan now. I'll update you when it finishes.",
    "Kicked off the build. I will let you know once it passes.",
    "I'll get back to you with numbers.",
    "Will follow up after lunch.",
    "I'll check and tell you.",
    "Running it now; will post here when done.",
    "我回头把结果发给你。",
    "我去查一下日志。",
    "接下来我会跑完整测试。",
])
def test_promise_detected(text):
    v = judge.heuristic(text)
    assert v["ball"] == "ai" and v["promise"] and v["state"] == "working", text


@pytest.mark.parametrize("text", [
    "I'll report the findings here. Which format do you want?",
    "我稍后去查，要不要先重启？",
])
def test_question_beats_broadened_promise(text):
    assert judge.heuristic(text)["state"] == "asked"


def test_old_promise_not_in_last_two_sentences_is_replied():
    text = ("I'll check the logs first. Found the bug in parser.py. Fixed it and added a test. "
            "All 94 tests pass now.")
    v = judge.heuristic(text)
    assert v["ball"] == "human" and v["state"] == "replied"


# ── restart mid-turn: quiet working rows ──
def _age_row(store, key, minutes):
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    db = store._db()
    db.execute("UPDATE threads SET since=?, updated=? WHERE key=?", (old, old, key))
    db.commit()


def test_old_working_row_renders_quiet(store):
    ball = bp.Ball(store, FakeCtx())
    ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="do the thing"))
    k = core.thread_key(SRC)
    assert "AI is working" in ball.command("", source=SRC)
    _age_row(store, k, 45)
    before = store.get(k)
    out = ball.command("", source=SRC)
    assert out == "⚠️ AI quiet 45m — may have stopped (restart?) · reply go to nudge it"
    board = ball.command("all", source=SRC)
    assert "⚠️ 1 AI stalled:" in board and "AI working" not in board
    assert store.get(k) == before  # display only, no DB writes


def test_quiet_timeout_zero_off_and_zh(store):
    ball = bp.Ball(store, FakeCtx({"working_timeout_min": 0}))
    ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="开始吧"))
    _age_row(store, core.thread_key(SRC), 600)
    assert "AI 在干活" in ball.command("", source=SRC)
    ball.ctx.config["working_timeout_min"] = 30
    assert ball.command("", source=SRC).startswith("⚠️ AI 10h 没动静")
    assert "1 个 AI 卡住了" in ball.command("all", source=SRC)


def test_quiet_does_not_touch_interrupted_or_asked(store):
    k = core.thread_key(SRC)
    store.set(k, "human", note="interrupted: x", state="interrupted")
    _age_row(store, k, 120)
    assert "Interrupted" in core.render_one(store.get(k), lang="en")
    store.set(k, "human", note="pick A or B?", state="asked")
    _age_row(store, k, 120)
    assert "Your move" in core.render_one(store.get(k), lang="en")


# ── write failures (disk full / read-only) ──
def test_write_failure_banner_logged_once_and_recovers(dbpath, caplog):
    flag = {"ro": False}

    def connect():
        if flag["ro"]:
            return sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True, check_same_thread=False)
        return sqlite3.connect(dbpath, check_same_thread=False)

    store = bp.Store(connect)
    ball = bp.Ball(store, FakeCtx())
    ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="hi"))
    store._conn.close(); store._conn = None
    flag["ro"] = True
    caplog.set_level("DEBUG", logger="hermes_plugins.ball")
    for _ in range(3):  # hooks never raise
        assert ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="again")) is None
        assert ball.on_post_llm_call(session_id="x", assistant_response="done.") is None
    warns = [r for r in caplog.records if r.levelname == "WARNING" and "could not save" in r.getMessage()]
    assert len(warns) == 1
    assert store.write_failure is not None
    for sub in ("", "all", "more", "done"):
        out = ball.command(sub, source=SRC)
        assert re.match(r"⚠️ Ball couldn't save since \d\d:\d\d UTC \(.+, disk full\?\) — board may be out of date\n",
                        out), out
    assert "couldn't save" not in ball.command("help", source=SRC)
    store._conn.close(); store._conn = None
    flag["ro"] = False
    ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="back"))
    assert store.write_failure is None
    assert not ball.command("", source=SRC).startswith("⚠️ Ball")


def test_write_failure_connect_raises_then_read_fails(store, dbpath):
    ball = bp.Ball(bp.Store(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database or disk is full"))),
                   FakeCtx())
    assert ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="hi")) is None
    assert ball.store.write_failure[1] == "database or disk is full"
    out = ball.command("", source=SRC)
    assert out == "⚠️ Ball couldn't read its database (database or disk is full)"


def test_write_failure_banner_zh(store):
    ball = bp.Ball(store, FakeCtx({"language": "zh"}))
    ball.on_pre_gateway_dispatch(event=NS(source=SRC, text="你好"))
    store.write_failure = (datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc), "disk I/O error")
    assert ball.command("", source=SRC).startswith("⚠️ Ball 自 09:05 UTC 起无法保存（disk I/O error，磁盘满了？）— 看板可能过时\n")


# ── courtesy closers are not asks ──
@pytest.mark.parametrize("text,state", [
    ("Fixed the CSS path and redeployed. Let me know if you have any questions.", "replied"),
    ("已改好并部署。如有问题告诉我。", "replied"),
    ("Feel free to reach out if anything breaks.", "replied"),
    ("Any questions?", "replied"),
    ("有问题随时告诉我。", "replied"),
    ("Let me know which option you prefer", "asked"),
    ("Should I delete the old drafts?", "asked"),
    ("Found two. Should I use the cheaper one? Let me know if you have any questions.", "asked"),
    ("Do you want me to keep the logs, or let me know if you need anything else?", "asked"),
    ("要不要也更新文档？有需要再找我。", "asked"),
    ("I'll report back soon. Let me know if anything breaks.", "working"),
    ("Want me to deploy? I'll report back after.", "asked"),  # question beats promise
])
def test_courtesy_closers(text, state):
    assert judge.heuristic(text)["state"] == state


def test_courtesy_note_picks_real_ask():
    v = judge.heuristic("Should I merge it? Let me know if you have any questions.")
    assert v["note"] == "Should I merge it?"


def test_endings_fixture_precision_recall():
    import json
    import pathlib
    rows = [json.loads(l) for l in (pathlib.Path(__file__).parent / "fixtures" / "endings.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) >= 80
    assert 0.35 <= sum(r["lang"] == "zh" for r in rows) / len(rows) <= 0.65
    m = {"asked": "asked", "replied": "replied", "working": "promised"}
    got = [m[judge.heuristic(r["text"])["state"]] for r in rows]
    tp = sum(g == "asked" and r["label"] == "asked" for g, r in zip(got, rows))
    fp = sum(g == "asked" and r["label"] != "asked" for g, r in zip(got, rows))
    fn = sum(g != "asked" and r["label"] == "asked" for g, r in zip(got, rows))
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    acc = sum(g == r["label"] for g, r in zip(got, rows)) / len(rows)
    print(f"endings n={len(rows)} asked precision={precision:.3f} recall={recall:.3f} 3-way accuracy={acc:.3f}")
    assert precision >= 0.95 and recall >= 0.90


# ── promise grace ──
def test_promise_grace_shows_working(env):
    now = datetime.now(timezone.utc)
    fresh = {"key": "k", "ball": "ai", "promise": "I'll report back", "since": now.isoformat(),
             "updated": now.isoformat(), "note": "", "state": "working"}
    old_ts = (now - timedelta(minutes=5)).isoformat()
    old = dict(fresh, since=old_ts, updated=old_ts)
    assert core.render_one(fresh, now=now, lang="en").startswith("🤖 AI is working")
    assert core.render_one(old, now=now, lang="en").startswith("⚠️ AI stalled")
    assert core.render_one(fresh, now=now, lang="en", promise_grace_min=0).startswith("⚠️ AI stalled")
    assert "stalled" not in core.render_board([fresh], now=now, lang="en")
    assert "1 AI stalled" in core.render_board([old], now=now, lang="en")


# ── privacy: group rooms only see their own row ──
def _grp(chat_id, chat_type="group"):
    return NS(platform="matrix", chat_id=chat_id, thread_id=None, profile=None, chat_name="R" + chat_id,
              chat_type=chat_type)


def _two_rooms(env):
    a, b = _grp("!a"), _grp("!b")
    for src, q in ((a, "Should I merge A?"), (b, "Secret plan for B?")):
        env.ball.on_pre_gateway_dispatch(event=NS(text="hi", source=src), session_store=env.ss)
        env.store.set(core.thread_key(src), "human", note=q, state="asked")
    return a, b


@pytest.mark.parametrize("sub", ["all", "more", "done", "more all"])
def test_group_board_hides_other_rooms(env, sub):
    a, _ = _two_rooms(env)
    out = env.ball.command(sub, source=a)
    assert "Secret plan" not in out and "R!b" not in out
    assert "1 other chats hidden here — use !ball all in your DM" in out
    if sub in ("all", "more"):
        assert "Should I merge A?" in out


def test_dm_and_cli_board_show_everything(env):
    _two_rooms(env)
    for src in (_grp("!dm", "dm"), None):
        out = env.ball.command("all", source=src)
        assert "Secret plan" in out and "Should I merge A?" in out and "hidden here" not in out


def test_board_in_groups_all_opt_out(env):
    a, _ = _two_rooms(env)
    env.ball.ctx = FakeCtx(config={"board_in_groups": "all"})
    out = env.ball.command("all", source=_grp("!a", "channel"))
    assert "Secret plan" in out and "hidden here" not in out


def test_group_board_zh_hidden_line(env):
    a, _ = _two_rooms(env)
    env.ball.ctx = FakeCtx(config={"language": "zh"})
    assert "🔒 这里隐藏了 1 个其他对话 — 在私聊里用 !ball all 查看" in env.ball.command("all", source=a)
