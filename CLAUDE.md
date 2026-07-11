# AI Live Quiz Show

An AI-hosted live quiz show on YouTube. A scripted AI avatar reads questions
(pre-rendered video clips); viewers answer in YouTube live chat; a backend reads
chat in real time, scores correct answers with speed decay, and renders a live
leaderboard overlay on the stream.

**Current goal:** ship a fun first episode and see if people enjoy it.
Optimize for shipping, not scale. Scrappy beats clever. Monetization is a later
question, not a design input.

## Core architecture

- **Pre-render everything.** The host is scripted, so the whole episode is
  rendered ahead of time as one HeyGen video. HeyGen honors inline
  `[pause N seconds]` markers, so the beat between a word's two hints is baked
  into the script text and each round is one clip. The only *live* parts are
  chat ingestion, scoring, and the overlay.
- **The video is the clock.** Because the pauses and each word's speech run
  different lengths, windows are NOT a fixed number of seconds. Second-marks are
  measured off the rendered video into `questions/timeline.json`, and the scorer
  replays them against a single "press Enter when the video starts" sync.
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
│   ├── research_words.py  One command per show: fetch trends -> LLM editor ->
│   │                   playable words. Pulls recent_words from the DB so
│   │                   shows don't repeat themselves (unless rehearsal_mode
│   │                   is on in config.yaml).
│   ├── generate_hints.py  Stage two: reads the latest word set from the DB,
│   │                   runs hint_generator per word, saves show-ready
│   │                   [{word, hints}] back to the DB. Flags hints that leak
│   │                   their target word — review before TTS recording.
│   ├── generate_questions.py  Stage three (no LLM): hints from the DB ->
│   │                   ../questions/questions.json for the scorer. Keeps
│   │                   existing scoring settings; answers still need a
│   │                   human variants pass before the show.
│   ├── generate_show_script.py  Stage four: show_scripter agent writes the
│   │                   host frame (intro/lead-ins/reveals/outro); weaves in
│   │                   the hints and writes ../questions/show_script.txt
│   │                   (human reference), .json (automation) and
│   │                   _tts.txt — the paste-ready HeyGen sheet, with the
│   │                   beat between hints as an inline [pause 4 seconds].
│   ├── fetch_topics.py Topic fetchers: Google Trends RSS (geo=AM + US),
│   │                   hy-Wikipedia top reads, YouTube trending in AM
│   │                   (reuses the scorer's OAuth). A failed source is a
│   │                   warning, not an error. Run alone to eyeball feeds.
│   ├── orchestrator.py Loads config.yaml, spawns all configured agents, runs
│   │                   them by name (manual orchestration — you decide when).
│   ├── agent.py        Agent = config block + SKILL.md system prompt +
│   │                   provider-agnostic LangChain model (Anthropic/OpenAI).
│   ├── db.py           SQLite shared agent state. Creates alias_game.db here
│   │                   on first run — gitignored runtime artifact. Run
│   │                   directly (python db.py) to set up/upgrade the schema
│   │                   on a fresh machine; init_db is idempotent.
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
│   ├── questions.json  points (decay schedule), min_points, window_seconds
│   │                   (FIXED-mode fallback only), and questions[] with
│   │                   text + hints[] + answers[] variants. The answers are
│   │                   hand-curated; stage 3 carries them forward on regen.
│   ├── timeline.json   Second-marks measured off the rendered video: one row
│   │                   per word {word, start, teaser, hint} + outro_start.
│   │                   `start` is where the host announces the PREVIOUS
│   │                   word's answer — that is the window boundary. Committed
│   │                   (it describes a specific episode's video).
│   └── show_script.txt / .json / _tts.txt  Per-episode clip script; _tts.txt
│                       is the one you paste into HeyGen.
├── scorer/
│   ├── chat_scorer.py  Finds the active broadcast's liveChatId, polls
│   │                   liveChatMessages.list, scores answers, writes
│   │                   ../overlay/state.json. You press Enter ONCE, the
│   │                   moment the video starts, and it replays timeline.json
│   │                   from there (FIXED-mode fallback if that file is
│   │                   missing or inconsistent).
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
pip install -r requirements.txt
python code/overlay/server.py       # overlay server, binds 0.0.0.0:8080
python code/scorer/chat_scorer.py   # scorer (broadcast must be LIVE, not just scheduled)
```

Agents (need `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`OPENROUTER_API_KEY` in
`.env` at repo root — OpenRouter serves the Gemini-backed trend researcher):

```
python code/agents/main.py           # demo: generate hints for one word
python code/agents/research_words.py # per show, stage 1: trends -> word set
python code/agents/generate_hints.py # per show, stage 2: word set -> hints
python code/agents/generate_questions.py # stage 3: hints -> questions.json
python code/agents/generate_show_script.py # stage 4: hints -> clip script
```

## Setup on a new machine

Only two gitignored files carry real secrets and must be copied by hand
(`scp` over the LAN — never commit them, never paste them through chat/cloud
apps). Everything else is recreated automatically: `token.json` via the
browser auth flow, `state.json` / `alias_game.db` / `__pycache__` at runtime.

```
git clone https://github.com/gregaro/alias-game.git && cd alias-game
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # then fill in the API keys, or scp .env from the Pi
scp <user>@<pi-ip>:alias-game/code/secrets/client_secret.json code/secrets/
python code/agents/main.py          # smoke test
```

Note: the OBS Mac needs none of this to run the show — its only job is a
Browser Source pointed at the Pi. This setup is for development.

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
- **A window closes exactly where the host says the answer.** That is why
  `timeline.json`'s `start` marks the host announcing the PREVIOUS word's
  answer, not the current word's first hint. Score any later and chat can just
  copy the host off the video.
- **Scoring and display are two different clocks.** Scoring runs the whole
  window (the span in which the answer is secret). The overlay instead follows
  the host's voice: previous answer while he announces it, then the teaser, then
  the long hint replacing it, with the ring running teaser -> end of window.
  Don't collapse them back into one.
- **The overlay only shows hints the host actually SAYS.** `questions.json`
  holds three hints per word, but the script speaks only the first and last —
  the middle one is a spare. Never render it.
- **Answer matching is exact on the normalized form.** Every spelling a viewer
  might type has to be listed in `answers[]` — transliterations, the English
  name, nicknames, Russian loanwords in Cyrillic. A missing variant is a viewer
  who knew the answer and scored zero.
- **Don't let the overlay reflow.** Hide things with `visibility: hidden`, not
  `display: none`: an element leaving the flex row shifts the text sideways for
  a frame, which reads as a glitch on stream.

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
chat reader + scoring. **Phase 5 in progress** — a full 10-word episode is
scripted and rendered, and a first live test on YouTube drove the timeline
scorer and the overlay's answer/hint/countdown behaviour.

Before the next run:

- Fill `teaser`/`hint` marks for all 10 rows in `questions/timeline.json`.
  Without them the scorer falls back to showing both hints at window open,
  which spoils the closer and skips the answer reveal.
- Set `rehearsal_mode: false` in `agents/config.yaml`.

## Open decisions

- Niche/topic, host persona (name/voice/look), episode cadence + length.

(Show language is settled: **Armenian** — hints, host script and overlay chrome
are all Armenian.)

## Style

Python, standard library where reasonable. Keep modules small and readable with
comments explaining *why*. This is a solo for-fun build — prefer simple,
debuggable code over abstraction. Don't add Redis or a web app unless the show
proves fun first (those are explicit backlog items). The one database is the
agents' small SQLite state file (`code/agents/db.py`) — keep it that way.
