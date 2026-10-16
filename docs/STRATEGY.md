# What kind of video to make (content strategy)

> **Outcome:** the project was archived soon after (October 2026): views stayed low. The
> lessons are summarised in the [README](../README.md#what-i-learned).

Written 2026-10-15, after the first three videos. The dumplings video did much better than the
microwave and ketchup ones. This page says what the research suggests, what the app now does
about it, and how to check whether it works for this channel.

**Read this with care:**
- It is too early to judge. There is one strong video and two weaker ones, and the ketchup
  video was not live everywhere yet.
- The platforms don't publish how their recommendations work. The sources below are
  marketing blogs and creator-tool companies that study many channels. Treat them as good
  hints, not facts.
- **Your own analytics decide.** The app can now compare videos by picture style, voice,
  length and posting time (Analytics → compare).

## What the three videos tell us

|                 | Dumplings (best)                    | Microwave                 | Ketchup               |
|-----------------|-------------------------------------|---------------------------|-----------------------|
| First line      | "Some dumplings were never meant to be eaten." (a bold, strange statement) | "What if your kitchen's favorite gadget started with a ruined snack?" (a generic "what if") | "Would you swallow ketchup...?" |
| Story           | little-known                        | very well known           | fairly little-known   |
| Length          | 43 s                                | 48 s                      | 44 s                  |
| Pictures        | realistic (channel_style_v1)        | storybook                 | storybook             |
| Voice           | Kokoro george (male)                | Kokoro heart (female)     | Kokoro heart          |
| Instagram post  | –                                   | 23:33                     | 00:10                 |

Several things changed at once, so we can't say which one mattered. The differences that
match the research best:
- the first line;
- how well known the story already was;
- late-night posting.

## What the research says

1. **The first 1–2 seconds decide.**
   - Shorts and Reels are shown to small test groups first.
   - If people swipe away right away, the video stops spreading.
   - A concrete, surprising first line works better than a generic opener ("What if…",
     "Did you know…").
   - Text on screen in the first frame helps people who scroll with the sound off.
   - Sources: [Socialchamp](https://www.socialchamp.com/blog/youtube-shorts-algorithm/),
     [Metricool](https://metricool.com/youtube-shorts-algorithm/),
     [Everpop](https://everpop.app/blog/youtube-shorts-algorithm-2026-what-actually-matters).
2. **Watched to the end, and replayed.**
   - Completion and rewatches matter most.
   - About 30–45 seconds suits all three platforms; 15–35 seconds is often advised for Shorts.
   - An ending that flows back into the start makes people watch again.
   - Sources: [Vidpal](https://vidpal.ai/blog/ideal-video-length-for-every-platform),
     [Virvid](https://virvid.ai/blog/best-shorts-length-retention-2026),
     [vidIQ on looping](https://vidiq.com/blog/post/double-youtube-watch-time-looping-shorts/),
     [Opus](https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention),
     [Humble&Brag benchmarks](https://humbleandbrag.com/blog/youtube-shorts-benchmarks).
3. **Food stories work when they surprise.**
   - Faceless food channels do best with curiosity: strange origins, myths that are wrong,
     food once used as medicine or money.
   - Stories everybody already knows do worse.
   - Sources: [FlowShorts](https://flowshorts.app/youtube-shorts-ideas/food),
     [BecomeViral](https://becomeviral.com/blog/faceless-youtube-food-niche-complete-guide),
     [vidIQ](https://vidiq.com/blog/post/youtube-channel-ideas-without-showing-your-face/),
     [Mashed: food origin myths](https://www.mashed.com/31587/food-origin-myths-everyone-believes-true/).
4. **AI content is fine when it has real value.**
   - YouTube's "inauthentic content" policy (renamed in July 2025) targets mass-produced,
     repetitive videos, not AI as such.
   - Checked facts with sources are exactly that value.
   - Uncanny AI faces and low-quality pictures cost trust.
   - Sources: [Exemplary](https://www.exemplary.ai/blog/youtube-inauthentic-content-policy),
     [LensPOV](https://lenspov.com/articles/youtube-ai-content-demonetization-2026),
     [Green Frog Labs](https://greenfroglabs.com/blog/ai-content-audience-says-hate-but-watches),
     [Meltwater](https://www.meltwater.com/en/blog/ai-slop-consumer-sentiment-social-listening-analysis).
5. **Picture style and voice: no solid evidence either way.**
   - No study compares illustrated and realistic pictures, or AI and human voices, for this
     kind of channel. Your analytics will tell.
   - Source on voices: [Outspoken Voices](https://outspokenvoices.com/blog/ai-voice-or-human-voice-over-how-smart-brands-are-actually-using-both-in-2026).
6. **When to post.**
   - Early evening in your viewers' time zone (about 18:00–21:00) is a common best time.
   - Very late at night, few people are scrolling.
   - On Instagram, reach beyond your followers depends on watch time and shares ("sends").
   - Sources: [Buffer](https://buffer.com/best-time-to-post),
     [SocialPilot](https://www.socialpilot.co/blog/instagram-reels-algorithm).

## What the app does now

- **Ideas.**
  - The topic scout favours little-known food stories: surprising origins, wrong myths,
    food as medicine or money, banned food, archaeological finds, wartime food, food
    science with a story.
  - It warns against famous stories.
  - Every idea gets a `familiarity` score (low / medium / high); aim for low.
  - "food" is the default category.
- **Length.**
  - New episodes aim for **35 seconds** (`target_duration_sec` in `settings.yaml`).
  - The allowed range stays 30–55 s.
- **The hook.**
  - One short sentence that names the food and states the surprising thing.
  - Warnings (they never block) when it is longer than 14 words (`hook_max_words`) or starts
    with a generic opener. Run with AI, the AI is asked to fix them.
- **The script.** Short sentences, a payoff that answers the hook, and a last line that loops
  back to the start. No "follow for more".
- **The pictures.**
  - The first shot is the hook picture: the food in close-up, with a calm top for the text.
  - The food is the hero.
  - No AI faces in close-up: people appear as hands, silhouettes or from behind.
  - New style **appetizing_v1**: a rich painting of the food, clearly not a photo.
  - The storybook style is one swap away in `settings.yaml`.
- **Hook on screen.** The first line is burned in as big yellow text at the top for the first
  3 seconds (`hook_overlay_sec`; 0 turns it off). It is there from the very first frame.
- **Titles.** A specific, surprising statement under ~60 characters that names the food.
- **Posting tip** on every platform: early evening, not late at night.
- **Analytics.**
  - Compare by picture style, voice, length and posting time.
  - The watch-time box accepts "0:26" the way the apps show it.

## How to find out what works for this channel

1. Change **one thing at a time** where you can. For example, keep the new style for three
   videos, then try storybook for three.
2. After 7 days, add each platform's numbers.
   - Most useful: average watch time, and YouTube's "stayed to watch" %.
   - Views come from those.
3. Analytics → compare by picture style, voice, length, hook style or posting time.
   - A difference only means something after 5+ videos per group.
   - Look at the median, not the single best video.
4. If something clearly wins, set it as the default in `settings.yaml`.
