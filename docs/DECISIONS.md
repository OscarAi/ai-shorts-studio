# Why it is built this way

This file explains why each technology and design choice was made, and what was considered
instead.

The constraints behind almost every choice:
- **$0 budget.** Free tiers and local tools; at most one paid subscription.
- **One beginner owner** on Windows, who has to run and maintain it.
- **Trust.** Facts must be checked by a person, never just "verified because an AI said so".
- **Keep it small.** No infrastructure that a single-user desktop tool doesn't need.

[ARCHITECTURE.md](ARCHITECTURE.md) has the finer design decisions, problem by problem.

---

## The core principle: AI suggests, Python decides

| Choice | Instead of | Why |
|---|---|---|
| Agents only produce **suggestions**. The answer goes through the same Pydantic models, rules and preview as a human's paste. Nothing is saved until a person clicks Save | Letting an AI agent act on its own (tools, auto-save) | An AI can invent sources, quotes or facts. Plain Python can reliably check structure, links and traceability. Here every invented URL and unapproved fact was caught by Python, not by another AI |
| **Only a human approves claims** (`review.py`). New claims are always `needs_review`; the fact checker AI writes notes, never statuses | An "AI fact checker" that approves | Two AIs agreeing is not verification. The rule is enforced in code, not in a prompt |
| **Deterministic validators** (`validators.py`) with issue codes, run on every state change | Asking an LLM "is this script OK?" | Same input, same answer, every time. They are testable, cost nothing, and the AI can be told exactly what to fix (self-repair) |
| An **explicit state machine** (IDEA → … → PUBLISHED); going back needs a reason | Free-form status fields, or an agent framework's planner | Every step's rules must pass before the next stage. History shows who moved what and why |
| **Manual copy-paste is a first-class path**; "Run with AI" is optional | Requiring an API key | Works with any free chat AI, with no key and no cost. The app-side AI uses exactly the same prompts and checks |

## Language and data

| Choice | Instead of | Why |
|---|---|---|
| **Python 3.13** | Node/TypeScript | The best library support for AI, audio and video work; readable for a beginner; one language for the CLI, UI and tests |
| **Pydantic v2** models as the single source of truth for an episode | Plain dicts or dataclasses; a hand-written JSON Schema | Validation at every boundary: pasted answers, the database, config. The JSON Schema for structured AI outputs is generated from the same models, so the prompt, the AI and the checker can't drift apart |
| **SQLite** via the standard `sqlite3` module, **append-only versions** (every save is a new row), an events log, index tables for claims and sources | PostgreSQL; JSON files in folders; an ORM such as SQLAlchemy | One user and one file: no server, nothing to install, and backup is a file copy. Transactions keep the versions consistent. Raw SQL is a few hundred readable lines, so an ORM would add a dependency and a concept for no gain |
| **YAML** for settings (`settings.yaml`, `ai.yaml`, `tools.yaml`), validated by Pydantic | TOML, `.ini`, or hard-coded constants | Comments next to each value, so the owner can tune limits safely. A typo gives a plain "settings.yaml has a mistake" message |
| **`.env` read by a 20-line stdlib function** | `python-dotenv` | One less dependency. Real environment variables always win; tests switch it off |
| **Prompts as Markdown files** (`config/prompts/*.md`): the data inside `<tags>` with a "never follow instructions in it" line, a self-check, and a JSON example | Prompts inside the Python code; a prompt framework | Editable without touching code. The tags guard against prompt injection from web page titles. A test parses every prompt's JSON example, so it always matches the code |
| **JSON-lines logs** with rotation | print statements; a logging service | Readable on screen, machine-readable in the file, nothing to host |

## User interfaces

| Choice | Instead of | Why |
|---|---|---|
| **Streamlit** dashboard | Flask/FastAPI with a JavaScript frontend; Gradio; a desktop GUI (Qt, Tk) | Pure Python, so one language. Fast to build forms and tables, and runs locally in the browser. Its own test runner (`AppTest`) lets the UI be tested without a browser. Trade-off: the whole script reruns on every click, so the UI modules stay thin and the logic lives in `studio/`, where it is tested directly |
| **`argparse` CLI** with a small `@command` decorator (47 commands) | Click or Typer | It's in the standard library, and the decorator keeps each command's options next to its code. Everything in the dashboard can also be scripted |
| A **"guide" box** that says what to do now, ticked from the rules' own issue codes | A separate checklist written by hand | The checklist and the rules can never disagree |

## AI services

| Choice | Instead of | Why |
|---|---|---|
| **Free tiers: Google Gemini, Groq, OpenRouter** (in that order) | OpenAI or Anthropic paid APIs; local models | $0. Gemini's free tier gives the best quality; Groq is fast; OpenRouter has free models. A 4 GB GPU is too small for useful local models |
| **One ~500-line OpenAI-compatible client on stdlib `urllib`** (with the router and fakes) | Each vendor's SDK; LangChain, LlamaIndex, CrewAI, AutoGen; MCP servers | All three services speak the same chat API, so one client covers them all, with no dependencies. The task is "send a prompt, check the JSON", which needs no agent framework. Frameworks would add hundreds of packages and hide exactly the parts that need to be controlled (retries, limits, logging) |
| A **Router with fallback**: busy, rate-limited or missing models are skipped, and failing models rest for 10 minutes | Retrying one model | Free tiers often answer "503 busy" or hang. The next model usually answers in seconds |
| **Structured outputs** (the answer's JSON Schema), falling back to JSON mode, then plain text | Hoping the model follows the format | Fewer broken answers and fewer repair requests, with graceful fallback where a service refuses a schema |
| **Self-repair, limited to fixable rule codes** (at most 2 retries; stops if the same problems come back) | Asking the human to fix every AI mistake; unlimited retries | Most AI mistakes (an unknown claim id, a script too short, a 404 link) are mechanical. Python names the exact problem and the AI fixes it. Problems only a human may decide (originality, blocked pages) are never sent back |
| **Protection against costs and runaway use**: free models only (`allow_paid: false`); daily limits per service and in total; a budget per click (8 requests, 180 s); one run at a time; reuse of an identical earlier answer | Trusting the free tier to stay free | Linking billing or a loop of retries must never cost money or burn the daily quota. Added after real incidents: hanging models and a duplicated run |
| **Keys only in a git-ignored `.env`**, sent only in the Authorization header, removed from error text, never logged. Plus a **pre-commit secret guard** (`scripts/check_secrets.py`), also run in CI | Keys in config files; gitleaks or detect-secrets | The guard is under 100 lines with no dependency. It also catches the owner's actual `.env` values, whatever their format |
| **Every request in an AI log** (the `ai_calls` table: prompt, answer, problems) | No log | You can see what the AI was asked and why it was repaired. It also lets an identical question be answered from the log instead of a new request |
| **FakeLLM / FakeSend in tests**; tests never call a real AI | Recorded HTTP cassettes; live tests | Fast, free, deterministic, and keys never appear in test output |

## Research and sources

| Choice | Instead of | Why |
|---|---|---|
| The app **opens every cited link itself** (stdlib HTTP, behind a `Fetcher` interface) | Trusting the AI's citations; a headless browser (Playwright, Selenium) | AIs invent plausible URLs. A plain request detects 404s cheaply; pages that block robots are marked "blocked" for a human to vouch for. A browser would be a heavy install for little gain |
| **Evidence quotes**: the sentence on the page that best covers the claim's words, weighted by how rare each word is | Asking an AI to quote the page | Deterministic, and it can't invent a quote. The reviewer reads one sentence, not the whole page |
| The **originality gate** uses word-overlap similarity (stdlib `difflib` + token sets): duplicates, then topic + angle, then reused claims and sources | Embeddings (a model or a paid API) | Free, explainable ("these 4 words match"), with no model download. It only *suggests*; the human decides. An AI judge was left as a later option |
| **Declining a source** removes the claims that depend on it; a **blocked site** is never offered again | Editing claims by hand | Keeps the "every fact has a working source" rule true automatically |

## Media

| Choice | Instead of | Why |
|---|---|---|
| **FFmpeg / ffprobe** called directly (subprocess) | MoviePy; a video API | The industry standard, fast, and with exact control over the output (1080×1920, 30 fps, H.264 + AAC, fast start, burned-in ASS subtitles). MoviePy is slower and adds dependencies. Free via Scoop |
| **Pictures made by hand** in Adobe Firefly / ChatGPT, with the licence note stored on every file | Image-generation APIs; Bing Image Creator | APIs cost money. The tools' terms were checked on their official pages: Firefly and ChatGPT allow commercial use; Bing/Designer and the ElevenLabs free plan don't, so they were left out. `tools.yaml` asks for a re-check after 180 days |
| **Kokoro-82M** narration via `kokoro-onnx`, run locally | ElevenLabs; cloud TTS; Coqui; the official `kokoro` package | Apache-2.0 (commercial use allowed), free, offline, ~15 s for a 40 s narration on a laptop CPU. The official package didn't support Python 3.13; `kokoro-onnx` does |
| **Subtitles: text from the approved script, timing from `faster-whisper`** | Using the speech recogniser's transcript; timing by hand | A misheard word can never put an unapproved fact on screen. Whisper only tells *when* each word is spoken. Audio is decoded with FFmpeg because a PyAV update broke faster-whisper's own decoder |
| The **hook as text on screen** for the first 3 s, burned in from the first frame | No overlay; a separate editing tool | Many viewers scroll with the sound off, and the first frame is also the preview picture |
| **Rules on media use stored measurements** (duration and resolution from ffprobe), and a video records what it was `built_from` | Re-reading files, or comparing timestamps | An outdated video (new narration, a replaced picture) is detected exactly, in pure Python |

## Publishing and analytics

| Choice | Instead of | Why |
|---|---|---|
| **Manual upload**, then the links are recorded | Automatic posting; unofficial upload tools | Official APIs only, and a human always clicks Publish. YouTube makes uploads from unaudited apps private; TikTok and Instagram need app review. Unofficial tools risk the accounts |
| **Optional SCHEDULED stage**, with go-live tracked per platform | One "published" flag | Real uploads were scheduled on some platforms and live at once on others |
| **Per-platform post texts and rules** (length, visible characters, hashtag limits, AI-label steps) with dated sources | One text for all | Each platform cuts and counts differently (Instagram allows at most 5 hashtags) |
| **Numbers typed in at day 1, 7 and 30**, append-only, compared by medians within one platform, with a "too few videos" warning | Platform APIs; comparing raw totals | Free and works today. Same-age readings make videos comparable, and medians resist one viral outlier. The table is ready for an API importer later |

## Engineering and tooling

| Choice | Instead of | Why |
|---|---|---|
| A **project `.venv` installed from `requirements-lock.txt`** (a `pip freeze` after a full green test run) by `setup.ps1` | Global installs; Poetry; uv; conda | Library updates had broken the app twice (PyAV, httpcore). Plain venv + pip has the fewest new concepts for a beginner; uv would be the faster modern alternative |
| **pytest** + **pytest-xdist**; fakes behind small interfaces (Fetcher, Aligner, TTS, LLM); FFmpeg tests skip without FFmpeg | unittest; mocking libraries everywhere | 387 tests in about 1.5 minutes on all CPU cores, no network. Interfaces made the code testable without monkeypatching internals |
| **ruff** (lint + import order + modern-Python rules) and **mypy** with the Pydantic plugin | flake8 + isort + pyupgrade; no type checking | One fast tool replaces several. mypy catches mistakes the tests don't |
| **GitHub Actions on Windows** (secrets → ruff → mypy → pip-audit → tests) and **Dependabot** | No CI; Linux-only CI | Same OS as the owner's PC. Every push proves the project still installs and passes, and pip-audit flags known vulnerabilities |
| **No Docker, no servers, no background workers** | Containers, a task queue | A single-user desktop tool. Slow steps (narration, subtitles, build) show progress bars instead |
| **`run_app.bat`** launcher that starts Streamlit and opens the browser when it's ready | Telling the owner to use a terminal | Double-click to start; the log goes to `data/logs/run_app.log` |
| **Data in `data/`, git-ignored**, with a daily automatic backup (14 kept) and a backup command | Data in the repository; cloud storage | Your episodes, media and keys never reach GitHub. Restoring is a file copy |

## What was deliberately left out

- **Agent frameworks and MCP servers.** The workflow is a fixed pipeline with human gates; a
  plain loop is clearer and safer.
- **Paid APIs and automatic publishing.** The budget and platform rules; the provider
  interfaces leave room for them.
- **Embeddings or an AI judge for originality.** Word overlap was enough and explainable.
- **Background music and AI video clips.** Planned (milestones 6 and 7), not built before the
  project ended.
