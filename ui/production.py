"""Episode section for production: shot files, narration, subtitles, the video."""

from pathlib import Path

import streamlit as st

from studio.assets import (
    abs_path,
    add_narration,
    add_shot_asset,
    made_today,
    narration_text,
    suggest_tools,
)
from studio.errors import StudioError
from studio.models import (
    AssetKind,
    Status,
    latest_asset,
    shot_files,
    subtitles_are_current,
    video_is_current,
)
from studio.render import build_video, missing_for_build
from studio.subtitles import WhisperAligner, make_subtitles, read_cues, whisper_dir
from studio.tools import OTHER, Tool, Tools, load_tools
from studio.tts import SPEED_RANGE, VOICES, KokoroTTS, generate_narration, use_narration
from ui.core import (
    ACTOR,
    BUILT_IN_VOICE,
    act,
    length_mark,
    repo,
    settings,
    show_error,
    show_table,
    state,
)

IMAGE_TYPES = ["png", "jpg", "jpeg", "webp"]
CLIP_TYPES = ["mp4", "mov", "webm"]
AUDIO_TYPES = ["mp3", "wav", "m4a"]


def show_media(asset) -> None:
    """Picture / clip / sound player. A missing file shows a warning instead of breaking the
    whole page (files moved or deleted, or sample data without real files)."""
    path = abs_path(repo, asset)
    if not path.exists():
        st.warning(f"File missing: {asset.path}. Add it again, or restore it from a backup.")
        return
    if asset.kind == AssetKind.IMAGE:
        st.image(str(path), width="stretch")
    elif asset.kind == AssetKind.VOICE:
        st.audio(str(path))
    else:
        st.video(str(path))


def tools_or_none() -> Tools | None:
    try:
        return load_tools()
    except StudioError as e:
        show_error(e)
        return None


def tool_picker(tools: list[Tool], key: str, memory: str, default: str | None = None,
                used: dict[str, int] | None = None) -> Tool | None:
    """Pick the tool: shows its link, tip, daily budget and license note. `default` is the
    suggested tool (else the last one used). Outside the upload form so everything updates
    as soon as you pick."""
    names = [t.name for t in tools] + [OTHER]
    preferred = default or state.get(memory)
    choice = st.selectbox("Made with", names, key=key,
                          index=names.index(preferred) if preferred in names else 0)
    tool = Tools.find(tools, choice)
    if tool is None:
        return None
    if tool.daily_limit and used is not None:
        n = used.get(tool.name, 0)
        if n >= tool.daily_limit:
            st.warning(f"Today's {tool.daily_limit} {tool.name} pictures are used up. "
                       "Pick another tool, or do this shot tomorrow.")
        else:
            st.caption(f"{tool.name}: {n} of {tool.daily_limit} used today")
    if tool.url:
        st.link_button(f"Open {tool.name} ↗", tool.url)
    if tool.tip:
        st.caption(f"💡 {tool.tip}")
    st.caption(f"License note: {tool.license_note}")
    if tool.stale():
        st.warning(f"These terms were last checked on {tool.checked}. Re-check {tool.terms_url} "
                   "and update config/tools.yaml.")
    return tool


def _remember(memory: str, tool: Tool | None) -> None:
    """Pre-select the same tool next time."""
    state[memory] = tool.name if tool else OTHER


def provider_and_license(tool: Tool | None, key: str) -> tuple[str, str]:
    """Inside the form: the tool's note is used unless you write your own."""
    if tool is None:
        c1, c2 = st.columns(2)
        return (c1.text_input("Tool name", key=f"{key}_made"),
                c2.text_input("Source / license (required)", key=f"{key}_lic"))
    own = st.text_input("License note (leave empty to use the one above)", key=f"{key}_lic")
    return tool.name, own.strip() or tool.license_note


def shot_card(ep, shot, asset, tools: list[Tool], suggested: str | None = None,
              used: dict[str, int] | None = None) -> None:
    with st.container(border=True):
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"**{shot.shot_id}** · {', '.join(shot.segment_ids)} · "
                        f"{shot.duration_sec:g}s · {shot.purpose or '-'}")
            st.code(shot.generation_prompt, language=None, wrap_lines=True)
            tool = tool_picker(tools, f"tool_{ep.episode_id}_{shot.shot_id}", "last_image_tool",
                               default=suggested or (asset.provider if asset else None),
                               used=used)
            with st.form(f"up_{ep.episode_id}_{ep.version}_{shot.shot_id}", clear_on_submit=True):
                file = st.file_uploader("Picture or clip", type=IMAGE_TYPES + CLIP_TYPES,
                                        key=f"file_{ep.episode_id}_{shot.shot_id}")
                made, lic = provider_and_license(tool, f"up_{shot.shot_id}")
                if st.form_submit_button("Upload" if asset is None else "Replace"):
                    if file is None:
                        st.error("Choose a file first.")
                    else:
                        act(add_shot_asset, repo, ep.episode_id, shot.shot_id, file.getvalue(),
                            file.name, provider=made, license_note=lic, actor=ACTOR,
                            then=lambda _: _remember("last_image_tool", tool))
        with right:
            if asset is None:
                st.caption("No picture yet.")
            else:
                show_media(asset)
                st.caption(f"{asset.asset_id} · {asset.width}x{asset.height} · "
                           f"{asset.provider or '?'} · {asset.license_note}")


@st.cache_resource
def narrator(model_dir: str) -> KokoroTTS:
    """Kept loaded between clicks: only the first narration pays the loading time."""
    return KokoroTTS(Path(model_dir))


def generate_tab(ep) -> None:
    key = f"narr_{ep.episode_id}"
    c1, c2 = st.columns(2)
    voice = c1.selectbox("Voice", list(VOICES), format_func=lambda v: VOICES[v][1],
                         key=f"{key}_voice")
    speed = c2.slider("Speed", *SPEED_RANGE, value=1.0, step=0.05, key=f"{key}_speed",
                      help="Slower or faster speech changes the length.")
    st.caption(f"Expected length about {ep.script.total_estimated_sec / speed:.0f}s. Free, runs on "
               "this computer. The first time downloads the voice model (~350 MB, once).")
    if st.button("🗣️ Generate narration", key=f"{key}_go", type="primary"):
        bar = st.progress(0.0, text="Starting...")
        tts = narrator(str(settings.model_dir))
        tts.progress = lambda f, msg: bar.progress(f, text=msg)
        try:
            state[f"{key}_preview"] = generate_narration(repo, ep.episode_id, voice=voice,
                                                         speed=speed, tts=tts)
        except StudioError as e:
            show_error(e)
        finally:
            bar.empty()
    preview = state.get(f"{key}_preview")
    if preview is None:
        return
    st.audio(preview.wav, format="audio/wav")
    st.markdown(f"Preview ({VOICES[preview.voice][1]}, speed {preview.speed:g}): "
                f"{length_mark(preview.seconds)} · not saved yet")
    b1, b2 = st.columns(2)
    if b1.button("✅ Use this narration", key=f"{key}_use", type="primary"):
        act(use_narration, repo, preview, actor=ACTOR,
            then=lambda _: state.pop(f"{key}_preview", None))
    if b2.button("Discard", key=f"{key}_discard"):
        state.pop(f"{key}_preview", None)
        st.rerun()


def upload_tab(ep, voice_tools: list[Tool], current) -> None:
    tool = tool_picker(voice_tools, f"tool_{ep.episode_id}_voice", "last_voice_tool")
    with st.form(f"voice_{ep.episode_id}_{ep.version}", clear_on_submit=True):
        file = st.file_uploader("Narration audio", type=AUDIO_TYPES,
                                key=f"file_{ep.episode_id}_voice")
        made, lic = provider_and_license(tool, "voice")
        if st.form_submit_button("Upload narration" if current is None else "Replace narration"):
            if file is None:
                st.error("Choose a file first.")
            else:
                act(add_narration, repo, ep.episode_id, file.getvalue(), file.name,
                    license_note=lic, actor=ACTOR, provider=made,
                    then=lambda _: _remember("last_voice_tool", tool))


def narration_card(ep, voice_tools: list[Tool]) -> None:
    with st.container(border=True):
        st.markdown(f"**Narration** · script length {ep.script.total_estimated_sec:g}s "
                    f"(allowed {settings.min_duration_sec:g}-{settings.max_duration_sec:g}s)")
        st.code(narration_text(ep), language=None, wrap_lines=True)
        current = latest_asset(ep, AssetKind.VOICE)
        if current:
            show_media(current)
            st.markdown(f"Current narration: {length_mark(current.duration_sec)} · "
                        f"{current.license_note}")
        generate, upload = st.tabs(["🗣️ Generate here (free)", "⬆️ Upload a recording"])
        with generate:
            generate_tab(ep)
        with upload:
            upload_tab(ep, voice_tools, current)


def production_section(ep) -> None:
    """At PRODUCTION: a file for every shot, the narration, then build the video."""
    if ep.status != Status.PRODUCTION:
        return
    st.subheader("Production")
    st.caption("Paste each prompt into a free image tool, then upload the result. "
               "Check each tool's terms: monetized channels count as commercial use.")
    tools = tools_or_none()
    shots_part(ep, tools.image_tools if tools else [])
    narration_card(ep, [t for t in (tools.voice_tools if tools else [])
                        if t.name != BUILT_IN_VOICE])
    subtitles_card(ep)
    build_part(ep)


def picture_budget(ep, image_tools: list[Tool]) -> tuple[dict[str, int], dict]:
    """Today's pictures per tool, and a suggested tool for every empty shot."""
    used = made_today(repo)
    limited = [t for t in image_tools if t.daily_limit]
    if limited:
        st.caption("Today's pictures: " + " · ".join(
            f"{t.name} {used.get(t.name, 0)}/{t.daily_limit}" for t in limited)
            + " (a replaced picture no longer counts)")
    plan = suggest_tools(ep, image_tools, used)
    if plan and all(name is None for name in plan.values()):
        st.warning("Today's picture budget is used up. Continue tomorrow, or use your own pictures.")
    return used, plan


def shots_part(ep, image_tools: list[Tool]) -> None:
    """One folded card per shot; the first shot still missing a picture is open."""
    by_shot = shot_files(ep)
    shots = ep.storyboard.shots
    done = sum(1 for s in shots if s.shot_id in by_shot)
    st.progress(done / max(len(shots), 1),
                text=f"{done} of {len(shots)} shots have a picture or clip")
    used, plan = picture_budget(ep, image_tools)
    first_missing = next((s.shot_id for s in shots if s.shot_id not in by_shot), None)
    for shot in shots:
        asset = by_shot.get(shot.shot_id)
        hint = f" · suggested: {plan[shot.shot_id]}" if plan.get(shot.shot_id) else ""
        title = (f"{'✅' if asset else '⬜'} {shot.shot_id} · "
                 f"{shot.duration_sec:g}s · {shot.purpose or shot.action or ''}{hint}")
        with st.expander(title, expanded=shot.shot_id == first_missing):
            shot_card(ep, shot, asset, image_tools, suggested=plan.get(shot.shot_id), used=used)


def build_part(ep) -> None:
    missing = missing_for_build(ep)
    if missing:
        st.caption("Before building the video, add: " + ", ".join(missing) + ".")
    if st.button("🎬 Build video", key=f"build_{ep.episode_id}", type="primary",
                 disabled=bool(missing)):
        with st.spinner("FFmpeg is building the video..."):
            act(build_video, repo, ep.episode_id, actor=ACTOR)
    final_video_player(ep)


@st.cache_resource
def subtitle_timer(model_dir: str) -> WhisperAligner:
    """Kept loaded between clicks, like the narrator."""
    return WhisperAligner(Path(model_dir))


def subtitles_card(ep) -> None:
    """Time the approved script to the narration, word by word."""
    voice = latest_asset(ep, AssetKind.VOICE)
    captions = latest_asset(ep, AssetKind.CAPTIONS)
    with st.container(border=True):
        st.markdown("**Subtitles** · the approved script, timed word by word to the narration")
        if voice is None:
            st.caption("Add the narration first: subtitles are timed to it.")
            return
        if captions is not None:
            current = subtitles_are_current(ep)
            cues = read_cues(repo, captions) if abs_path(repo, captions).exists() else []
            st.markdown(("✅ Match the current narration" if current else
                         "⚠️ The narration changed: make the subtitles again")
                        + f" · {len(cues)} subtitles")
            with st.expander("Show the subtitles"):
                show_table([{"from": f"{c.start:.2f}s", "to": f"{c.end:.2f}s", "text": c.text}
                            for c in cues], "No subtitles.")
        label = "📝 Make subtitles" if captions is None else "📝 Make subtitles again"
        if st.button(label, key=f"subs_{ep.episode_id}",
                     type="primary" if captions is None else "secondary"):
            bar = st.progress(0.0, text="Starting...")
            timer = subtitle_timer(str(whisper_dir(settings)))
            timer.progress = lambda f, msg: bar.progress(f, text=msg)
            try:
                act(make_subtitles, repo, ep.episode_id, timer, actor=ACTOR)
            finally:
                bar.empty()


def final_video_player(ep) -> None:
    video = latest_asset(ep, AssetKind.FINAL_VIDEO)
    if video is None:
        return
    left, right = st.columns([1, 2])
    with left:
        show_media(video)
    with right:
        st.markdown(f"**{video.asset_id}** · {video.duration_sec}s · {video.width}x{video.height}")
        if not video_is_current(ep):
            st.warning("A shot or the narration changed after this video was built. "
                       "Build it again.")
