import os

import pytest
from helpers import sample_episode

from studio.config import Settings
from studio.db import EpisodeRepository

# Tests never read your real .env, and never see real AI keys (no network, no costs).
os.environ["SHORTS_NO_DOTENV"] = "1"
for _key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(_key, None)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def repo(settings):
    return EpisodeRepository(settings)


@pytest.fixture
def full_episode(repo):
    """An episode in the database, still at IDEA, with complete valid content saved."""
    return repo.get_episode(sample_episode(repo))
