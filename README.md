# hermes-ball

**Ball shows which of your AI chats are waiting on you, and what each one asked.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin. It never sends messages, never pings,
never rewrites anything — you ask, it answers. No AI summarizer: the "what it asked" line is the last question
sentence of the AI's reply, cut to 40 characters, picked by plain rules.

## Install

```bash
hermes plugins install https://github.com/getaskclaw/hermes-ball
hermes plugins validate ~/.hermes/plugins/ball        # optional
```

Requires Hermes `>=0.23.1`. Restart the gateway to load it.

## Commands

| Command (Matrix `!ball`, elsewhere `/ball`) | Shows |
|---|---|
| `!ball` | this chat: your move / done / AI working / AI stalled |
| `!ball all` | the 3 chats that have waited longest, plus stalled + working counts |
| `!ball more` | every chat waiting on you (`!ball more all` also lists the done ones) |
| `!ball done` | chats the AI answered without asking anything — nothing needed from you |
| `!ball all hide`, `!ball more hide` | same, chat names replaced by 对话 A/B/C (for screenshots) |
| `!ball help` | short help |

`!ball` in a chat:

```
🏀 轮到你 · 8m
它问你：要我写进哪个文件？
```
```
✅ 已答完 · 8m — 无需你处理
```
```
🤖 AI 在干活 · 2m — 不用管
```
```
⚠️ AI 卡住了 · 2h
说了「马上」但没动静 · 回一句 go 催它
```

`!ball all` (output from the test suite):

```
🏀 10 个对话在等你
1. Ball Demo 10 · 1h — 问题10？
2. Ball Demo 9 · 1h — 问题9？
3. Ball Demo 8 · 1h — 问题8？
…还有 7 个：!ball more
⚠️ 1 个 AI 卡住了：Ops — 去那里回 go
🤖 1 个 AI 在干活
```

Only chats where the AI **asked** something count as "waiting on you". Chats where it just answered
(note `replied`) are *done*: not counted, summarised in one line at the bottom, listed by `!ball done`:

```
🏀 2 个对话在等你
1. Ball Demo 2 · 25m — 问题2？
2. Ball Demo 1 · 15m — 问题1？
💤 7 个已答完，无需你处理
```
```
💤 3 个已答完，无需你处理
1. Done 3 · 1h
2. Done 2 · 1h
3. Done 1 · 1h
```

English:

```
🏀 1 chat waiting on you
1. Ball · 1h — Should I deploy main now?
⚠️ 1 AI stalled: Ops — reply go there
```

Nothing waiting: `✅ 没有对话在等你` / `✅ No chats waiting on you` (plus the done line, e.g. `💤 7 done, nothing needed`).
English single chat: `✅ Done · 8m — nothing needed`.

**AI stalled** means the AI ended its turn with a promise ("I'll report back", "马上") but its loop has stopped —
nothing will happen until you reply (e.g. `go`).

**Language**: `language: auto` (default) answers in Chinese if your last message in that chat (or the stored
question) contains Chinese, otherwise English. Set `zh` or `en` to force.

Chat names come from the chat's public name (e.g. the Matrix room name); DMs show `DM`; otherwise
`platform · #hash` (raw ids are never shown).

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
        language: auto          # auto | zh | en
        llm_judge: false        # default; true = ask the active model after each turn
        llm_judge_timeout: 20   # seconds; on timeout/bad JSON the heuristic stands, note = "judge failed"
```

## What is stored

One SQLite row per thread in `<HERMES_HOME>/plugin-data/ball/data.db` (WAL):
`key` (`profile:platform:chat_id:thread_id`), `ball`, `since`, `promise` (matched phrase, ≤200 chars), `note`
(≤80 chars: only the short question the AI asked, e.g. `要我写进哪个文件？`, or `replied` — never the full reply),
`updated`, `label` (chat name), `lang` (`zh`/`en`, from your last message's script — not its text).
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
