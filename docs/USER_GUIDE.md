# AI Shorts Studio: user guide

The full manual: setup, every stage of making an episode, the dashboard and the command line.
For an overview, see the [README](../README.md); for why it is built this way, see
[DECISIONS.md](DECISIONS.md).

No API keys are needed: you can use any free chat AI by copy and paste. Free AI keys make it
faster (see "AI inside the app").

## One-time setup (Windows)

1. Install **Python 3.13** from python.org. On the first installer screen tick **"Add python.exe to PATH"**.
2. Install **Git for Windows** (git-scm.com) and **VS Code** (code.visualstudio.com).
3. Install **FFmpeg** (`scoop install ffmpeg`), needed to build videos.
4. Open this folder in VS Code (File > Open Folder), then open a terminal (Terminal > New Terminal) and run:

```
powershell -ExecutionPolicy Bypass -File setup.ps1
```
This creates a private Python folder for this project (`.venv`) and installs the **exact library
versions that passed all tests** (`requirements-lock.txt`). Library updates elsewhere on your PC
can no longer break the app. In VS Code choose **Python: Select Interpreter → .venv** once.

Then start every command with `.venv\Scripts\python` (no "activate" needed), for example
`.venv\Scripts\python cli.py list`. In the examples below, `python` means `.venv\Scripts\python`.

**Updating libraries** is a deliberate step: `.venv\Scripts\python -m pip install -U <name>`, run the
tests, and only if all pass: `.venv\Scripts\python -m pip freeze > requirements-lock.txt`.
`requirements.txt` is the short list of what the app needs; the lock file is what you actually install.

## Try it

```
python cli.py init
python cli.py demo
python cli.py list
python cli.py show EP_000001
python cli.py history EP_000001
python cli.py advance EP_000001 APPROVED
python cli.py new --topic "The accidental invention of the microwave" --category inventions --series "Invented by Accident"
python cli.py originality EP_000003
python cli.py backup
```

The `demo` command builds a complete sample episode and walks it through every gate.
Try to cheat the rules and watch Python refuse: `python cli.py advance EP_000002 SCRIPT`.

## Originality gate

Before an idea may move from IDEA to RESEARCH, and again before RESEARCH moves on to FACT_CHECK,
its latest originality check must be up to date and passed (or accepted by you).
The check is plain Python, no AI. It looks at three levels:

1. **Duplicates:** near-identical titles, or reworded copies of a script.
2. **Topic and angle:** the same subject is fine if the story angle is clearly different.
   A short topic fully inside another one ("Penicillin" vs "Penicillin: the accidental mold...")
   is flagged for review; 3+ words fully inside suggests a block.
3. **Facts and sources:** reused claims and reused source links.

It gives one of three verdicts:

| Verdict | Meaning | What you do |
|---|---|---|
| `PASS` | Nothing similar found | Nothing; the gate is open |
| `REVIEW` | Something overlaps | Accept or reject it, with a note |
| `SUGGEST_BLOCK` | Probably a repeat | Accept or reject it, with a note |

The gate only suggests; **you make the final call**. If you edit the topic or story angle,
the check goes out of date and must be run again (the dashboard re-runs it for you). After
research the topic and angle are locked: a new topic means a new episode.

```
python cli.py originality EP_000003
python cli.py decide-originality 3 accept --note "different angle: the inventor's life"
```
(`3` is the check id printed by the first command.) Sensitivity is set by the `originality_*`
numbers in `config/settings.yaml`. Series boilerplate words listed in `originality_ignore_words`
(e.g. "accidental", "invention") are skipped, so "The accidental invention of velcro" is not a
duplicate of "...of the microwave". Run the check again after research (`--stage research`)
to catch reused facts.

## What kind of video works (content strategy)

The channel focuses on **little-known food stories**, about **35 seconds** long. Each one opens
with a short, surprising first line that names the food. That line also appears as big text on
screen for the first 3 seconds. Pictures make the food the hero, and the last line loops back
to the start. The research behind this, with sources, the caveats, and how to test it with your
own numbers: **[docs/STRATEGY.md](STRATEGY.md)**.

You can change all of it in `settings.yaml`:
- `default_category`
- `target_duration_sec`
- `hook_max_words`
- `hook_overlay_sec`
- `visual_style`

## Not sure what to make? Let an AI suggest topics

Dashboard > **New episode** > **Suggest ideas**: copy the prompt into any chat AI, paste its answer
back, and every idea is shown with its originality verdict (checked against your library and
against the other ideas). Click **Create** on the one you like: topic, angle and series are filled in.

```
python cli.py ideas --category food --theme "food once used as medicine" --out prompt.txt
python cli.py ideas-paste answer.txt              # list the ideas with their verdicts
python cli.py ideas-paste answer.txt --create 2   # turn idea 2 into an episode
```

## Making an episode: agents, mock and manual paste

Four agents fill the stages. Each one may only edit its own section:

| Stage | Agent | Fills in | Then YOU |
|---|---|---|---|
| RESEARCH | researcher | claims + sources | check them |
| FACT_CHECK | fact checker | a *suggestion* per claim | approve or reject each claim, then the fact check |
| SCRIPT | scriptwriter | hook + segments, using only approved claims | read it |
| STORYBOARD | storyboarder | shots with image/video prompts | read it |

**Manual paste (free, real content):** get the prompt, paste it into any chat AI (Claude, ChatGPT,
Gemini...), then paste the AI's answer back. Python checks the answer before saving it.

```
python cli.py prompt EP_000003 researcher --out prompt.txt
python cli.py paste  EP_000003 researcher answer.txt
python cli.py claim  EP_000003 C001 approve --note "checked both sources"
python cli.py factcheck EP_000003 approve
```

**Mock (free, fake content for testing):** `python cli.py mock EP_000003 researcher` fills one stage;
`python cli.py mock-pipeline EP_000003` fills every empty stage up to PRODUCTION. Mock text is
always marked `[MOCK]`. The mock pipeline only approves `[MOCK]` claims: real research always waits for you.

Rules Python enforces, whatever the AI writes: new claims always start as `needs_review`; the fact
checker can never approve a claim; the scriptwriter only sees approved claims; shots start as `planned`.
Prompt wording lives in `config/prompts/` and is safe to edit.

## AI inside the app (free)

Instead of copy-paste, the app can ask a free AI service itself: **🤖 Run with AI** in the Work
section (Research, Fact check, Script, Storyboard). The answer appears in the box with its
preview; **nothing is saved until you click Save**, and you still approve every fact.

**Set it up once (about 5 minutes):**
1. Go to https://aistudio.google.com/apikey, sign in, **Create API key**, copy it.
   Do not add a billing account (that changes the terms and can cost money).
2. Optional backup: a free key at https://console.groq.com/keys (and/or https://openrouter.ai/keys).
3. In the project folder, copy `.env.example` to `.env`, open it in VS Code and paste the key(s)
   after `GEMINI_API_KEY=` (and `GROQ_API_KEY=`). Save. `.env` never goes to GitHub.
4. Restart the app. On the **Health** page, click **Test connection** (or `python cli.py ai-test`).

**What the app does for you:**
- **Self-repair:** if the answer can't be read, or breaks a rule the AI can fix (a script
  sentence using an unapproved fact, wrong shot count or lengths, a claim without a source), it
  sends the exact problems back and asks again, up to 2 times (`max_repairs` in `config/ai.yaml`).
  Link checks and originality are never sent back: those are yours.
- **Backup services:** when one service is out of free requests or busy, the next one in
  `config/ai.yaml` is tried (Gemini → Groq → OpenRouter), model by model.
- **AI log:** every request (prompt, answer, what was sent back) is kept: Episode → Details →
  **AI log**. The version history says which service wrote what ("AI Gemini …, 2 tries").

| Service | Free | Watch out (checked 2026-10-14) |
|---|---|---|
| Google Gemini | Free key, no card; limits shown in AI Studio | Free tier: Google may use prompts and answers to improve its products and people may review them. Public facts and scripts only, never private information. |
| Groq | Free key, no card; daily limits per model | Says it does not train on your prompts; check its terms for commercial use |
| OpenRouter | Free models, about 50 requests/day | Free models change often |

About 6-10 requests per video (including repairs), so the free tiers cover a video or two a day.

**Protection against costs and leaks** (all in `config/ai.yaml`, on by default):
- **Daily limits:** the app counts every request it sends (also failed ones and connection tests)
  and stops before sending when a service's `daily_limit` (Gemini 100, Groq 150, OpenRouter 40)
  or the total `daily_limit_total` (150) is reached. The Work section shows "AI requests today".
- **Free models only:** `allow_paid: false` refuses paid models (OpenRouter bills non-`:free`
  models once you add credit). Never link a billing account to your Google AI Studio key.
- **Answer length ceiling** (`max_output_tokens`), so a runaway answer can't get long.
- **Keys:** only in `.env` (git-ignored), never shown, logged or put in the AI log. A **secret
  guard** (installed by `setup.ps1`, also run on GitHub) blocks any commit that contains a key,
  a value from `.env`, or `.env` itself. The Health page checks all of this.

**Efficiency** (fewer requests for the same result):
- Python does the maths the AI is bad at: the script gets a word budget, the storyboard gets the
  exact shot plan per segment, and shot lengths that are a little off are fitted by Python
  instead of costing a repair.
- Prompts end with a short self-check, so most answers are right the first time.
- **Invented links are caught:** AIs often make up plausible web addresses. During Run with AI,
  Python opens every link the researcher gives; pages that don't exist go straight back to the
  AI to replace, before you see the answer.
- **Busy models are skipped:** a model that failed in the last 10 minutes (busy, timed out) is
  tried last, so you don't wait for it on every click (the free Gemini models are often busy).
- Repairs stop when the same problems come back; a "too many requests this minute" answer is
  waited out once (up to 20 s) instead of switching to a weaker model.
- Asking the exact same question again reuses the earlier good answer (no request);
  **🔁 New AI answer** (or `--fresh`) asks again on purpose.

```
python cli.py ai-test                 # which keys work, and the models they can use
python cli.py ai EP_000005            # run this stage's agent with AI and show the result
python cli.py ai EP_000005 --save     # ... and keep it
```

## Checking sources

AIs sometimes invent links. Before RESEARCH can move to FACT_CHECK, Python opens every cited link:

| Status | Meaning | What you do |
|---|---|---|
| ✅ ok | The page loaded | Nothing |
| ❌ broken | 404 or the site doesn't exist | Replace or remove the source |
| 🔒 blocked | The site refuses robots (403, "are you human?" page) | Open it yourself, then mark it as checked |
| ⚠️ error | Timeout, or the secure connection could not be verified | Try again, or open it and mark it |

Only working (or human-checked) sources count toward the "2 different publishers" rule. For each
claim, Python also finds the sentence on each page that best matches it, so in **Review claims**
you read one quote instead of searching the whole page. Only short quotes are stored.

```
python cli.py verify EP_000003
python cli.py source-ok EP_000003 S001 --note "opened it, the paper is there"
```

### Declining a source you don't want

In **Sources** (Research and Fact check), open **🚫 Decline a source**, pick it, say why, and
confirm. Before anything changes, you see exactly what will happen:
- The source stays listed under **Declined sources** with your reason, but is never used again.
- A claim that relied **only** on it is rejected (it can't reach the script or the description).
- A claim that has other sources just loses this one. If you had approved it, it goes back to
  review, because its support changed. The "2 different publishers" rule still applies.
- An approved fact check is withdrawn, so you approve the facts again.

Tick **Never use this site again** to block the whole website (e.g. `example.com`, including
`news.example.com`) for every future episode: the researcher's prompt says not to use it, and any
source from it in an answer is declined automatically. A re-run of the researcher can't bring
back a page you declined. Blocked sites are listed (and can be unblocked) under **Blocked sites**.
After the script is written, go back to Fact check first (Other moves).

```
python cli.py decline EP_000004 S006 --reason "page does not load" [--block-site]
python cli.py blocked-sites
python cli.py block-site example.com --reason "tabloid"
python cli.py unblock-site example.com
```

## Producing and publishing the video

Needs **FFmpeg** (free). Check with `ffmpeg -version`; if missing: `scoop install ffmpeg`
or `winget install ffmpeg`, then open a new terminal.

1. **PRODUCTION**: each shot shows its image prompt and an **Open Adobe Firefly / ChatGPT** button.
   Paste the prompt there, download the picture, upload it: the license note is filled in from
   the tool you picked. For the narration, click **Generate here** (free, on this computer) or
   upload a recording. It must be 30-55 seconds.
2. **Make subtitles**: the approved script is timed word by word to the narration (speech
   recognition only finds *when* each word is said; the text is always your approved script).
   Short chunks of up to 3 words, white with a black outline, above the platform buttons.
   New narration means new subtitles.
3. **Build video**: FFmpeg makes a 1080x1920 MP4 whose pictures end exactly when the narration
   ends (gentle zoom on pictures), with the subtitles burned in. Change a shot, the narration or
   the subtitles later and you must build again.
4. **QA**: watch the video with sound and tick every box (narration matches, no wrong facts,
   no garbled text or deformed hands, no real living person, licenses OK).
5. **HUMAN_REVIEW → APPROVED**, then **Publish**: title, hashtags, platforms and one ready-to-paste
   **text per platform** (see below). Download the MP4 (and the `.srt` for YouTube's captions),
   upload it yourself, turn on the platform's AI-content label, then record it:
   - **📅 Scheduled**: per platform its link, and either a date and time or **Live now** (for
     an app without scheduling). Nothing live yet → SCHEDULED; when it goes live, click
     **✅ It's live** → PUBLISHED. Something live already → PUBLISHED straight away.
   - **✅ All published now**: links → PUBLISHED.
   The video counts as published as soon as it is live on **any** platform; the others keep
   their times and show "📅 scheduled, goes live …" until then (and get analytics after that).
   A published episode is locked, except **Add a link**: post elsewhere later and add the link
   with when it went live (a later time is recorded as that platform's schedule). Adding a live
   link while SCHEDULED publishes the episode.

```
python cli.py tools                                   # the free tools and their license notes
python cli.py asset EP_000003 SHOT01 shot1.png --tool "Adobe Firefly"
python cli.py narrate EP_000003 --voice heart --speed 1.0   # or: narration voice.mp3 --tool "My own voice"
python cli.py subtitles EP_000003                     # first time downloads ~145 MB, once
python cli.py render EP_000003
python cli.py qa EP_000003 approve --all-checked --note "watched twice"
python cli.py publish-info EP_000003 --title "..." --hashtags "#history #food" --platform youtube_shorts --platform tiktok
python cli.py post EP_000003 --platform tiktok        # the TikTok text + checks + tips
python cli.py schedule EP_000003 --platform youtube_shorts --at "2026-10-14 18:00" --url https://youtube.com/shorts/...
python cli.py live EP_000003                          # it went live: scheduled -> published
python cli.py published EP_000003 --url https://...   # or: published straight away
python cli.py add-link EP_000003 --url https://www.tiktok.com/@you/video/... --posted "2026-10-14 18:00"
```
Files live in `data/assets/<episode>/`. `python cli.py backup` copies them too.

## Post texts per platform (checked 2026-10-13)

Each platform gets its own text, pre-filled from your approved content (hook first, the angle,
"Sources: …", an AI note, hashtags). Edit and save it per platform; the app counts characters and
warns you. **"Make it catchier"** gives you a prompt for any chat AI; it contains only your
approved facts and asks the AI to add none.

| Platform | Text limit | Shows before "more" | Hashtags | AI label |
|---|---|---|---|---|
| YouTube Shorts | title 100, description 5,000 | (title) | 3-5; the first 3 show above the title | Details → "AI use": required for *realistic* AI, optional for stylized |
| TikTok | 2,200 | ~100 characters | 3-5 work best | "AI-generated content" toggle: required for realistic AI |
| Instagram Reels | 2,200 | ~125 characters | **at most 5** (since Dec 2025) | "AI info" label |

Platforms change these numbers; they live in `studio/platforms.py` with their sources.

## Tracking performance (views, likes, shares...)

Read each video's numbers at about **day 1, 7 and 30** after it went live and type them in under
**📈 Analytics** (or the **Performance** box of a published episode). "Numbers to collect today"
tells you which ones are due and links to the video. Each platform gets its own boxes, in its own
words:

| Platform | Where | Numbers |
|---|---|---|
| YouTube Shorts | YouTube Studio → Content → Shorts → the video → Analytics ("Advanced mode" for engaged views and shares) | views, engaged views, likes, comments, shares, average view duration, stayed to watch %, subscribers gained |
| TikTok | TikTok Studio, or the video → ⋯ → Analytics | views, likes, comments, shares, saves, average watch time, watched full video %, new followers |
| Instagram Reels | the reel → View insights (professional account) | views, likes, comments, shares, saves, average watch time, follows |

Leave a box empty when you don't know it. Why it works this way (as YouTube Studio, Metricool and
similar tools do):
- **Same age, fair comparison.** Shorts get most views in the first days, so a 2-day-old video
  can't be compared with a 30-day-old one. The age counts from when each platform went live
  (the time you give when adding its link, or its schedule).
- **Rates, not only counts.** Likes and shares per 1,000 views, and how many stayed, show what
  people liked, not only how many saw it. Shares and "stayed to watch" are the strongest signals.
- **One platform at a time.** Since 31 Mar 2025 YouTube counts every play of a Short as a view;
  the old count is now "engaged views". So views aren't comparable across platforms.
- **Compare** groups your videos by hook style, category, series, picture style, voice, length
  or posting time (median numbers, with how many videos each group has). Under 3 videos per
  group the difference is mostly luck. Give new ideas a **hook style** (e.g. shocking fact,
  mystery) to learn which openings work.
- **Watch time** is typed the way the apps show it: `0:26` (or `26`). A reading under one second
  gets a warning, because "0:26" typed as 0.26 is an easy slip.
- Numbers are only ever added (a typo is fixed by adding a corrected reading), are kept apart
  from the locked episode, and are in your backups. **Download CSV** for a spreadsheet.

```
python cli.py stats EP_000003 --platform youtube_shorts --views 1200 --engaged-views 640 --likes 80 --shares 6 --stayed 71 --avg-view-sec 0:26
python cli.py stats-due              # what to collect today
python cli.py stats-show EP_000003
```

## Pictures per video and your daily budget

About **12 pictures per video** (one every ~3.5 s) keeps a Short lively: `shots_target: 12` and
`shots_max: 12` in `settings.yaml`. The storyboarder plans 12 shots; more than 12 is blocked.
`tools.yaml` holds each tool's `daily_limit` (now 6 Adobe Firefly + 6 ChatGPT). The Production
section shows "Today's pictures: Adobe Firefly 3/6 · ChatGPT 0/6" and suggests a tool for every
empty shot (the first 6 Firefly, then ChatGPT, adjusted to what's left today).

## Picture style

Every image prompt of a new storyboard gets the channel style from `settings.yaml`
(`visual_style`), now **appetizing_v1**: a rich painting of the food in sharp close-up, glistening
textures, warm side light, clearly a painting and not a photo, people only as hands or silhouettes
(why: [docs/STRATEGY.md](STRATEGY.md)). The earlier storybook style is one swap away (it is
written in `settings.yaml`). If an AI's storyboard forgets the style, the app adds it.
Change the sentence to restyle future videos; existing episodes keep their look, and
Analytics → compare by "Picture style" shows which look your viewers stay for.

## Free tools and licences (checked 2026-10-12)

A monetized channel is commercial use. These were checked against each tool's official terms:

| Tool | For | Commercial use | Notes |
|---|---|---|---|
| [Adobe Firefly](https://firefly.adobe.com/) | Images | ✅ Output is your content ([terms, 23 Apr 2026](https://www.adobe.com/go/adobe-gen-ai-addl-terms)) | Limited free credits. Don't remove watermarks / Content Credentials |
| [ChatGPT](https://chatgpt.com/) | Images | ✅ "You own the Output" ([terms](https://openai.com/policies/terms-of-use/)) | Limited free images per day |
| Kokoro-82M (built in) | Narration | ✅ Apache-2.0 ([model card](https://huggingface.co/hexgrad/Kokoro-82M)) | Runs on this computer; model downloaded once (~350 MB) |
| [Kokoro web demo](https://huggingface.co/spaces/hexgrad/Kokoro-TTS) | Narration | ✅ Same Apache-2.0 model | No install needed |
| faster-whisper base.en (built in) | Subtitle timing | ✅ MIT | Runs on this computer; model downloaded once (~145 MB). Text always comes from your script |
| Bing Image Creator / Microsoft Designer | Images | ⚠️ Not granted ("for entertainment purposes") | Not offered in the app |
| ElevenLabs (free plan) | Narration | ❌ Commercial licence only on paid plans | Not offered in the app |

The list and each license note live in `config/tools.yaml`. Terms change: the app asks you to
re-check a tool 180 days after its terms were checked. Note that purely AI-made images and voices
may not be protected by copyright, so others could reuse them.

## Dashboard

**Double-click `run_app.bat` in File Explorer** (in VS Code a click only opens the file), or type
`.\run_app.bat` in the VS Code terminal. A black window opens and runs the app; keep it open while
you work and close it to stop the app. It is the same as:
```
.venv\Scripts\python -m streamlit run dashboard.py
```
Plain `streamlit run dashboard.py` uses your PC's global Python instead of the tested versions;
the Health page then warns "not the project's .venv".
Your browser opens at http://localhost:8501. Stop it with Ctrl+C in the terminal.

- **Pipeline**: how many episodes are at each stage, and a list with **what's next** for each
  one. Click a row to open it. The sidebar also lists your 5 most recent episodes.
- **Episode**: at the top, the stage path (✅ Idea → ✅ Research → 🔵 **Fact check** → …) and a
  **"What to do now"** box: a ticked checklist for this stage and one **Continue →** button
  (with "Why can't I continue yet?" when something is missing). Below it, only the work for this
  stage: originality and **Edit idea**; **Work** (copy the prompt, paste the answer, preview,
  save; or a mock; or edit as JSON); **Sources**; **Review claims** with evidence quotes;
  **Production** (finished shots fold away, narration, subtitles, build); **QA**; **Publish**.
  Going back, rejecting and reviving are under **Other moves**; numbers and full history are
  under **Details** at the bottom.
- **New episode**: let an AI suggest ideas, or write your own. You go straight to the new episode.
- **Health**: is everything installed, backed up and up to date (with fixes).
- **Analytics**: numbers to collect today, add a video's numbers, all videos side by side, compare
  by hook style / category / series, download CSV.

`python cli.py next EP_000003` prints the same "what to do now" checklist in the terminal.

The dashboard uses the same rules as the CLI. Anything the CLI refuses, the dashboard refuses too.

## Health check

```
.venv\Scripts\python cli.py doctor
```
(or the **Health** page in the dashboard) checks: the project's Python and exact library versions,
FFmpeg, the narration and subtitle models, database integrity, how old your newest backup is, free
disk space, tool licences due for a re-check, and whether your code is on GitHub. Each problem
comes with the fix.

## Run the tests

```
.venv\Scripts\python -m pytest -n auto
```
All tests should pass (about 1.5 minutes; `-n auto` uses every CPU core and needs the developer
tools below; plain `pytest` works too, it just takes about twice as long). If any fail, copy the
output and send it to Claude.

**Developer tools** (`.venv\Scripts\python -m pip install -r requirements-dev.txt`):

| Check | Command | What it catches |
|---|---|---|
| Code checks | `.venv\Scripts\python -m ruff check .` | likely bugs, unused code, simpler idioms |
| Type checks | `.venv\Scripts\python -m mypy studio` | a value that may be missing used as if it is always there |
| Secrets | `.venv\Scripts\python scripts/check_secrets.py --all` | an AI key or `.env` about to be published |
| Vulnerabilities | `.venv\Scripts\python -m pip_audit -r requirements-lock.txt` | known security problems in the libraries |

**On GitHub, automatically:** every push runs all of these and the tests on Windows (the
Actions tab shows ✅ or ❌). Once a month Dependabot proposes library updates as a pull request;
merge it only when its tests are green.

## Backing up and restoring

- **Code → GitHub (private repo).** After each milestone: `git push`. The first push opens a browser
  window to log in to GitHub (Git Credential Manager remembers it).
- **Your episodes and media are NOT on GitHub, on purpose.** They are private and large.
  `.gitignore` keeps `.env`, the database, media, models and logs out of Git. Check with
  `git status` before committing: you should never see `.env` or a `.db` file listed.
- **Automatic daily copy of the database**: the first time the app or a command runs each day, it
  copies the database to `data/backups/auto/` and keeps the newest 14 (`auto_backup_keep` in
  `settings.yaml`). Media are not included (too big for daily copies), so also use the button below.
- **Database + media → Backup** (sidebar button or `python cli.py backup`) copies them into
  `data/backups/` (`studio_<date>.db` plus a `studio_<date>_assets` folder). Copy that folder to OneDrive or a USB drive now and then. It is on the same disk
  as the original, so on its own it does not protect against a broken disk.
- **New PC:** `git clone <your repo url>`, run `setup.ps1`, then copy a backup into `data/`: the
  `.db` file renamed to `studio.db`, and the `_assets` folder renamed to `assets`. The narration and
  subtitle models download again by themselves on first use.

## Where things are

| Path | What it is |
|---|---|
| `cli.py` | The commands you type |
| `dashboard.py` | The Streamlit dashboard: page setup, sidebar, navigation |
| `ui/` | The dashboard's pages and Episode-page sections (one file each) |
| `studio/guide.py` | "What to do now" per episode, one checklist per stage, ticked from the rules |
| `config/settings.yaml` | Duration limits, source rules (safe to edit) |
| `studio/models.py` | The shape of an episode (single source of truth) |
| `studio/state_machine.py` | Which status changes are legal |
| `studio/validators.py` | Deterministic rules (no AI) |
| `studio/db.py` | SQLite storage, versions, audit log, originality checks, backup |
| `studio/originality.py` | The originality gate (verdicts and matches) |
| `studio/similarity.py` | Text-matching helpers used by the gate |
| `studio/agents.py` | The four agents: answer shapes, how answers are merged, mocks |
| `studio/prompts.py` | Loads and fills the prompt templates (one shape for all of them) |
| `studio/timeutil.py` | Times in one place: stored UTC vs the local times you type |
| `studio/review.py` | Human-only steps: approving claims and the fact check |
| `studio/sources.py` | Opens cited links, finds evidence quotes |
| `studio/declines.py` | Declining sources and blocked sites |
| `studio/llm.py` | Talking to the free AI services (Gemini, Groq, OpenRouter) |
| `studio/ai_runner.py` | Run with AI: self-repair and the AI log |
| `config/ai.yaml` | The AI services, their order, terms and models (safe to edit) |
| `studio/ideas.py` | Topic suggestions, screened for originality |
| `studio/assets.py` | Uploaded pictures, clips and narration (measured with ffprobe) |
| `studio/render.py` | Builds the final vertical MP4 with FFmpeg |
| `studio/tts.py` | Built-in narration with Kokoro (free, local) |
| `studio/subtitles.py` | Word-timed subtitles from the approved script (.srt + burned-in .ass) |
| `studio/platforms.py` | Per-platform limits, tips and post-text drafts |
| `studio/copywriter.py` | Optional AI "make it catchier" (approved facts only) |
| `studio/analytics.py` | Performance numbers: check-ins, rates, comparisons, CSV |
| `studio/health.py` | Health check (`cli.py doctor`, Health page) |
| `.github/` | Automatic tests on GitHub (CI) and monthly update proposals (Dependabot) |
| `pyproject.toml` | Settings for the code checks (ruff), type checks (mypy) and tests (pytest) |
| `scripts/` | The app launcher's helper, and the secret guard run before every commit |
| `config/tools.yaml` | Free image/voice tools, links and license notes (safe to edit) |
| `config/prompts/` | Prompt wording for each agent and the topic scout (safe to edit) |
| `studio/samples.py` | Demo content for tests |
| `studio/dashboard_data.py` | What the dashboard shows (plain Python, tested) |
| `tests/` | Automated tests |
| `data/` | Your database, logs, backups (git-ignored) |

## Key ideas

- **SQLite is the source of truth.** Every save adds a new version; nothing is overwritten.
- **Python owns the rules.** Status changes, source requirements and claim approvals are checked in code.
- **Facts are traced per sentence.** Each sentence is `factual` (must cite approved claim IDs), `creative`, or `transition`.
- **Approved claims are protected** from silent edits or deletion.
- **Published episodes are locked.** Analytics will live in a separate table (Milestone 8).
- **Duration limits and originality thresholds live in `settings.yaml`**, not in the code.
- **Originality is checked before research.** Python suggests; you decide, and your note is kept.
- **Every idea needs a story angle before research**: the specific twist this video tells
  (`min_story_angle_chars` in `settings.yaml`). Episodes already past IDEA are not affected.
- **AI suggests, you approve.** No agent can approve a fact; every approval is saved with who made it.
- **Links are checked by Python, not trusted.** A dead link never counts as a source.
- **Every file says where it came from.** No license note, no QA. A video built from old files
  is refused. You click "published" yourself; then the episode is locked.

## Not built yet

Background music (6), AI video clips behind a provider interface (7), automatic uploading and
automatic numbers through the platforms' official APIs (8), the optional AI judge for the
originality gate.
