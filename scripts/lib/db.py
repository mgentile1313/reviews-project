"""Supabase client for the data scripts (uses the service-role key)."""

from supabase import Client, ClientOptions, create_client

from .config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

# Explicit 30s timeout on every PostgREST request. Without this, a stalled
# connection can hang for hours (we hit this twice during Phase C, where two
# chunks took 3.5–4.5 hours apiece before httpx's default timeout fired).
# Our largest legitimate request is ~500-row upserts in load_reviews, well
# under 30s on a normal connection. If 30s elapses, something is genuinely
# wrong — fail fast so the caller's retry logic can take over.
POSTGREST_TIMEOUT_SECONDS = 30


def get_client() -> Client:
    """Return a service-role Supabase client. Server-side use only."""
    return create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY,
        options=ClientOptions(postgrest_client_timeout=POSTGREST_TIMEOUT_SECONDS),
    )
