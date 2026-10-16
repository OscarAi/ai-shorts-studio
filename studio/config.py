"""Settings: loaded from config/settings.yaml, validated by Pydantic."""
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import StudioError
from .models import Category

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in settings.yaml raises an error

    data_dir: Path = Path("data")
    default_channel_id: str = "main"
    log_level: str = "INFO"
    min_duration_sec: float = Field(30, gt=0)
    max_duration_sec: float = Field(55, gt=0)
    duration_tolerance_sec: float = Field(3, ge=0)
    high_importance_min_sources: int = Field(2, ge=1)
    min_story_angle_chars: int = Field(10, ge=0)  # 0 turns the rule off
    subtitles_required: bool = True
    hook_overlay_sec: float = Field(3.0, ge=0)  # the hook as big text at the top (0 = off)
    auto_backup_keep: int = Field(14, ge=0)  # daily database copies to keep (0 = no auto backup)
    shots_target: int = Field(12, ge=1)  # pictures per video the storyboarder aims for
    shots_max: int = Field(12, ge=1)     # hard limit: your daily picture budget
    # New episodes (old ones keep theirs). Why these: docs/STRATEGY.md
    default_category: Category = Category.FOOD  # a typo in settings.yaml is reported
    target_duration_sec: int = Field(35, gt=0)
    hook_max_words: int = Field(14, ge=0)  # a longer hook gets a warning (0 = off)
    # The look of every picture (added to each image prompt). Change it here to restyle.
    visual_style_name: str = "appetizing_v1"
    visual_style: str = (
        "Rich painterly food illustration, like a painting in a high-end food magazine: the food "
        "in sharp focus with glistening, appetizing textures, warm natural light from the side, "
        "deep warm colours, soft painterly background with period details. Visible brushwork, "
        "clearly a painting, not a photo. People only as hands, silhouettes or seen from behind. "
        "No text on screen.")

    # Originality gate thresholds (0 to 1). Higher = stricter match needed to flag.
    originality_title_near_dup: float = Field(0.85, ge=0, le=1)
    originality_script_near_dup: float = Field(0.6, ge=0, le=1)
    originality_topic_overlap: float = Field(0.5, ge=0, le=1)
    originality_topic_near_dup: float = Field(0.8, ge=0, le=1)
    originality_topic_contained: float = Field(1.0, ge=0, le=1)
    originality_contained_block_words: int = Field(3, ge=1)
    originality_ignore_words: list[str] = Field(default_factory=lambda: [
        "accidental", "accident", "invention", "invented"])
    originality_angle_similar: float = Field(0.4, ge=0, le=1)
    originality_angle_review: float = Field(0.6, ge=0, le=1)
    originality_claim_review: float = Field(0.3, ge=0, le=1)
    originality_claim_block: float = Field(0.7, ge=0, le=1)
    originality_source_review: float = Field(0.6, ge=0, le=1)
    originality_semantic_review: float = Field(0.8, ge=0, le=1)
    originality_include_rejected: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studio.db"

    @property
    def model_dir(self) -> Path:
        """Downloaded AI models (narration voice, subtitle timing)."""
        return self.data_dir / "models"

    def length_ok(self, seconds: float | None) -> bool:
        """Is this a valid length for a video / narration?"""
        return seconds is not None and self.min_duration_sec <= seconds <= self.max_duration_sec

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load_env(path: Path | None = None) -> list[str]:
    """Read KEY=value lines from the .env file into the environment (built-in, no library).
    Values already set in the real environment win. Set SHORTS_NO_DOTENV=1 to skip (tests).
    Returns the names it set (never the values)."""
    if os.environ.get("SHORTS_NO_DOTENV"):
        return []
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return []
    added = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and name not in os.environ:
            os.environ[name] = value
            added.append(name)
    return added


def read_yaml(path: Path) -> dict:
    """A YAML file as a dict; a typo gives a plain message, not a traceback."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise StudioError(f"{path} is missing.") from None
    except yaml.YAMLError as e:
        raise StudioError(f"{path.name} has a mistake: {e}") from None


def load_yaml_model[M: BaseModel](path: Path, model: type[M]) -> M:
    """Read a config file and check it against its Pydantic model (one way for every file)."""
    try:
        return model.model_validate(read_yaml(path))
    except ValidationError as e:
        raise StudioError(f"{path.name} has a mistake: {e}") from None


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_env()
    path = Path(config_path) if config_path else PROJECT_ROOT / "config" / "settings.yaml"
    raw = read_yaml(path) if path.exists() else {}
    env_dir = os.environ.get("SHORTS_DATA_DIR")
    if env_dir:
        raw["data_dir"] = env_dir
    try:
        settings = Settings(**raw)
    except ValidationError as e:
        raise StudioError(f"{path.name} has a mistake: {e}") from None
    if not settings.data_dir.is_absolute():
        settings = settings.model_copy(update={"data_dir": PROJECT_ROOT / settings.data_dir})
    return settings
