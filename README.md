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
| `!ball all hide`, `!ball more hide` | same, chat names replaced by Chat A/B/C (for screenshots) |
| `!ball forget` | remove **this** chat's row from the board (the only command that deletes data) |
| `!ball help` | short help |

`!ball` in a chat:

```
🏀 Your move · 8m
It asks: Which file should I write to?
```
```
✅ Done · 8m — nothing needed
```
```
🤖 AI is working · 2m — nothing needed from you
```
```
⚠️ AI stalled · 2h
Said "I'll report back" but nothing happened · reply go to nudge it
```

`!ball all`:

```
🏀 4 chats waiting on you
1. Blog draft · 2d — Delete the old drafts?
2. Photo sorter · 1d — Should I rename the files?
3. Tax notes · 5h — Which file should I write to?
…1 more: !ball more
⚠️ 1 AI stalled: Deploy helper — reply go there
🤖 1 AI working
💤 3 done, nothing needed
```

Only chats where the AI **asked** something count as "waiting on you". Chats where it just answered
(note `replied`) are *done*: not counted, summarised in one line at the bottom, listed by `!ball done`:

```
💤 3 done, nothing needed
1. Book club · 1d
2. Meal planner · 6h
3. Budget sheet · 25m
```

Nothing waiting: `✅ No chats waiting on you` (plus the done line, e.g. `💤 7 done, nothing needed`).

**AI stalled** means the AI ended its turn with a promise ("I'll report back") but its loop has stopped —
nothing will happen until you reply (e.g. `go`).

**Language**: `language: auto` (default) replies in Chinese if your last message in that chat (or the stored
question) contains CJK characters, otherwise English. Set `zh` or `en` to force.

Chat names come from the chat's public name (e.g. the Matrix room name); DMs show `DM`; otherwise
`platform · #hash` (raw ids are never shown).

## How the ball moves

| Event (Hermes hook) | Ball |
|---|---|
| you send a message (`pre_gateway_dispatch`) | AI |
| approval / clarify / sudo prompt (`on_human_input_request`) | you |
| prompt answered (`on_human_input_resolved`) | AI |
| turn ends (`post_llm_call`) | judged: question or decision ask → you (*asked*); promise in the last 2 sentences → AI + ⚠; otherwise you (*replied* = done) |
| interrupt, `/stop` (`agent_loop_stopped`) | you (`interrupted: <reason>`) |

The turn-end judge is a deterministic phrase heuristic (English and Chinese). Optionally an LLM judge can refine it.
The verdict is decided from the **full** reply and stored as `state` (`asked` / `replied` / `working` /
`interrupted`); the short note is display-only, so truncating a long question never turns it into "Done".
A question always beats a promise. Promise phrases (only counted in the final 2 sentences): `I'll report /
update you / let you know / get back / follow up / check (and tell you) / look into`, `will post here`, `stand by`,
… plus the Chinese equivalents (e.g. "later", "right away", "I will check") ….

## Configuration

```yaml
plugins:
  entries:
    ball:
      settings:
        language: auto          # auto | zh | en
        llm_judge: false        # default; true = ask the active model after each turn
        llm_judge_timeout: 20   # seconds; on timeout/bad JSON the heuristic stands, note = "judge failed"
        stale_days: 7           # board hides chats with no activity for N days (0 = never); hidden, not deleted
```

## What is stored

One SQLite row per thread in `<HERMES_HOME>/plugin-data/ball/data.db` (WAL):
`key` (`profile:platform:chat_id:thread_id`), `ball`, `since`, `promise` (matched phrase, ≤200 chars), `note`
(≤80 chars: only the short question the AI asked, e.g. `Which file should I write to?`, or `replied` — never the full reply),
`updated`, `label` (chat name), `lang` (`zh`/`en`, from your last message's script — not its text), `state`
(`asked`/`replied`/`working`/`interrupted`). Older databases get the `state` column added in place; legacy rows are
backfilled conservatively (anything not clearly a plain reply becomes `asked`, so it still shows as waiting).
Rows are only deleted by `!ball forget`; `stale_days` only hides them from `!ball all/more/done`.
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
- Left/forgotten rooms: Hermes exposes no public plugin signal for "room left" or "session removed" on Matrix, so
  Ball cannot drop those rows itself. They disappear from the board after `stale_days`; `!ball forget` in the chat
  removes one for good (once you have left a room you cannot run it there; the row just stays hidden).
- Pending clarify / question prompts: a raw `!ball` (or ` /ball` with leading spaces) is rewritten to `/ball` in
  `pre_gateway_dispatch`, so the gateway's clarify text-intercept (which skips text starting with `/`) never takes
  it as the answer. **Limit:** while the agent turn is running, a plugin command is not a built-in, so the
  adapter's busy path neither bypasses the active session for it nor fires `pre_gateway_dispatch`; Hermes routes
  `/ball` like any busy follow-up (steer/queue per `busy_input_mode`). Ball cannot intercept that path.
- The heuristic is phrase-based and will misjudge sometimes; `/ball` is a hint, not a contract.

## Development

```bash
python -m pytest -q
hermes plugins validate . --install-deps
```

## License

MIT
