# AI Shorts Studio: notes for AI coding assistants

Project rules and conventions for anyone (human or AI) changing this code. Overview:
[README.md](README.md). Why things are built this way: [docs/DECISIONS.md](docs/DECISIONS.md).

**Status:** finished and archived (October 2026). Everything below still applies if you pick
it up again.

## Environment
- Windows, Python 3.13, a project `.venv` created by `setup.ps1` from `requirements-lock.txt` (the
  exact versions that passed the suite). Run things as `.venv\Scripts\python -m pytest`,
  `.venv\Scripts\python cli.py ...`.
- Re-freeze the lock only after a full green run.
- FFmpeg/ffprobe must be on PATH; FFmpeg tests skip without it.
- `data/` (database, media, models, logs, backups) is git-ignored and must never be committed.

## Hard rules
- **Python, not an AI**, enforces:
  - required fields;
  - source rules;
  - valid state transitions;
  - claim approvals.
- **Facts:**
  - Never treat a fact as verified because an AI said so. Facts need sources plus evidence.
  - Only `review.py` approves claims.
  - The scriptwriter may only use approved claims, traced per sentence (factual / creative /
    transition).
- **Agents only suggest.**
  - Their answers go through `parse_response` → `preview` → rules.
  - Nothing is saved without an explicit save.
  - New claims are always `needs_review`.
- **Keys** come only from environment variables or the git-ignored `.env`. They are never
  hard-coded, logged, or shown. `scripts/check_secrets.py` runs as a pre-commit hook and in CI.
- **Free models only** (`allow_paid: false`). Tests never call a real AI or the network
  (FakeLLM, FakeSend, FakeFetcher, FakeAligner, FakeTTS; conftest sets `SHORTS_NO_DOTENV=1`).
- **Storage:**
  - SQLite is the source of truth, and every save is a new version.
  - Approved claims are protected.
  - PUBLISHED is locked, except `save(publication_only=True)` for links and post texts.
- **Human approval:** required before expensive generation and always before publishing. Use
  official platform APIs only.
- **No new infrastructure** (MCP servers, LangChain, CrewAI, AutoGen, n8n, Docker) unless a
  specific requirement justifies it. Keep it modular, but don't over-engineer.

## Layout
- **Entry points:** `cli.py` (an `@command` registry) | `dashboard.py` (page setup and routing
  only) + `ui/` (one module per page/section).
- **`ui/core.repo` and `.settings`** forward to `get_repo()` on every use. Never cache a repo in a
  module global: Streamlit keeps modules imported across reruns.
- **`studio/` core:**
  - `models.py` (Pydantic, canonical)
  - `state_machine.py`
  - `validators.py` (deterministic rules with issue codes; `CHECKS_BY_STAGE`)
  - `db.py` (repository: versions, events, metrics, ai_calls, ai_usage, blocked_sites)
  - `guide.py` (per-stage checklist ticked from issue codes)
  - `timeutil.py` (stored UTC vs local "YYYY-MM-DD HH:MM")
- **`studio/` AI and research:**
  - `agents.py` (the 4 agents: prompt / answer model / apply / mock) + `prompts.py` +
    `config/prompts/*.md`
  - `llm.py` (OpenAI-compatible client, Router fallback with cooldowns, daily limits,
    `RunBudget`, FakeLLM)
  - `ai_runner.py` (`ask_agent`: one run at a time, self-repair of FIXABLE codes, AI log)
  - `ideas.py` (topic scout), `copywriter.py`
  - `sources.py` (link checks, evidence quotes), `declines.py`
  - `originality.py` + `similarity.py` (the originality gate), `review.py` (the human steps)
- **`studio/` media:**
  - `assets.py` (uploads in `data/assets/<EP>`, ffprobe), `render.py` (the FFmpeg build)
  - `tts.py` (Kokoro), `subtitles.py` (script text timed by faster-whisper; .srt + .ass with
    the hook overlay)
- **`studio/` publishing:** `platforms.py` (per-platform rules, go-live state), `analytics.py`
  (append-only metric snapshots, compare), `health.py`.
- **Config:** `config/settings.yaml`, `ai.yaml`, `tools.yaml` (all validated by Pydantic; a typo
  gives "X has a mistake").
- **Tests:** `tests/` (`helpers`: open_gate, walk_to, sample_episode, publish; `media.produced` is
  built once per process).

## Conventions
- **A milestone is done only when** `pytest -n auto`, `ruff check .` and `mypy studio` all pass.
  Commit one step at a time, only on a green suite.
- **Real data:** test changes against a copy of `data/studio.db` (set `SHORTS_DATA_DIR`), never
  the real one.
- **New rules** belong in `validators.py` with an issue code. If an AI can fix one, add the code to
  `ai_runner.FIXABLE`.
- **Changing a prompt's JSON example** must keep it valid for its answer model
  (`tests/test_prompts.py` checks this).
- **Style:** match the surrounding code; plain-language docstrings and user messages.

## Gotchas
- **Streamlit AppTest:** `DateInput.set_value` stores a range and is ignored inside forms; test
  with defaults.
- **faster-whisper's own decoder** breaks with newer PyAV: audio is decoded with FFmpeg instead.
- **Gemini free tier:** sometimes answers 503 or hangs; flash-lite is usually fast. Groq
  model ids change; check with `cli.py ai-test`.
- **Editing files from Bash heredocs on Windows** can mangle backslashes and non-ASCII
  characters. Use a proper editor or a Python script for such lines.

## Commands
```
python -m pytest -n auto | python -m ruff check . | python -m mypy studio      (prefix: .venv\Scripts\)
python -m streamlit run dashboard.py   (or run_app.bat)
python cli.py demo | list | show EP_x | next EP_x | doctor
python cli.py prompt/paste/mock EP_x <agent> | ai EP_x [--save] | ai-test
python cli.py verify EP_x | source-ok EP_x S001 --note ... | decline EP_x S001 --reason ...
python cli.py ideas / ideas-paste | asset / narrate / subtitles / render / qa EP_x ...
python cli.py publish-info / schedule / live / published / add-link EP_x ...
python cli.py stats EP_x --platform youtube_shorts --views .. --avg-view-sec 0:26 | stats-due | stats-show EP_x
```
