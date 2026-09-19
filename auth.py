import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_user_from_token(token: str) -> dict | None:
    """Resolve a Supabase access token (JWT) to a user id + email, or None."""
    result = supabase.auth.get_user(token)
    if result is None or result.user is None:
        return None
    return {"sub": result.user.id, "email": result.user.email}