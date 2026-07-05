# AI Live Quiz Show

An AI-hosted live quiz show on YouTube. A scripted AI avatar reads questions
(pre-rendered video clips); viewers answer in YouTube live chat; a backend reads
chat in real time, scores correct answers with speed decay, and renders a live
leaderboard overlay on the stream.

**Current goal:** ship a fun first episode and see if people enjoy it.
Optimize for shipping, not scale. Scrappy beats clever. Monetization is a later
question, not a design input.

## Core architecture

- **Pre-render everything.** The host is scripted, so all avatar clips are
  generated ahead of time (ElevenLabs TTS -> D-ID/HeyGen avatar mp4). The only
  *live* parts are chat ingestion, scoring, and the overlay.
- **`state.json` is the single source of truth.** The scorer writes it; the
  overlay reads it. They are decoupled through this one file. Never couple them
  any other way.
- **Two machines:** a Raspberry Pi 5 runs the overlay server and the chat
  scorer; a Mac runs OBS. OBS pulls the overlay as a browser source over the
  network and composites it onto the pre-rendered avatar video.

## Folder structure

Files are split into folders by role. Scripts resolve their paths relative to
their own location (`HERE`-based), so they work from anywhere and folders can
move together without code edits.

```
code/
├── agents/
│   ├── main.py         Demo entry point: runs hint_generator once for a word.
│   ├── orchestrator.py Loads config.yaml, spawns all configured agents, runs
│   │                   them by name (manual orchestration — you decide when).
│   ├── agent.py        Agent = config block + SKILL.md system prompt +
│   │                   provider-agnostic LangChain model (Anthropic/OpenAI).
│   ├── db.py           SQLite shared agent state. Creates alias_game.db here
│   │                   on first run — gitignored runtime artifact.
│   ├── config.yaml     Per-agent provider/model/temperature/skill routing.
│   │                   Add an agent by adding a block; no code changes.
│   └── skills/         One SKILL.md per agent role: hint_generation,
│                       difficulty_tuning, trend_research.
├── overlay/
│   ├── server.py       Flask overlay server. Serves overlay.html at /, live
│   │                   state at /state, a test control at /timer/<seconds>
│   │                   (and /timer/stop). Reads ../overlay/state.json.
│   ├── overlay.html    Transparent overlay page (leaderboard, question
│   │                   lower-third, countdown ring). Polls /state every 1s.
│   │                   Added to OBS as a Browser Source.
│   └── state.json      The shared state: phase, question, window_ends_at
│                       (epoch seconds), leaderboard ([{name, score}]).
│                       Gitignored — runtime artifact.
├── questions/
│   └── questions.json  window_seconds, points (decay schedule), min_points,
│                       and questions[] with text + answers[] variants.
├── scorer/
│   ├── chat_scorer.py  Finds the active broadcast's liveChatId, polls
│   │                   liveChatMessages.list, scores answers, writes
│   │                   ../overlay/state.json. You press Enter to open each
│   │                   question's answer window (manual orchestrator).
│   ├── normalize.py    Answer normalization + matching. Armenian-aware.
│   ├── youtube_auth.py OAuth with token caching. First run opens a browser;
│   │                   later runs refresh silently. Reads/writes ../secrets/.
│   └── test_youtube_auth.py  Standalone auth smoke test (prints channel name).
└── secrets/            Gitignored entirely — never commit anything here.
    ├── client_secret.json  OAuth client downloaded from Google Cloud.
    └── token.json          Created automatically after first authorization.
```

## How to run

On the Pi, from the repo root (`alias-game/`):

```
pip install flask google-api-python-client google-auth-oauthlib
python code/overlay/server.py       # overlay server, binds 0.0.0.0:8080
python code/scorer/chat_scorer.py   # scorer (broadcast must be LIVE, not just scheduled)
```

Agents (need `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in `.env` at repo root):

```
pip install pyyaml python-dotenv langchain-anthropic langchain-openai
python code/agents/main.py          # demo: generate hints for one word
```

OBS (Mac): Browser Source -> `http://<pi-lan-ip>:8080`, 1920x1080, layered
ABOVE the avatar Media Source.

## Conventions and hard-won lessons

- **Bind the server to `0.0.0.0`**, not `127.0.0.1`, or other devices get
  `ERR_CONNECTION_REFUSED`. `HOST`/`PORT` env vars override.
- **`/timer/<n>` is a control endpoint, not a page.** Never add it as an OBS
  browser source — it returns JSON and will render raw text on the video. Hit it
  from a browser tab (or the scorer calls it); the ring then shows on the main
  overlay.
- **Atomic writes:** write `state.json` via a temp file + `os.replace()` so the
  overlay never reads a half-written file.
- **Score by channelId, not display name.** Names repeat/change; channel IDs are
  stable. Keep the latest display name only for rendering.
- **Speed decay:** first correct answer gets the most points, then less, per the
  `points` schedule in `questions.json`. One scoring chance per person per
  question.
- **Backlog protection:** only count chat messages whose `publishedAt` is after
  the answer window opened. Never score pre-window chatter.
- **Generous answer windows (20–30s).** YouTube ultra-low latency still puts
  viewers ~3–10s behind, plus poll delay. Don't score to the second.

## Armenian text handling (normalize.py)

The Data API returns the original text the viewer typed (creator-device
auto-translation does NOT affect API data), so matching runs on real Armenian:

- NFC normalize, then casefold.
- **Word-internal Armenian marks are DELETED, not spaced** — the question mark ՞
  (U+055E), emphasis ՛, exclamation ՜, apostrophe, and the Armenian hyphen ֊ sit
  *inside* a word. Replacing them with a space wrongly splits the word.
- Other punctuation/symbols/separators become spaces; whitespace is collapsed.
- Matching is **exact on the normalized form** (no fuzzy/substring), so a chatty
  sentence won't score. List every acceptable variant (Armenian spelling,
  transliteration, Latin) in `questions.json` instead.

## Status

Phases 0–4 done: accounts/auth, test stream, one avatar clip, live overlay,
chat reader + scoring. **Next: Phase 5** — generate a full episode's clips and
run an end-to-end dress rehearsal for friends on an unlisted stream.

## Open decisions

- Niche/topic, host persona (name/voice/look), episode cadence + length.
- Show language: Armenian, English, or multilingual.

## Style

Python, standard library where reasonable. Keep modules small and readable with
comments explaining *why*. This is a solo for-fun build — prefer simple,
debuggable code over abstraction. Don't add Redis or a web app unless the show
proves fun first (those are explicit backlog items). The one database is the
agents' small SQLite state file (`code/agents/db.py`) — keep it that way.
