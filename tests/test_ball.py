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
    assert cols == ["key", "ball", "since", "promise", "note", "updated"]


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
    assert env.ball.command("all") is None
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
    assert core.render_one({"ball": "human", "since": _iso(3), "promise": None, "note": "asked: ok?"}, NOW) == \
        "🏀 Ball: you · 3m — asked: ok?"
    assert core.render_one({"ball": "ai", "since": _iso(120), "promise": "I'll check", "note": ""}, NOW) == \
        "🏀 Ball: AI · 2h ⚠ empty promise"
    assert "no turns" in core.render_one(None)


def test_render_board_order():
    rows = [
        {"key": "a", "ball": "ai", "since": _iso(50), "promise": "x", "note": "p"},
        {"key": "b", "ball": "human", "since": _iso(5), "promise": None, "note": ""},
        {"key": "c", "ball": "human", "since": _iso(90), "promise": None, "note": "old"},
        {"key": "d", "ball": "ai", "since": _iso(200), "promise": None, "note": ""},
    ]
    lines = core.render_board(rows, NOW).splitlines()
    assert lines[0].startswith("🏀 Ball board — 2 waiting on you, 2 with AI")
    assert [l.split(" · ")[-1].split(" ")[0] for l in lines[1:]] == ["c", "b", "d", "a"]
    assert "⚠" in lines[4] and "⚠" not in lines[3]
    assert "empty" in core.render_board([])


def test_command_handler(env):
    assert "no turns" in env.ball.command("", source=SRC)
    dispatch(env)
    assert env.ball.command("", source=SRC).startswith("🏀 Ball: AI · ")
    assert env.ball.command("all").startswith("🏀 Ball board — 0 waiting on you, 1 with AI")
    assert "no chat thread" in env.ball.command("")


def test_register_matches_manifest(tmp_path):
    import yaml
    ctx = FakeCtx()
    bp.register(ctx)
    manifest = yaml.safe_load((bp.__file__ and __import__("pathlib").Path(bp.__file__).parent / "plugin.yaml").read_text())
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert list(ctx.commands) == ["ball"]
    assert manifest["version"] == "0.1.0" and isinstance(manifest["version"], str)
