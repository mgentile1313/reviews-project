"""Anthropic client for the data scripts."""

from anthropic import Anthropic

from .config import ANTHROPIC_API_KEY


def get_client() -> Anthropic:
    """Return an Anthropic client. Used by labeling (Haiku) and briefs (Sonnet/Opus)."""
    return Anthropic(api_key=ANTHROPIC_API_KEY)
