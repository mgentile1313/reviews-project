"""Supabase client for the data scripts (uses the service-role key)."""

from supabase import Client, create_client

from .config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


def get_client() -> Client:
    """Return a service-role Supabase client. Server-side use only."""
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
