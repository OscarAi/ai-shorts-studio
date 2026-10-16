"""The free image and voice tools the studio links to (config/tools.yaml).

The facts about each tool's terms live in that file, not in code, because terms change.
Every file made with a tool is saved with the tool's license note, and a tool whose terms
were last checked long ago is flagged so you re-check them.
"""
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .config import PROJECT_ROOT, load_yaml_model

TOOLS_PATH = PROJECT_ROOT / "config" / "tools.yaml"
RECHECK_AFTER_DAYS = 180
OTHER = "Other"


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    url: str | None = Field(None, pattern=r"^https?://\S+$")
    license_note: str = Field(min_length=1)
    tip: str = ""  # how to get the channel's look with this tool
    daily_limit: int | None = Field(None, ge=1)  # pictures you can make per day with it
    terms_url: str | None = Field(None, pattern=r"^https?://\S+$")
    checked: date | None = None

    def stale(self, today: date | None = None) -> bool:
        """True when the terms were checked more than RECHECK_AFTER_DAYS ago."""
        if self.checked is None or self.terms_url is None:
            return False  # e.g. "my own recording": nothing to re-check
        return ((today or date.today()) - self.checked).days > RECHECK_AFTER_DAYS


class Tools(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_tools: list[Tool] = Field(min_length=1)
    voice_tools: list[Tool] = Field(min_length=1)

    @staticmethod
    def find(tools: list[Tool], name: str) -> Tool | None:
        return next((t for t in tools if t.name.lower() == name.strip().lower()), None)


def load_tools(path: Path | str | None = None) -> Tools:
    return load_yaml_model(Path(path or TOOLS_PATH), Tools)
