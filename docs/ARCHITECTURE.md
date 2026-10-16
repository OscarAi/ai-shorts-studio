# Architecture decisions (Milestones 1-5, free tools, analytics, AI, clean-up)

| Issue in original spec/schema | Decision |
|---|---|
| `creative` segment type missing from schema | Split into segment `role` and per-sentence `kind` (factual / creative / transition) |
| JSON files in status folders AND a database | SQLite is the only source of truth; JSON is an export |
| Hand-written JSON Schema drifts from code | Pydantic models are canonical; `python cli.py export-schema` generates the schema |
| `HIS_000001` ids embed a category | Neutral permanent ids: `EP_000001` |
| 30-55s hardcoded in schema | Limits live in `config/settings.yaml`, enforced by validators |
| Missing "technology" category | Added |
| Undefined backward transitions | Explicit transition table; backward moves and rejections require a reason |
| Single `analytics` object | Removed from the episode. Numbers live in an append-only `metrics` table (`MetricSnapshot`: one platform, one moment, the video's age in days), so the PUBLISHED lock is untouched; `studio/analytics.py` computes day-1/7/30 check-ins (age per platform from `Publication.live_at` / schedule / published_at), per-1k-view rates and medians per hook style / category / series within one platform. `source` is `manual` now; an API importer can fill the same table later |
| `total_estimated_sec` could disagree with segments | Derived property |
| No audit trail | Append-only `episode_versions` + `events` + `rejections` tables |
| "Agents edit only their section" unenforced | `save(allowed_sections=...)` rejects edits outside the allowed sections |
| Approved facts could be silently edited | Protected unless `allow_claim_changes=True` is passed explicitly |
| No cross-episode fact lookup | `claims` and `sources` index tables, rebuilt on every save |
| No multi-channel / series support | `channel_id` and `metadata.series` fields |
| No cost / license tracking | `Asset.cost_usd` and `Asset.license_note` |
| No AI-disclosure flag | `Publication.ai_disclosure` |
| "Originality" was undefined | Deterministic three-level gate (duplicate / topic+angle / claims+sources) that only suggests; you accept or reject with a note. Checks are stored with a fingerprint of topic+angle, so editing the idea makes the check stale. Enforced on IDEA -> RESEARCH and RESEARCH -> FACT_CHECK (a topic edited during research must be re-checked); topic and angle are locked after RESEARCH |
| "Agents verify facts" is unsafe | Agents only suggest. New claims are forced to needs_review, the fact checker writes notes, never statuses; claim and fact-check approval live in `review.py` and record the human actor |
| Agents tied to one AI vendor | Each agent is prompt + answer model + apply + mock; the answer text can come from a mock, a manual paste, or (later) an API. Prompts are editable files in `config/prompts/` |
| AI-cited links were trusted | `sources.py` opens every cited URL (standard library, behind a `Fetcher` interface so tests use `FakeFetcher`); cited sources must be usable before FACT_CHECK and only usable ones count for the 2-publisher rule; a human can vouch for robot-blocked pages, with a note |
| Fact review meant reading whole pages | Deterministic evidence quotes: the sentence that best covers the claim's words, weighted by rarity on the page; only short quotes are stored |
| Media files and the database could disagree | Files live in `data/assets/<EP>/`, are measured with ffprobe when added, and the numbers are stored on the `Asset`; rules use the stored numbers (pure Python). Replaced files stay on disk for history |
| "Is the video up to date?" by timestamp is fragile | A final video records `built_from` (the asset ids it used); QA requires it to equal the current shot files + narration exactly |
| Video assembly | FFmpeg (installed separately, found on PATH): per-shot 1080x1920 30 fps segments, shot lengths scaled to the narration, concat + AAC, fast-start. Each build is a new `final_vN.mp4` |
| Which free tools may a monetized channel use? | Checked on official terms pages and recorded in `config/tools.yaml` (link, license note, terms URL, date checked); the app fills the license note from the chosen tool and asks for a re-check after 180 days. Tools whose terms don't grant commercial use are left out |
| Scheduled uploads and later links | Optional SCHEDULED stage between APPROVED and PUBLISHED (its rule only applies when entering it, so direct publishing still works); while SCHEDULED only `publication` changes; PUBLISHED stays locked except `save(publication_only=True)` for adding links / post texts, recorded in history |
| One text for every platform | `studio/platforms.py` holds each platform's limits, visible length, hashtag rules and AI-label step (dated, with sources) and drafts a text per platform from approved content only; an optional copywriter prompt gets only approved facts |
| Pictures looked photographic / "AI" | A channel `visual_style` in settings is added to every new image prompt by the storyboarder (agents' `apply` now receives the settings); old episodes keep their look |
| One 1,100-line dashboard file; "what next?" logic mixed into widgets | `studio/guide.py` computes per-stage checklist, Continue target and blockers (pure, tested, shared with `cli.py next`); the dashboard is split into `ui/` modules per page/section; `ui/core.repo` forwards to the cached repository on every use because Streamlit keeps modules imported across reruns |
| Subtitles that can't show unapproved words | Subtitle text always comes from the approved script; speech recognition (faster-whisper, behind an `Aligner` interface, `FakeAligner` in tests) only supplies word timing, aligned by sequence matching; < 60% matched words is refused. Captions are an asset built from one narration; outdated subtitles block QA and the build |
| Narration without paid TTS | Kokoro-82M (Apache-2.0) run locally via kokoro-onnx behind a `TTSProvider` interface (`FakeTTS` in tests); model downloaded once with an exact size check; generation returns a preview, saving goes through the normal narration rules |
| Library updates broke the app (PyAV, httpcore) | Project `.venv` installed from `requirements-lock.txt` (a pip freeze after a full green run) by `setup.ps1`; `requirements.txt` keeps the short list of direct needs |
| Publishing without platform APIs yet | Human QA checklist, publication package with AI disclosure and sources in the description, manual upload, links recorded, then PUBLISHED locks the episode |
| Choosing topics needed inspiration | Topic scout (`ideas.py`) suggests ideas; each is screened by the same originality engine against the library and the other ideas before anything is saved |

| The same logic written in several places (2026-10-15 clean-up) | One home each: `timeutil` (UTC for storing, local "YYYY-MM-DD HH:MM" for people), `config.load_yaml_model` (every YAML file), `models.subtitles_are_current / video_is_current / shot_files`, `Episode.with_research / with_publication`, the repository's `_event / _rows`, review's publication helpers, `prompts.render`. Go-live state per platform lives in `platforms` |
| The checklist re-implemented the rules | `guide.py`: one function per stage in a `STEPS` table; ticks come from the rules' own issue codes, so the checklist and the Continue button can never disagree |
| Each CLI command's options lived 400 lines away | `@command(name, help, *opt(...))` next to each function; verified identical to the old parser (47 commands) |
| 29 hand-written "try / refresh / show error" blocks, three message systems | `ui.core.act()` (do it, refresh, or show why), `error_text()` (one format, with the rule code), `flash(kind, message, slot)` |
| A claim review wrote one version per claim | `review.set_claim_statuses`: the whole review is one version |
| AI answers drifting from the expected shape | Structured outputs: the answer's JSON Schema (from the same Pydantic model, simplified by `llm.answer_schema`) is sent to services that support it, with JSON mode and plain text as fallbacks; a temperature per agent; prompts share one structure with data in `<tags>` marked as never-instructions; `tests/test_prompts.py` keeps every prompt's JSON example valid |
| Slow tests (3.5 min) | pytest-xdist (`-n auto`) and the finished-video episode built once per test process: about 1.5 min |
| Bugs only a type checker sees | mypy with the Pydantic plugin, clean, run on every push |
| Long functions doing several jobs | The originality comparison is one function per level (duplicates, topic/angle, claims, sources) sharing a small `_Pair`; the dashboard's Production section is an outline of small parts |

Providers (AI, research, video, voice) arrive in later milestones behind interfaces.
