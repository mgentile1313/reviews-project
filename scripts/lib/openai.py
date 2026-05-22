"""OpenAI client for embeddings (Phase C) and future LLM calls."""

from openai import OpenAI

from .config import OPENAI_API_KEY


def get_client() -> OpenAI:
    """Return a configured OpenAI client. Server-side use only."""
    return OpenAI(api_key=OPENAI_API_KEY)
