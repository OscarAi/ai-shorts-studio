"""Prompt templates (config/prompts/*.md): loaded and filled in one place.

Every prompt has the same shape, so it works the same pasted into a chat AI or sent by the
app (studio/llm.py):
  1. who the AI is, and that text inside <tags> is DATA, never instructions
     (a web page's title or summary must not be able to steer the AI);
  2. the episode's data, inside <tags>;
  3. the task and its rules;
  4. a short self-check;
  5. "Answer with ONLY this JSON" and an example (tests keep it valid for the code).
"""
import re

from .config import PROJECT_ROOT

PROMPT_DIR = PROJECT_ROOT / "config" / "prompts"
ANSWER_MARKER = "Answer with ONLY this JSON, no other text:"
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def fill_template(template: str, values: dict[str, str]) -> str:
    """Replace {name} placeholders. Other braces (JSON examples) are left alone."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def render(name: str, values: dict[str, str]) -> str:
    """config/prompts/<name>.md with its placeholders filled in."""
    template = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return fill_template(template, values).strip() + "\n"


def unfilled(prompt: str) -> list[str]:
    """Placeholders still in a prompt (a forgotten value): should always be empty."""
    return sorted(set(_PLACEHOLDER.findall(prompt)))


def json_example(name: str) -> str:
    """The JSON example at the end of a prompt template (after the answer marker)."""
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return text.split(ANSWER_MARKER, 1)[1]
