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
  rendered ahead of time as one HeyGen video. The `[pause N seconds]` markers
  in the script are NOT parsed by HeyGen — each pause is added by hand via
  HeyGen's AI Studio pause tool at the marked spot (then the marker text is
  deleted), so the beat between a word's two hints ends up baked into the
  clip. The only *live* parts are chat ingestion, scoring, and the overlay.
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
│   │                   runs the REFINEMENT LOOP per word (not a single call
│   │                   — see refine.py), saves show-ready [{word, hints}]
│   │                   back to the DB with each word's status. Words run in
│   │                   parallel. `--word W` refines ONE word and prints it
│   │                   without touching the DB: the cheap way to test a
│   │                   prompt change (~4 calls, not ~40).
│   ├── refine.py       The loop engine: produce -> evaluate -> revise, up to
│   │                   max_rounds, keeping the BEST candidate rather than the
│   │                   last. Knows nothing about hints; a role plugs in a
│   │                   spec. Self-test: `python refine.py`.
│   ├── hint_loop.py    The hint half of that loop — gates, prompts, ranking,
│   │                   accept condition, and the blind-solver measurement.
│   │                   Self-test: `python hint_loop.py`.
│   ├── tracing.py      Append-only JSONL trace of a run (code/logs/agents/).
│   │                   Named tracing, not trace: `trace` shadows a stdlib
│   │                   module and sits on sys.path[0] here.
│   ├── review_agents.py  Reads a trace and prints what to act on: words that
│   │                   missed the bar, rounds where the loop made things
│   │                   WORSE, solver extremes, tokens per model. The
│   │                   review_log.py of the agent side — run it after a run.
│   ├── generate_questions.py  Stage three (no LLM): hints from the DB ->
│   │                   ../questions/questions.json for the scorer. Keeps
│   │                   existing scoring settings; answers still need a
│   │                   human variants pass before the show. Also syncs
│   │                   timeline.json's row list to the word order (this is
│   │                   the first stage that KNOWS it) — non-destructively:
│   │                   a row already carrying a measured mark is left alone,
│   │                   only a new/changed word gets a fresh empty row.
│   ├── generate_show_script.py  Stage four: show_scripter agent writes the
│   │                   host frame (intro/lead-ins/reveals/outro); weaves in
│   │                   the hints and writes ../questions/show_script.txt
│   │                   (human reference), .json (automation) and
│   │                   _tts.txt — the paste-ready HeyGen sheet; its inline
│   │                   [pause 4 seconds] markers show where to hand-add
│   │                   the pause in AI Studio (HeyGen doesn't parse them).
│   ├── measure_timeline.py  Stage five (no LLM): rendered video ->
│   │                   timeline.json marks. ffmpeg silencedetect, no speech
│   │                   recognition: the 2N longest gaps (pause + guess window
│   │                   per word) cut the audio into blocks that map onto the
│   │                   known script. hint/start/outro marks are exact; teaser
│   │                   and window 1's start are estimates, flagged "?".
│   │                   Prints a diff against the existing marks; --write
│   │                   saves (--force to overwrite measured ones). Validated
│   │                   on ep1-ep5: every hard mark within ~1s of hand marks.
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
│                       hint_critique, hint_solving, show_script,
│                       difficulty_tuning, trend_research.
├── overlay/
│   ├── server.py       Flask overlay server. Serves overlay.html at /, live
│   │                   state at /state, test controls at /timer/<seconds>
│   │                   (and /timer/stop) and /end (and /end/stop). Reads
│   │                   ../overlay/state.json.
│   ├── overlay.html    OBS overlay: animated/music pre-show countdown with
│   │                   STRICTLY ALLOWLISTED anonymous chat responses (raw
│   │                   viewer text/usernames must never enter the frame),
│   │                   leaderboard, question lower-third, countdown ring,
│   │                   plus an opaque end-card
│   │                   phase (leaderboard glides to center, confetti,
│   │                   thank-you note) for after the show, so it isn't a
│   │                   black frame once the host video ends. Polls /state
│   │                   every 1s. Added to OBS as a Browser Source.
│   └── state.json      The shared state: phase, question, window_ends_at
│                       (epoch seconds), leaderboard ([{name, score}]).
│                       Gitignored — runtime artifact.
├── episode.py          Episode paths, shared by agents/ and scorer/. ONE
│                       FOLDER PER SHOW, so an old episode stays runnable
│                       instead of being overwritten by the next one.
├── questions/
│   ├── current_episode One line naming the active show, e.g.
│   │                   "ep2-2026-07-12". Show night therefore needs no extra
│   │                   typing; `--episode <name>` overrides it to replay an
│   │                   old show. An unknown name fails loudly — running the
│   │                   WRONG episode live is worse than not running.
│   └── episodes/<epN-YYYY-MM-DD>/
│       ├── questions.json  points (decay schedule), min_points,
│       │                   window_seconds (FIXED-mode fallback only), and
│       │                   questions[] with text + hints[] + answers[].
│       │                   Answers are hand-curated; stage 3 carries them
│       │                   forward across ALL episodes, so a word that comes
│       │                   back keeps its transliterations.
│       ├── timeline.json   Second-marks measured off THIS episode's rendered
│       │                   video: one row per word {word, start, teaser,
│       │                   hint} + outro_start + outro_end. `start` is where
│       │                   the host announces the PREVIOUS word's answer —
│       │                   that is the window boundary. `outro_end` (optional)
│       │                   is where the closing line actually FINISHES, not
│       │                   where it begins (outro_start) — set it and the
│       │                   scorer auto-fires the overlay's end card there;
│       │                   leave it null and trigger it by hand with /end.
│       └── show_script.txt / .json / _tts.txt  Clip script; _tts.txt is the
│                           one you paste into HeyGen.
├── scorer/
│   ├── chat_scorer.py  Finds the active broadcast's liveChatId, consumes
│   │                   gRPC streamList (REST polling fallback), scores, writes
│   │                   ../overlay/state.json. You press Enter ONCE, the
│   │                   moment the video starts, and it replays timeline.json
│   │                   from there (FIXED-mode fallback if that file is
│   │                   missing or inconsistent). Also writes a JSONL event
│   │                   log per run (see logs/).
│   ├── review_log.py   Reads a run's log and prints the two things worth
│   │                   acting on: unmatched chat grouped by the word that was
│   │                   open (= missing spellings for answers[]), and pacing
│   │                   (who solved on the teaser vs the long hint, who missed
│   │                   the window). Run it after every show.
│   ├── normalize.py    Answer normalization + matching. Armenian-aware.
│   ├── youtube_auth.py OAuth with token caching. First run opens a browser;
│   │                   later runs refresh silently. Reads/writes ../secrets/.
│   └── test_youtube_auth.py  Standalone auth smoke test (prints channel name).
├── logs/agents/<timestamp>.jsonl      One append-only trace per AGENT run:
│                       every LLM call (model, tokens, latency, the RENDERED
│                       prompt) plus every round's gates/scores/solver result.
│                       Read it with review_agents.py. Gitignored.
├── logs/<episode>/<timestamp>.jsonl   One append-only log per RUN (a rehearsal
│                       and the real show are different facts; neither
│                       overwrites the other). Every chat message gets exactly
│                       one verdict — scored / late / no_match / repeat /
│                       pre_window — plus window opens/closes and hint marks.
│                       Gitignored: runtime artifact, and it carries viewer
│                       names and channel IDs.
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

### YouTube OAuth reauthorization

The Google Cloud project and OAuth client may remain owned by the developer
account. The Google account selected in the consent screen determines which
YouTube channel the cached token controls. For
the production broadcast, authorize `barlylive@gmail.com` and verify that the
API reports the **Barly Live** channel.

On a headless machine, use the dedicated flow from the repository root:

```
.venv/bin/python code/scorer/reauth.py
```

Agent procedure and pitfalls:

1. Run `reauth.py` with outbound network access approved **from the start**.
   The final exchange calls `oauth2.googleapis.com`; starting it in a network-
   restricted sandbox wastes the one-time callback.
2. Leave that exact process running at `Pasted URL:`. Do not restart or
   interrupt it after generating the link: the state and PKCE verifier belong
   to that process, so a callback cannot be handed to a new run.
3. Open the printed link in Safari (known to complete this flow reliably) or a
   private browser window. Sign in explicitly as `barlylive@gmail.com`, not the
   developer account. The "Google hasn't verified this app" warning is expected
   while the OAuth app is in testing; the invited test user can continue.
4. Approve access. The redirect to `http://localhost:53682/` is expected to
   fail because nothing listens there. Copy the **entire URL** from the address
   bar and paste it into the still-running process.
5. If the consent page spins forever, try Safari first. If necessary, while
   signed in as `barlylive@gmail.com`, remove **Alias Game** at
   `https://myaccount.google.com/connections`, then begin a completely fresh
   run. Do not revoke access merely because the localhost redirect fails.
6. Verify the result with outbound network access enabled:

```
.venv/bin/python code/scorer/youtube_auth.py
```

Success for production is `Authorized as: Barly Live`. The helper saves the
new gitignored `code/secrets/token.json` and preserves an existing token under
a timestamped backup name. OAuth callback URLs and authorization codes are
sensitive, short-lived credentials; do not commit or record them in docs.

The scorer runs whatever `questions/current_episode` names. To replay an older
show, name it — the full folder name, or a shorthand (`ep1`, or just `1`). A
shorthand that matches two episodes is an ERROR, never a guess, and the scorer
prints the RESOLVED name before the video rolls so you can see what it picked:

```
python code/scorer/chat_scorer.py --episode ep1-2026-07-11
python code/scorer/chat_scorer.py --episode ep1     # same thing
python code/scorer/chat_scorer.py --episode 1       # same thing
python code/agents/new_episode.py --list      # what episodes exist, * = current
```

Agents (need `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`OPENROUTER_API_KEY` in
`.env` at repo root — OpenRouter serves the Gemini-backed trend researcher):

```
python code/agents/new_episode.py    # per show, stage 0: new folder + point current_episode at it
python code/agents/research_words.py # stage 1: trends -> word set
python code/agents/generate_hints.py # stage 2: word set -> hints (agent loop)
python code/agents/generate_hints.py --word ալարկոտ   # one word, nothing saved
python code/agents/review_agents.py  # what the loop did, and what it cost
python code/agents/generate_questions.py   # stage 3: hints -> questions.json
python code/agents/generate_show_script.py # stage 4: hints -> clip script
python code/agents/measure_timeline.py VIDEO   # stage 5: rendered video -> timeline marks (--write saves)
python code/agents/main.py           # demo: generate hints for one word
```

Stages 1–2 are DB-only; stages 3–4 write into the current episode's folder (or
`--episode`). Re-running a stage overwrites that episode rather than creating a
new one, so iterating on words costs nothing. Run `new_episode.py` only when you
actually start a new show.

### New episode checklist

1. `new_episode.py`, then stages 1-3 (`research_words`, `generate_hints`,
   `generate_questions`). Stage 3 also fills `timeline.json`'s empty word rows.
2. Hand-curate `answers[]` in `questions.json`: transliterations, the English
   name, Cyrillic loanwords, nicknames. A missing spelling is a viewer who knew
   the answer and scored zero.
3. `generate_show_script.py` (stage 4). Paste `show_script_tts.txt` into HeyGen,
   hand-add each `[pause 4 seconds]` in AI Studio, delete the marker text, and
   render the episode as one video.
4. `measure_timeline.py <video>` (stage 5): read the table, check the `?` rows
   (teasers and window 1's start) against the video, then re-run with `--write`.
   It replaces scrubbing by hand. If it stops with "can't tell pauses from
   sentence breaks", the guess windows carry a music bed: try `--noise`.
5. `.venv/bin/python code/scorer/preflight.py --video <video>` must say READY,
   then follow "How to run" for show night.

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

**After editing `overlay.html`, refresh the OBS source — it won't pick up the
change on its own.** A Browser Source loads the page once when it starts and
keeps that same running Chromium instance for the whole stream; saving the
file on disk doesn't make it re-fetch. Symptom: everything that existed when
the source last loaded still works, but anything added since (new phases,
new CSS) silently does nothing — it's simply running old code, no error
anywhere. Fix: right-click the source -> **"Refresh cache of current
page."** First bit by this with the end-card feature: `phase: "ended"` was
reaching `state.json` correctly (the scorer even logged "End card
triggered"), but a stale source had no idea what that phase meant.

## Conventions and hard-won lessons

- **Bind the server to `0.0.0.0`**, not `127.0.0.1`, or other devices get
  `ERR_CONNECTION_REFUSED`. `HOST`/`PORT` env vars override.
- **`/timer/<n>` and `/end` are control endpoints, not pages.** Never add either
  as an OBS browser source — they return JSON and will render raw text on the
  video. Hit them from a browser tab; the effect then shows on the main overlay.
  `/end` switches the overlay to its after-show end card (leaderboard to
  center, confetti, thank-you note) — trigger it once the outro is winding
  down. `/end/stop` reverts to idle.
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
- **Answer matching is exact on the normalized form, plus one-character typo
  tolerance on longer answers.** `matches()` in `normalize.py` accepts an
  answer of 5+ normalized characters at edit distance <= 1 — tuned against a
  real rehearsal's chat log (2026-07-14): recovered 6 genuine typos, zero
  false positives, zero collisions across three episodes' real answers.
  Short answers stay exact (one edit on a 3-letter word covers too much of
  the alphabet to mean anything), and a SYNONYM or different word is never
  close enough in edit distance to slip through — those still have to be
  listed in `answers[]` explicitly, same as ever. Every spelling a viewer
  might type still needs listing — transliterations, the English name,
  nicknames, Russian loanwords in Cyrillic. A missing variant is a viewer
  who knew the answer and scored zero.
- **`generate_questions.py` refuses to write an episode where two different
  words' answers are close enough to blur into each other** — checked both
  exactly and at the same fuzzy distance the scorer now forgives, since a
  chat typo landing inside two words' tolerance at once would credit the
  wrong one. Fails loudly (`SystemExit`) rather than writing a bad file.
- **The failures are the ones you can't see.** A wrong-spelling answer and a
  too-late answer both used to be dropped silently, so the viewers the system
  failed left no trace and a show could look clean while quietly losing people.
  Both are now logged (never scored) and `review_log.py` prints them. Read that
  after every run: unmatched text several people typed while a word was open is
  a missing spelling, not chatter.
- **Never score past the window to be kind to late answers.** The window closes
  where the host says the answer AND where the overlay prints it — measured on
  ep3, the host names the word as little as 0.4s after the close, and the
  overlay shows it instantly. Any grace period pays people for echoing the
  reveal, which chat does naturally out of excitement. If people are missing the
  cut, add silence after the long hint in the RENDER instead; that moves the
  reveal, so the answer stays secret for the whole scored span.
- **Don't let the overlay reflow.** Hide things with `visibility: hidden`, not
  `display: none`: an element leaving the flex row shifts the text sideways for
  a frame, which reads as a glitch on stream.

## The agent refinement loop (refine.py / hint_loop.py)

Stage 2 is a real agent loop, not a script with an LLM call in it. Per word:
generate -> code gates -> blind solver -> critic -> revise, up to
`refinement.max_rounds` (3). Lessons from building and running it:

- **Three vendors, so nothing grades its own work.** Writer is Gemini,
  critic is Claude, solver is gpt-4o-mini. The generator's *fallback* was
  moved off `claude-opus-4-8` for the same reason — a fallback firing would
  have quietly put writer and critic on one model.
- **Measure guessability, never judge it.** A critic that knows the answer
  cannot un-know it, so "is this still guessable?" is a question it always
  answers yes to. A blind solver that never sees the word is the only honest
  signal. It runs BEFORE the critic so the critic sees the result.
- **The solver only climbs the rungs the audience climbs** — hint 1, then
  hints 1+3. Hint 2 is a spare that is never spoken (see below), so grading a
  ladder through it would measure a show that doesn't exist.
- **Judge the solver's guess exactly as you'd judge a viewer's**, via the
  scorer's `matches()` AND the curated answers from past episodes. It once
  answered «ծույլ» for «ալարկոտ» — a synonym this file lists as a valid
  answer — and three good rounds were marked "too obscure" for it.
- **The solver is not your audience.** gpt-4o-mini crushes world knowledge
  and whiffs on local jokes, exactly inverted from viewers. Treat its
  extremes as advisory and compare `solved_at` against real chat solve rates
  in `logs/<episode>/` after a show before trusting it further.
- **Rank on the ladder before the rubric.** Measured live: round 2 scored a
  point higher AND gave the answer away in the teaser, so a score-only
  ranking kept the worse candidate. `_SOLVER_RANK` puts a correct ladder
  above taste points. Too-easy still beats never-solved — a giveaway leaves a
  playable round; an unreachable word is dead air.
- **Never let humor be bought with clarity:** acceptance uses `min()` over
  the rubric, not a total, so a 5 for humor can't pay for a 2 in escalation.
- **`keep_unchanged` is enforced in CODE, not asked for in the prompt.** The
  writer was told to keep hints 1 and 3 and rewrote hint 1 anyway. A kept
  hint is now restored verbatim. But a hint the critic wrote a NOTE about is
  never kept (the critic contradicted itself; the note is more specific), and
  the solver overrides both — a giveaway teaser must move whatever the critic
  says.
- **Stop when no move exists.** If gates are clean and the critic keeps all
  three hints with nothing noted, another round is provably identical.
  Observed burning a full round to change nothing before `exhausted` existed.
- **Gate in code before paying for opinions.** A hint containing its own
  target word needs a rewrite, not a critique — the leak check short-circuits
  the round and skips ~3 calls.
- **A loop's failures are invisible; log the decisions, not just the calls.**
  A hosted tracer gives you prompts, tokens and latency — none of which say
  why round 2 scored worse than round 1. That is why `tracing.py` records
  gates/scores/solver per round, and why `review_agents.py` leads with
  regressions. `observability: none` is the default and works offline; the
  LangSmith/Langfuse seam in `build_model()` is there if a UI is ever wanted.
- **Log the RENDERED prompt.** A bad revise round is nearly always the critic
  notes and the kept-hints block contradicting each other, which the input
  variables don't show.
- **Cost is multiplicative.** 10 words x rounds x ~4 calls. Words refine in
  parallel (`parallel_words`) because that is the wall-clock fix; use
  `--word` to iterate on prompts without paying for the full set.

## Armenian text handling (normalize.py)

The Data API returns the original text the viewer typed (creator-device
auto-translation does NOT affect API data), so matching runs on real Armenian:

- NFC normalize, then casefold.
- **Word-internal Armenian marks are DELETED, not spaced** — the question mark ՞
  (U+055E), emphasis ՛, exclamation ՜, apostrophe, and the Armenian hyphen ֊ sit
  *inside* a word. Replacing them with a space wrongly splits the word.
- Other punctuation/symbols/separators become spaces; whitespace is collapsed.
- Matching is **exact on the normalized form, plus a one-character typo
  tolerance on answers 5+ characters long** — never substring/sentence
  matching, so a chatty sentence still won't score. List every acceptable
  variant (Armenian spelling, transliteration, Latin) in `questions.json`
  regardless; the fuzzy step only forgives a slipped keystroke, not a
  different word.

## Status

Phases 0–4 done: accounts/auth, test stream, one avatar clip, live overlay,
chat reader + scoring. **Phase 5 in progress** — three episodes exist
(`ep1-2026-07-11`, `ep2-2026-07-12`, `ep3-2026-07-13`), each with a fully
measured `start`/`teaser`/`hint`/`outro_end` timeline, curated answers, and an
after-show end card that auto-triggers at `outro_end`. `rehearsal_mode` is off
in `agents/config.yaml`.

**Agents are being made real agents** (branch
`worktree-agent-refinement-loop`). Stage 2 (hints) now runs the refinement
loop above and is validated on real single-word runs; a full 10-word run has
not been done yet. Stage 1 (`research_words.py`) is still a single pass — a
backlog item (see the README roadmap), deliberately deferred until the loop
has proven itself on hints across a real show.

**All OpenAI models go through OpenRouter** (`provider: openrouter`, slug
`openai/...`), never `provider: openai`. One key, one bill — and the direct
account 429s at call time, which surfaces halfway through a run rather than
at startup.

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
