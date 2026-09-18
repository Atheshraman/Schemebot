import os
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Client:
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
    if "supabase.com/dashboard" in url:
        raise ValueError(
            "SUPABASE_URL must be the project API URL like https://<project-ref>.supabase.co"
        )

    _supabase_client = create_client(url, key)
    return _supabase_client
