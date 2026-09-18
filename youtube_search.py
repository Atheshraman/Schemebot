import os
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

SERPAPI_URL = "https://serpapi.com/search.json"
logger = logging.getLogger(__name__)


def youtube_search_fallback(scheme_name: str) -> str:
    query = quote_plus(f"{scheme_name} official government scheme")
    return f"https://www.youtube.com/results?search_query={query}"


def find_youtube_video(scheme_name: str) -> Optional[str]:
    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key or not scheme_name.strip():
        return None

    params = {
        "engine": "google",
        "q": f'site:youtube.com/watch "{scheme_name}" official',
        "api_key": api_key,
        "num": 10,
        "gl": "in",
        "hl": "en",
    }
    try:
        response = httpx.get(SERPAPI_URL, params=params, timeout=20)
        response.raise_for_status()
        data: Dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
            logger.warning("SerpAPI rejected SERPAPI_KEY; using the YouTube search fallback.")
        else:
            logger.warning("Unable to find a YouTube video for %r: %s", scheme_name, exc)
        return None

    results = list(data.get("video_results", [])) + list(data.get("organic_results", []))
    for result in results:
        link = str(result.get("link") or "")
        if "youtube.com/watch" in link:
            return link
    return None
