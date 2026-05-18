"""Shared config — loads .env.local from the project root."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Both the Next.js app and these scripts read the same .env.local.
load_dotenv(PROJECT_ROOT / ".env.local")


def require(name: str) -> str:
    """Return an env var, or raise if it's missing."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


SUPABASE_URL = require("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = require("SUPABASE_SERVICE_ROLE_KEY")
OPENAI_API_KEY = require("OPENAI_API_KEY")
ANTHROPIC_API_KEY = require("ANTHROPIC_API_KEY")
BRIGHTDATA_API_KEY = require("BRIGHTDATA_API_KEY")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
BRIEF_MODEL = "claude-opus-4-7"
