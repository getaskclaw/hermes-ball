# hermes-ball

![Ball demo: board shows chats waiting on you; answer one, board drops by one](demo/video/ball-demo.gif)

*Demo workspace, fictional projects, unedited plugin output. [MP4 (720p)](demo/video/ball-demo-720.mp4)*


**Ball shows which of your AI chats are waiting on you, and what each one asked.**

Ball is an add-on for [Hermes Agent](https://github.com/NousResearch/hermes-agent), an open-source AI assistant
you talk to from chat apps such as Matrix and Telegram. On Matrix type `!ball all` (on other platforms, `/ball all`) and
Ball lists the chats where the AI asked you something and is still waiting for your reply. Answer one and it drops
off the list.

Ball only speaks when you type the command. It never messages you on its own and never changes what the AI sees
or says. The question it shows is the AI's last question, copied by fixed rules (not an AI summary): stored up to
80 characters, shown up to 40.

**Tested on Matrix only.** Other Hermes chat platforms use the same hooks and should work, but are untested.

## Install

```bash
hermes plugins install https://github.com/getaskclaw/hermes-ball
hermes plugins validate ~/.hermes/plugins/ball        # optional; installs under the plugin name `ball`
hermes gateway restart                                # load it
```

Requires Hermes `>=0.23.1`.

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
said “I'll report back” but went quiet · reply go to nudge it
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

In all output, "reply go" means: send the word `go` in that chat to nudge the AI.

Chats count as "waiting on you" when the AI **asked** something or its turn was interrupted. Chats where it just
answered are *done*: not counted, summarised in one line at the bottom, listed by `!ball done`:

```
💤 3 done, nothing needed
1. Book club · 1d
2. Meal planner · 6h
3. Budget sheet · 25m
```

Nothing waiting: `✅ No chats waiting on you` (plus the done line, e.g. `💤 7 done, nothing needed`).

**AI stalled** means the AI ended its turn with a promise ("I'll report back") but its loop has stopped —
nothing will happen until you reply (e.g. `go`).

| Stored `state` | `ball` | Shown as | Counts as waiting on you? |
|---|---|---|---|
| `asked` | you | 🏀 Your move — *It asks: …* | yes |
| `interrupted` | you | 🏀 Your move — *Interrupted — your call* | yes |
| `replied` | you | ✅ Done / 💤 done line | no |
| AI's turn, or a promise younger than `promise_grace_min` | AI | 🤖 AI is working | no |
| AI holds a promise past the grace period | AI | ⚠️ AI stalled | flagged separately |
| AI turn silent past `working_timeout_min` | AI | ⚠️ AI quiet — may have stopped (restart?) | flagged as stalled |

"Stalled" and "quiet" are not stored; they are computed from the stored row and its age when you ask.

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

How the turn-end judge works:

- It is a deterministic phrase heuristic (English and Chinese). An optional LLM judge can refine it.
- The verdict is decided from the **full** reply and stored as `state`; the short note is display-only, so
  truncating a long question never turns it into "Done".
- A question always beats a promise.
- Promise phrases count only in the final 2 sentences: `I'll report / update you / let you know / get back /
  follow up / check / look into`, `will post here`, `stand by`, and Chinese ones such as `稍后` (later),
  `马上` (right away), `我会检查` (I will check).
- Courtesy closers are not asks: "Let me know if you have any questions", "Feel free to reach out",
  "Happy to help", "Any questions?", and Chinese ones such as `有问题随时告诉我` (tell me if there's a problem).
  A finished reply ending with one shows as *done*. A real ask in the same reply still wins
  ("Should I merge? Let me know if...").
- A promise younger than `promise_grace_min` (default 2 min) shows as "AI working", not stalled.
- Tests: `tests/fixtures/endings.jsonl` holds 88 hand-labelled endings (about half English, half Chinese),
  written by the author while tuning the rules. They are regression tests, not an accuracy measurement;
  expect misjudgements on real chats.

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
        working_timeout_min: 30 # "AI working" older than this shows as "AI quiet — may have stopped (restart?)" (0 = off)
        promise_grace_min: 2    # a promise younger than this shows as "AI working", not stalled (0 = off)
        board_in_groups: here   # here = in a group/channel room, !ball all/more/done shows only that room; all = every chat
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

**Privacy in group rooms:** with the default `board_in_groups: here`, `!ball all/more/done` in a group or channel
shows only that room's row plus "N other chats hidden here — use !ball all in your DM", so other rooms' names and
questions are never posted where other people can read them. DMs and the CLI show the full board.

## Network / LLM disclosure

- Default: **no network calls**, no shell commands, no credentials, no telemetry. Reads only Hermes' in-process
  session routing (public `SessionStore.lookup_by_session_id/lookup_by_session_key`).
- If you enable `llm_judge`: after each turn the last ≤2000 chars of the assistant's final reply are sent through
  `ctx.llm` to **your active model/provider** (your credentials, your spend). It runs in a background thread so it
  never delays a turn. No provider/model override capability is requested.

## Limits

The three that matter most:

- **It guesses.** The heuristic is phrase-based and will misjudge sometimes; `!ball` is a hint, not a contract.
- **Matrix only tested.** Other platforms should work but are untested.
- **It never wakes anything.** "AI stalled" is a flag, not enforcement: Ball never nudges the agent or anyone.

Smaller ones:

- Ball tracks *threads* as Hermes routes them. In DMs the whole DM is one thread; in groups the key is per
  chat+thread, not per user.
- CLI/TUI turns are ignored: there is no chat thread to attach them to.
- Commands (`!ball`, `/ball`, any `/x` or `!x`) never move the ball; `!ball` shows the state from before you asked.
- `since` is when the ball last **changed hands**; a second message from the same side does not reset it.
- A reply that finishes right after install, before any new message arrives, is not recorded.
- Rooms you left are not removed automatically (Hermes has no "room left" signal for plugins); they hide after
  `stale_days`. `!ball forget` removes the current chat for good.
- If the gateway restarts mid-turn, the turn end never arrives; after `working_timeout_min` the chat shows as
  "AI quiet — may have stopped (restart?)".
- If Ball can't write its database, Hermes is unaffected; `!ball` shows a warning line until a write succeeds.
- While the AI is mid-turn, `!ball` is handled like any other follow-up message (queued or steered per your
  `busy_input_mode`), so the answer may arrive after the turn ends.

## Development

```bash
python -m pytest -q
hermes plugins validate . --install-deps
```

## License

MIT
