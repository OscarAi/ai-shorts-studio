# AI Shorts Studio

[![Tests](https://github.com/OscarAi/ai-shorts-studio/actions/workflows/tests.yml/badge.svg)](https://github.com/OscarAi/ai-shorts-studio/actions/workflows/tests.yml)

A local, human-in-the-loop studio for making **fact-checked short videos** (YouTube Shorts,
Instagram Reels, TikTok) on a $0 budget.

The AI does the heavy lifting: research, scripts, storyboards. **Python decides what is
allowed and a person approves every fact**, so the AI can never put an unchecked claim on
screen. Every fact in a script is traced back to a source, and the final video is built
locally with FFmpeg.

> **Project status: finished and archived (October 2026).** Built in five days, with six videos
> made and five published. The tool worked; the channel didn't get the views to justify
> continuing. The code, tests and docs are kept here as a complete, working example. See
> [What I learned](#what-i-learned).

---

## What it does

```mermaid
flowchart LR
    I[Idea] --> R[Research] --> F[Fact check] --> S[Script] --> B[Storyboard]
    B --> P[Production] --> Q[QA] --> H[Human review] --> A[Approved]
    A --> SC[Scheduled] --> PUB[Published]
    A --> PUB
```

Every episode moves through a fixed pipeline. Python checks each step's rules before it can
move on:

| Stage | What happens | Who decides |
|---|---|---|
| **Idea** | An AI suggests little-known topics; an originality gate compares each one with everything already made | You accept or reject |
| **Research** | An AI proposes claims with sources; the app opens every link and finds an evidence quote on the page | Python blocks broken, invented or declined sources |
| **Fact check** | An AI gives a second opinion on each claim | **Only you** approve a claim |
| **Script** | An AI writes ~35 s of narration; every factual sentence must cite approved claims | Python rejects unknown or unapproved claims |
| **Storyboard** | An AI plans 12 shots with image prompts in the channel's style | Python checks coverage and timing |
| **Production** | You make the pictures (free tools with checked licences); narration by a local voice model; word-timed subtitles; FFmpeg builds a 1080×1920 MP4 | Python blocks an outdated video |
| **QA → Publish** | Checklist, per-platform post texts with sources and AI disclosure, manual upload, links recorded | You click Publish; the episode is then locked |
| **Analytics** | Views, watch time, shares at day 1/7/30; compare by hook, style, voice, length, posting time | |

**AI inside the app, for free.** "Run with AI" uses free tiers of Google Gemini, Groq or
OpenRouter. If one is busy, the app moves on to the next. The AI fixes its own rule
violations (up to 2 retries), every request is logged, and limits per day and per click keep
it free. Without any key, everything also works by copy-pasting into any chat AI.

## Highlights

- **AI suggests, Python decides.** Agents' answers go through the same Pydantic models and
  deterministic validators as a human's. New claims always start as *needs review*.
- **Traceable facts.** Each script sentence is `factual` (with claim ids), `creative` or
  `transition`. Subtitles use the approved script text; speech recognition only times the words.
- **Append-only history.** Every save is a new version in SQLite, with an audit trail.
  Approved claims are protected and published episodes are locked.
- **Safe AI use.**
  - Keys live only in a git-ignored `.env` and are never logged.
  - Free models only, with daily limits plus a request and time budget per click.
  - One AI run at a time; prompt-injection guards on every prompt.
  - A pre-commit hook blocks commits that contain keys.
- **Tested.** 387 automated tests (no network, fake AI and fake fetchers). ruff, mypy and
  pip-audit run on every push in GitHub Actions on Windows.

## Tech stack

| Area | Choice |
|---|---|
| Language | Python 3.13 |
| Data | Pydantic v2 models, SQLite (stdlib `sqlite3`), YAML config |
| UI | Streamlit dashboard + an `argparse` CLI (47 commands) |
| AI | Gemini / Groq / OpenRouter through one small OpenAI-compatible client (stdlib `urllib`, no SDKs or frameworks) |
| Voice | Kokoro-82M via `kokoro-onnx`, run locally |
| Subtitles | `faster-whisper` for word timing only; burned in as ASS |
| Video | FFmpeg / ffprobe |
| Quality | pytest + pytest-xdist, ruff, mypy, pip-audit, Dependabot, GitHub Actions |

Why these and not the alternatives: **[docs/DECISIONS.md](docs/DECISIONS.md)**.

## Quick start (Windows)

You need Python 3.13, Git and FFmpeg (`scoop install ffmpeg`).

```powershell
git clone https://github.com/OscarAi/ai-shorts-studio.git
cd ai-shorts-studio
powershell -ExecutionPolicy Bypass -File setup.ps1   # creates .venv from the exact tested versions
.venv\Scripts\python cli.py init                     # creates the database in data/
.venv\Scripts\python cli.py demo                     # a sample episode to look at
run_app.bat                                          # opens the dashboard in your browser
```

Optional: copy `.env.example` to `.env` and add a free AI key (Gemini, Groq or OpenRouter).
Without one, use copy-paste.

Run the tests: `.venv\Scripts\python -m pytest -n auto`

The voice and speech models download on first use (~350 MB and ~145 MB) into `data/models/`.
Everything you make stays in `data/`, which is git-ignored.

## Documentation

| Document | What's in it |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | The full manual: every stage, the dashboard, all commands |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why each technology and design choice, and what was rejected |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design decisions, problem by problem |
| [docs/STRATEGY.md](docs/STRATEGY.md) | Research on what makes short videos work, with sources |
| [CLAUDE.md](CLAUDE.md) | Project rules for AI coding assistants |

## Project layout

```
cli.py, dashboard.py      entry points (command line, Streamlit)
studio/                   the core: models, rules, database, agents, AI, media, analytics
ui/                       dashboard pages
config/                   settings.yaml, ai.yaml, tools.yaml, prompts/*.md (all safe to edit)
tests/                    387 tests, no network
docs/                     guide, decisions, architecture, strategy
scripts/                  launcher helper, secret guard (pre-commit)
data/                     your database, media, logs, backups (git-ignored)
```

## What I learned

- **Building the pipeline was the easy part; distribution is the hard part.**
  - A careful, fact-checked process doesn't by itself bring viewers.
  - The first video did best: little-known story, bold first line.
  - The next ones, on well-known stories with generic openers, did far worse.
- **The first two seconds and the topic matter more than production quality.** The research
  and the changes this led to are in [docs/STRATEGY.md](docs/STRATEGY.md).
- **Free AI tiers are good enough, but unreliable.** Models hang or return "busy", so fallbacks,
  time limits and self-repair are essential, not extras.
- **Making the AI only suggest kept the content trustworthy.** Invented URLs, unapproved facts
  and over-long scripts were all caught by plain Python, not by another AI.
- **New social accounts can be restricted without explanation.** TikTok silently blocked a new
  account's likes and gave its videos 0 views. Expect that and plan around it.

## Licence

No licence has been chosen yet, so all rights are reserved by default. Open an issue if you'd
like to reuse something.
