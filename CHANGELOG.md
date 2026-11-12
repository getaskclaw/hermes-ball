# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/); while on 0.x, a minor bump (0.x.0) marks a change you will notice.

## [0.2.0] - 2026-11-12

### Changed (you will notice)
- **`/ball` with no arguments now shows the board**: the 3 chats that have waited longest, plus counts. Checking only the current chat moved to **`/ball here`**.
- `/ball all` lists every waiting chat (`/ball more` still works and does the same).
- `/ball done` shows the 10 most recent answered chats, newest first; `/ball done all` shows every one.
- Stalled chats get one numbered line each instead of one comma-separated line.
- Cron jobs, API calls and delegated runs (Hermes platform `local`) are left off chat boards by default; `show_local: true` lists them.
- Wording: "answered — nothing for you to do"; ages in Chinese read 「13分钟前」; filler such as 「下一步：」 is dropped from notes; chat names are bold; commands in hints are code, so tapping them on Telegram doesn't send a truncated command.
- Telegram DM topics drop the repeated owner-name prefix.

### Fixed
- File paths in chat names (`~/.hermes/config.yaml`) were removed by the gateway, which treats bare paths as attachments; they are now kept.
- A chat waiting on you whose question had no "?" showed the note "replied"; it now shows the last sentence, or "waiting for your reply" for older rows.

### Privacy
- Ball also reads, read-only, the session title Hermes generated from `state.db`, to name Telegram topics and other chats that have no name. Nothing is written to `state.db`, titles never go to the optional LLM judge, and nothing leaves the machine.

## [0.1.4] - 2026-11-12
- Hints say `/ball` outside Matrix; Telegram topic rows show the Hermes session title.

## [0.1.3] - 2026-11-12
- Board rows are written in `post_gateway_admission` (after authorization), not before it.

## [0.1.2] - 2026-11-11
- Board warning when the `state.db` origin lookup fails, so misses are never silent.

## [0.1.1] - 2026-11-11
- Read-only `state.db` origin lookup so turn ends are recorded after a plugin hot-reload; disclosed in the README.

[0.2.0]: https://github.com/getaskclaw/hermes-ball/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/getaskclaw/hermes-ball/commit/221cb002cb5b854e7cceed6e0594355cf09d5e61
