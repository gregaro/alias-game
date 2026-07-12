# AI Live Quiz Show 🎙️🦾

An **AI-hosted, Alias-style word-guessing show** streamed live on YouTube — in Armenian.

An AI avatar host reads witty hints for a secret word; viewers type their guesses into
YouTube live chat; a backend reads the chat in real time, scores correct answers with
speed decay, and renders a live leaderboard on the stream. The words come from live
trend research, the hints are written by an LLM with a sarcastic party-host persona,
and the host's video is generated with TTS + avatar tools.

> **Status:** pre-launch. The full pipeline works end-to-end and a first episode has been
> rendered and live-tested on YouTube; next milestone is a dress rehearsal on an unlisted
> stream. This README evolves with the project.

## How a round works

1. The avatar host reads a **short teaser hint** (4–5 words, oblique and funny). Fast,
   clever guessers earn the most points.
2. A **4-second beat** later the host reads a **fuller, easier hint** — most players
   guess here, for fewer points (speed decay: first correct answer scores highest).
3. To keep the answer out of the scored window, the host announces it as the *next* word
   begins ("that was X — now, next up…"). So a word's window **closes exactly where its
   answer is spoken**, and nobody can score by copying the host. Intro → 10 words →
   outro, the last answer landing in the outro. Correct answers pop onto the on-stream
   leaderboard live — one scoring chance per person per word; scores accumulate across
   the episode.

Windows are **not a fixed length**. The host's pauses and each word's speech run
different durations, so the real boundaries are measured off the rendered video into
`timeline.json`; the scorer replays them against a single "press Enter when the video
starts" sync. The overlay follows the host's *voice* — the previous answer while he
announces it, then the teaser, then the long hint replacing it — while scoring runs on
its own clock underneath.

## Architecture

The design splits the show into a **pre-rendered half** (everything the host says) and a
**live half** (chat ingestion, scoring, overlay) — so nothing latency-critical depends on
an LLM or TTS during the broadcast.

```
   BEFORE THE SHOW (content pipeline)              DURING THE SHOW (live loop)
┌──────────────────────────────────────┐   ┌────────────────────────────────────────┐
│ trends (Google Trends AM/US,         │   │  YouTube Live Chat                     │
│  hy-Wikipedia top reads,             │   │        │ liveChatMessages.list         │
│  YouTube trending AM)                │   │        ▼                               │
│        │                             │   │  chat_scorer.py ── normalize.py        │
│        ▼                             │   │  (answer windows, speed decay,         │
│ trend_researcher agent ── word set   │   │   scores by channelId)                 │
│        │                             │   │        │ atomic writes                 │
│        ▼                             │   │        ▼                               │
│ hint_generator agent ── 3 hints/word │   │   state.json  ◄─── single source       │
│        │                             │   │        │            of truth           │
│        ▼                             │   │        ▼                               │
│ human review → HeyGen episode video  │   │  overlay server (Flask) → overlay.html │
│        │                             │   │        │ polled as OBS Browser Source  │
│        ▼                             │   │        ▼                               │
│ measure timeline.json off the video  │──▶│  (scorer replays those marks live)     │
└──────────────────────────────────────┘   │        ▼                               │
                                           │  OBS composites overlay over the       │
                                           │  pre-rendered host video → YouTube     │
                                           └────────────────────────────────────────┘
```

Two machines run the show: a **Raspberry Pi 5** hosts the overlay server and the chat
scorer; a **Mac** runs OBS, pulling the overlay over the LAN and compositing it onto the
host video. The scorer and the overlay never talk directly — they are decoupled through
one file, `state.json`, written atomically so the overlay never reads a half-written
frame.

## The AI content pipeline

Content is produced by small, config-driven agents (`code/agents/`). Each agent is a
YAML block (provider, model, temperature, token budget) plus a `SKILL.md` system prompt —
adding an agent requires no code changes. Agents share state through a small SQLite DB.

**One command per stage, four stages per show:**

```
python code/agents/research_words.py       # stage 1: live trends -> 10 playable words
python code/agents/generate_hints.py       # stage 2: 3 hints per word, show-ready
python code/agents/generate_questions.py   # stage 3: hints -> questions.json (no LLM)
python code/agents/generate_show_script.py # stage 4: hints -> the episode clip script
```

- **`trend_researcher`** fetches ~50 live topics (Google Trends RSS for Armenia + US,
  Armenian-Wikipedia weekly top reads, YouTube trending in Armenia) and plays ruthless
  game-show editor: most trends make bad game words, so at most ~3 survive; the rest of
  the set are "wildcard" words invented across domains (foods, animals, verbs, famous
  people, places). Encoded policy: everyday spoken forms over dictionary forms, short
  typeable answers, at most 2 words per topic domain, max one political figure per set,
  no repeats within a 3-show window.
- **`hint_generator`** writes 3 escalating hints per word — a 4–5-word oblique teaser,
  a concrete middle hint (kept as spare material), and a confident closer — in the
  persona of a sharp-tongued friend at an Armenian խնջույք. Hard rules: never say the
  word or its root, never translate it, no invented facts about real people.
- **`difficulty_monitor`** (wired, not yet in the loop) will tune hint difficulty from
  live solve-time stats.
- **Stage 3 is plain code, no LLM:** it writes the reviewed hints into
  `questions.json` for the scorer, keeping existing scoring settings. Accepted answers
  are then curated by hand — matching is exact, so every form a viewer might type has to
  be listed (Armenian spellings, both Latin transliteration conventions, the English
  name, nicknames, Russian loanwords in Cyrillic). Stage 3 carries those hand-added
  answers forward on regeneration rather than clobbering them.
- **`show_scripter`** (stage 4) writes the host's frame around the hints — episode
  intro with the rules, a spoiler-free lead-in before each word, a reveal line after
  each answer window (often calling back to a hint's joke), and the outro — in the
  same persona, so the episode sounds like one person talking. It emits
  `show_script.txt` (human reference), `.json` (automation) and `_tts.txt` — the
  paste-ready HeyGen sheet. HeyGen does not parse inline `[pause 4 seconds]` markers
  from pasted text; they mark where to add a real pause of that length by hand via
  HeyGen's AI Studio pause tool before rendering, so the beat between a word's two
  hints ends up baked into one clip per round. The generator validates that the frame
  covers every word in order and that no lead-in leaks a target word.

Model routing is per-agent and provider-agnostic (Anthropic / OpenAI / any model on
OpenRouter). Current lineup after side-by-side A/B tests on real Armenian output:
**Gemini 3.1 Pro** for both research and hints (funniest hints, best command of
Armenian cultural references and street register), with **Claude Opus 4.8** as an
automatic mid-show fallback. Every generated hint passes a leak check (a hint must
never contain its target word) and a human read before TTS recording.

## The live scorer

`code/scorer/chat_scorer.py` finds the active broadcast's live chat, polls it at the
API-suggested interval, and scores answers while a question's window is open:

- **Armenian-aware matching** (`normalize.py`): NFC normalization, case folding, and
  correct handling of Armenian punctuation that sits *inside* words — the question mark
  `՞`, emphasis `՛`, exclamation `՜` and hyphen `֊` are deleted, not split, so
  `Յո՞ւպիտեր` matches `Յուպիտեր`. Matching is exact on the normalized form (no fuzzy
  matching — a chatty sentence never scores); spelling/transliteration variants are
  listed per question instead.
- **Fairness rules learned the hard way:** score by stable `channelId`, not display
  name; only count messages published after the window opened (no backlog sniping);
  generous windows (~30s) because ultra-low-latency YouTube still puts viewers 3–10s
  behind the stream.
- **Two clocks, deliberately.** Scoring covers the whole window — the span in which the
  answer is still secret. The overlay runs on its own clock, following the host's voice.
  A half-filled or inconsistent `timeline.json` is rejected with a printed reason and the
  scorer falls back to fixed windows: better to run plain than to run out of sync.

## Repository layout

```
code/
├── agents/      config-driven LLM agents, skills (SKILL.md per role), SQLite state,
│                trend fetchers, and the two per-show pipeline commands
├── overlay/     Flask server + transparent 1920x1080 overlay page (leaderboard,
│                question lower-third, countdown ring) polled by OBS
├── questions/   questions.json (points decay, questions, curated answers),
│                timeline.json (second-marks measured off the rendered video), and
│                the per-episode show script: show_script.txt (human reference),
│                .json (automation), _tts.txt (paste into HeyGen)
├── scorer/      YouTube auth (OAuth + token cache), chat poller/scorer, Armenian
│                answer normalization
└── secrets/     gitignored: OAuth client + cached token
```

## Running it

```bash
git clone https://github.com/gregaro/alias-game.git && cd alias-game
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY
```

Content pipeline (any machine): the stage commands above.
Show day (Pi): `python code/overlay/server.py` and `python code/scorer/chat_scorer.py`
(broadcast must be live, not just scheduled). OBS (Mac): add a Browser Source pointed
at `http://<pi-ip>:8080`, 1920×1080, layered above the host video.

## Roadmap

- [x] Accounts, OAuth, test stream
- [x] Live overlay (leaderboard, question card, countdown) as OBS Browser Source
- [x] Chat reader + scorer with speed decay and Armenian answer matching
- [x] Agent pipeline: trends → words → hints → questions + clip script, with model
      A/B testing and fallbacks
- [x] First avatar clip (TTS → talking-head video)
- [x] Full episode rendered as one HeyGen video, with inline pause markers
- [x] Question advancement automated in sync with the video (`timeline.json`)
- [ ] Dress rehearsal on an unlisted stream
- [ ] First public episode
- [ ] Feed live solve stats back into hint difficulty (difficulty_monitor)

*Built as a scrappy solo project — simple, debuggable code over abstraction,
and everything optimized for shipping a fun first episode.*
