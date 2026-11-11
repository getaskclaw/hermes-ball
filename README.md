# hermes-ball

**Whose turn is it?** Ball is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that keeps,
for every chat thread, one fact: is the ball with **you** or with the **AI** — and since when.
It is a board, not a nag: it never sends messages, never pings, never rewrites anything. You ask, it answers.

It also raises a flag when the AI ends its turn with a promise ("I'll report back", "稍后发你") but the agent
loop has actually stopped — an *empty promise*.

## Install

```bash
hermes plugins install https://github.com/getaskclaw/hermes-ball
hermes plugins validate ~/.hermes/plugins/ball        # optional
```

Requires Hermes `>=0.23.1`. Restart the gateway to load it.

## Commands

| Command | Shows |
|---|---|
| `/ball` (Matrix: `!ball`) | this thread, e.g. `🏀 Ball: you · 3m — asked: Should I deploy?` |
| `/ball all` | the board: threads waiting on you first, then oldest first; age, `⚠` empty-promise flag, note |

Sample `/ball all`:

```
🏀 Ball board — 1 waiting on you, 2 with AI
you ·   1h · Ball (matrix) — asked: Should I deploy?
AI  ·   5m ⚠ · DM (telegram) — promised: I'll check the logs… ← here
AI  ·   2d · matrix · #a1b2 — you spoke
```

Threads are labelled from the chat's public name (`SessionSource.chat_name`, e.g. the Matrix room name); DMs without a name show `DM (platform)`; otherwise `platform · #hash` (short hash of the chat id — raw ids are never shown). Notes are cut to 60 chars; `← here` marks the thread you asked from.

## How the ball moves

| Event (Hermes hook) | Ball |
|---|---|
| you send a message (`pre_gateway_dispatch`) | AI |
| approval / clarify / sudo prompt (`on_human_input_request`) | you |
| prompt answered (`on_human_input_resolved`) | AI |
| turn ends (`post_llm_call`) | judged: question or decision ask → you; unfulfilled promise → AI + ⚠; otherwise you |
| interrupt, `/stop` (`agent_loop_stopped`) | you (`interrupted: <reason>`) |

The turn-end judge is a deterministic English + Chinese phrase heuristic. Optionally an LLM judge can refine it.

## Configuration

```yaml
plugins:
  entries:
    ball:
      settings:
        llm_judge: false        # default; true = ask the active model after each turn
        llm_judge_timeout: 20   # seconds; on timeout/bad JSON the heuristic stands, note = "judge failed"
```

## What is stored

One SQLite row per thread in `<HERMES_HOME>/plugin-data/ball/data.db` (WAL):
`key` (`profile:platform:chat_id:thread_id`), `ball`, `since`, `promise` (matched phrase, ≤200 chars), `note`
(≤200 chars, e.g. the last line of a question or a redacted approval prompt), `updated`.
**No transcripts, no message bodies.** The session→thread map lives only in memory.

## Network / LLM disclosure

- Default: **no network calls**, no shell commands, no credentials, no telemetry. Reads only Hermes' in-process
  session routing (public `SessionStore.lookup_by_session_id/lookup_by_session_key`).
- If you enable `llm_judge`: after each turn the last ≤2000 chars of the assistant's final reply are sent through
  `ctx.llm` to **your active model/provider** (your credentials, your spend). It runs in a background thread so it
  never delays a turn. No provider/model override capability is requested.

## Limits

- Ball tracks *threads* as Hermes routes them. In DMs the whole DM is one thread; group sessions follow your
  `group_sessions_per_user` / thread settings, but the Ball key is per chat+thread, not per user.
- Tested against Matrix semantics (`!ball`); other platforms should work but are untested.
- The empty-promise flag is a flag, not enforcement — Ball never wakes the agent up or nudges anyone.
- Turn ends on non-gateway surfaces (CLI/TUI) are ignored: there is no chat thread to attach them to.
- Commands (`/ball`, Matrix `!ball`, any `/x` or `!x`) never move the ball; `/ball` shows the state as it was
  before you asked.
- `since` is when the ball last **changed hands**. Another message from the same holder (e.g. you writing twice
  while it is with the AI) does not reset it — the age answers "how long has it been with them".
- Ball maps a turn end (`post_llm_call` only carries a `session_id`) to a chat through the gateway's
  `session_store`, which it first receives with an inbound message. A reply that finishes right after Ball is
  installed/hot-loaded, before any message has arrived, is not recorded (logged at INFO as "turn not recorded").
- The heuristic is phrase-based and will misjudge sometimes; `/ball` is a hint, not a contract.

## Development

```bash
python -m pytest -q
hermes plugins validate . --install-deps
```

## License

MIT
