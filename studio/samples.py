"""Demo/test content: a complete, valid episode body about penicillin.

FOR TESTING ONLY. The claims are real-world plausible but you must verify any claim and
source yourself before using it in a real video - that is exactly what the pipeline is for.
"""
from .models import (
    Asset,
    AssetKind,
    Claim,
    ClaimStatus,
    ContentKind,
    FactCheck,
    Importance,
    Publication,
    QAResult,
    Reliability,
    Research,
    Script,
    Segment,
    SegmentRole,
    Sentence,
    Shot,
    ShotStatus,
    Source,
    SourceType,
    Storyboard,
)

# Sample sources are "vouched for" by the demo so the demo can pass the link check.
# Honest label: nobody actually opened these pages for this record.
DEMO_NOTE = "sample data, not actually checked"


def sample_sections() -> dict:
    """Return a dict of sections you can apply with `episode.model_copy(update=...)`."""
    research = Research(
        sources=[
            Source(source_id="S001", title="Sir Alexander Fleming - Nobel Lecture",
                   publisher="NobelPrize.org", url="https://www.nobelprize.org/prizes/medicine/1945/fleming/lecture/",
                   accessed_date="2026-10-11", source_type=SourceType.PRIMARY,
                   reliability=Reliability.HIGH,
                   evidence_summary="Fleming describes the mold contaminating a culture plate and the bacteria dying nearby.",
                   verified_by="demo", verified_note=DEMO_NOTE),
            Source(source_id="S002", title="Discovery and Development of Penicillin",
                   publisher="American Chemical Society", url="https://www.acs.org/education/whatischemistry/landmarks/flemingpenicillin.html",
                   accessed_date="2026-10-11", source_type=SourceType.UNIVERSITY,
                   reliability=Reliability.HIGH,
                   evidence_summary="Landmark summary of the 1928 observation and later development.",
                   verified_by="demo", verified_note=DEMO_NOTE),
        ],
        claims=[
            Claim(claim_id="C001", importance=Importance.HIGH, status=ClaimStatus.APPROVED,
                  statement="In 1928, Alexander Fleming noticed that mold contaminating a bacteria culture plate had killed the bacteria around it.",
                  source_ids=["S001", "S002"]),
            Claim(claim_id="C002", importance=Importance.MEDIUM, status=ClaimStatus.APPROVED,
                  statement="The mold was from the Penicillium genus.",
                  source_ids=["S002"]),
            Claim(claim_id="C003", importance=Importance.MEDIUM, status=ClaimStatus.APPROVED,
                  statement="Fleming shared the 1945 Nobel Prize in Physiology or Medicine with Howard Florey and Ernst Chain.",
                  source_ids=["S001"]),
        ],
    )

    def seg(sid, role, dur, sentences, intent, emotion):
        return Segment(segment_id=sid, role=role, duration_sec=dur, sentences=sentences,
                       visual_intent=intent, emotion=emotion)

    F, C = ContentKind.FACTUAL, ContentKind.CREATIVE
    script = Script(
        hook="What if one of medicine's biggest breakthroughs started with a messy lab?",
        segments=[
            seg("SEG01", SegmentRole.HOOK, 4,
                [Sentence(text="What if one of medicine's biggest breakthroughs started with a messy lab?", kind=C)],
                "Cluttered 1920s laboratory bench", "curiosity"),
            seg("SEG02", SegmentRole.SETUP, 10,
                [Sentence(text="In 1928, Alexander Fleming noticed mold on one of his bacteria plates.", kind=F, claim_ids=["C001"]),
                 Sentence(text="Most people would have thrown it away.", kind=C)],
                "Scientist inspecting a petri dish", "intrigue"),
            seg("SEG03", SegmentRole.DEVELOPMENT, 12,
                [Sentence(text="Around that mold, the bacteria had died.", kind=F, claim_ids=["C001"]),
                 Sentence(text="The mold belonged to the Penicillium family.", kind=F, claim_ids=["C002"])],
                "Close-up of a clear ring around mold", "surprise"),
            seg("SEG04", SegmentRole.PAYOFF, 9,
                [Sentence(text="In 1945, Fleming shared a Nobel Prize with Howard Florey and Ernst Chain.", kind=F, claim_ids=["C003"])],
                "Nobel ceremony, period-accurate", "awe"),
            seg("SEG05", SegmentRole.ENDING, 5,
                [Sentence(text="Sometimes the best discoveries are the ones you almost throw away.", kind=C)],
                "Petri dish glowing in warm light", "reflection"),
        ],
    )

    shots = [
        Shot(shot_id=f"SHOT0{i}", duration_sec=d, segment_ids=[sid], purpose=p,
             location="1920s London laboratory", characters=["Fleming"],
             action=a, camera="slow push-in, 35mm feel", continuity_keys=["fleming", "lab_1928"],
             generation_prompt=f"Cinematic 1920s laboratory, {a}, warm tungsten light, vertical 9:16, no text.",
             asset_path=f"assets/images/shot0{i}.png", status=ShotStatus.GENERATED)
        for i, (d, sid, p, a) in enumerate([
            (4, "SEG01", "hook", "a cluttered bench of glassware"),
            (10, "SEG02", "setup", "a scientist studies a petri dish"),
            (12, "SEG03", "development", "macro shot of mold with a clear ring"),
            (9, "SEG04", "payoff", "a formal ceremony hall"),
            (5, "SEG05", "ending", "a petri dish glowing softly"),
        ], start=1)
    ]

    # Asset RECORDS only (no real files): enough for the rules to walk the sample to the end.
    sample_note = "sample data, no real file"
    assets = [Asset(asset_id=f"A00{i}", kind=AssetKind.IMAGE, path=shot.asset_path,
                    shot_id=shot.shot_id, provider="sample", license_note=sample_note,
                    width=1080, height=1920)
              for i, shot in enumerate(shots, start=1)]
    assets.append(Asset(asset_id="A006", kind=AssetKind.VOICE, path="assets/sample/voice.mp3",
                        provider="sample", license_note=sample_note, duration_sec=40.0))
    assets.append(Asset(asset_id="A008", kind=AssetKind.CAPTIONS, path="assets/sample/A008.srt",
                        provider="sample", license_note=sample_note, built_from=["A006"]))
    assets.append(Asset(asset_id="A007", kind=AssetKind.FINAL_VIDEO,
                        path="assets/sample/final_v1.mp4", provider="ffmpeg",
                        license_note=sample_note, duration_sec=40.0, width=1080, height=1920,
                        built_from=[a.asset_id for a in assets]))

    return {
        "research": research,
        "fact_check": FactCheck(approved=True),
        "script": script,
        "storyboard": Storyboard(shots=shots),
        "assets": assets,
        "qa": QAResult(approved=True),
        "publication": Publication(
            title="The Mistake That Saved Millions", platforms=["youtube_shorts"],
            hashtags=["#history", "#science"], urls=["https://example.org/mock/published"]),
    }
