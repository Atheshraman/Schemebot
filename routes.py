import asyncio
import os
from typing import Any, Dict, List
import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import FileResponse

from profile_chat import ProfileChatbot
from local_db import (
    PROFILE_FIELDS,
    delete_bookmark,
    get_profile,
    list_bookmarks,
    save_bookmark,
    update_profile,
    upsert_profile,
    get_chat_history,
    get_all_sessions,
    rename_session,
)
from retriever import retrieve_candidate_schemes
from verifier import verify_eligibility
from embedder import embed_all_schemes
from myscheme_scraper import scrape_all
from supabase_client import get_supabase_client

router = APIRouter()

_STATIC_DIR = os.path.dirname(__file__)


@router.get("/")
async def serve_index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@router.get("/schemes")
async def list_schemes() -> Dict[str, Any]:
    """Return all schemes from Supabase for the directory view."""
    try:
        supabase = get_supabase_client()
        response = (
            supabase.table("schemes")
            .select("id,name,ministry,state,description,benefits,application_url,source_url,youtube_url")
            .limit(200)
            .execute()
        )
        schemes = response.data or []
        return {"schemes": schemes}
    except Exception as exc:
        return {"schemes": [], "error": str(exc)}


@router.get("/chat-sessions")
async def list_chat_sessions() -> Dict[str, Any]:
    """Return all chat sessions with their metadata."""
    sessions = get_all_sessions()
    return {"sessions": sessions}


@router.put("/chat-sessions/{session_id}/title")
async def update_session_title(session_id: str, payload: Dict[str, Any]) -> Dict[str, str]:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    rename_session(session_id, title)
    return {"status": "ok"}


@router.get("/chat-history/{session_id}")
async def chat_history(session_id: str) -> Dict[str, Any]:
    return {"messages": get_chat_history(session_id, limit=100)}


@router.get("/bookmarks/{session_id}")
async def bookmarks(session_id: str) -> Dict[str, Any]:
    return {"bookmarks": list_bookmarks(session_id)}


@router.post("/bookmarks/{session_id}")
async def add_bookmark(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    scheme_id = str(payload.get("scheme_id") or "").strip()
    scheme_name = str(payload.get("scheme_name") or "Scheme").strip()
    if not scheme_id:
        raise HTTPException(status_code=400, detail="scheme_id required")
    save_bookmark(session_id, scheme_id, scheme_name, payload.get("deadline"))
    return {"status": "saved", "bookmarks": list_bookmarks(session_id)}


@router.delete("/bookmarks/{session_id}/{scheme_id}")
async def remove_bookmark(session_id: str, scheme_id: str) -> Dict[str, str]:
    delete_bookmark(session_id, scheme_id)
    return {"status": "removed"}


def _run_scrape_and_embed() -> None:
    asyncio.run(scrape_all())
    embed_all_schemes()



@router.post("/chat")
async def chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    session_id = payload.get("session_id")
    message = payload.get("message")
    if not session_id or not message:
        raise HTTPException(status_code=400, detail="session_id and message required")

    language = payload.get("language", "en")
    bot = ProfileChatbot(session_id, language=language)
    try:
        result = bot.chat(message)
    except (ValueError, RuntimeError, httpx.RequestError, httpx.HTTPStatusError) as exc:
        # Return a friendly message instead of crashing with 502, so the UI stays usable
        error_msg = str(exc)
        if "Cannot reach LM Studio" in error_msg or "RequestError" in type(exc).__name__:
            friendly = (
                "[AI Offline] I can't reach the AI model right now. "
                "Please make sure LM Studio is open and its local server is running "
                "(click the green play button in LM Studio), then try again."
            )
        else:
            friendly = (
                "[AI Error] The AI model returned an empty response. "
                "Please check that a model is loaded in LM Studio and try again."
            )
        return {"reply": friendly, "profile_complete": False, "trigger_check": False}
    return result


@router.post("/check-eligibility")
async def check_eligibility(payload: Dict[str, Any]) -> Dict[str, Any]:
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    profile = get_profile(session_id)
    if not profile:
        raise HTTPException(status_code=404, detail="profile not found")

    try:
        schemes = retrieve_candidate_schemes(profile)
    except APIError as exc:
        error_text = str(exc)
        if "eligibility_criteria" in error_text or "schemes" in error_text:
            raise HTTPException(
                status_code=503,
                detail="Supabase schema is not ready. Run schema.sql in the Supabase SQL editor, then retry.",
            ) from exc
        raise HTTPException(status_code=502, detail="Supabase scheme lookup failed.") from exc
    result = verify_eligibility(profile, schemes)
    return result


@router.get("/profile/{session_id}")
async def get_profile_route(session_id: str) -> Dict[str, Any]:
    profile = get_profile(session_id)
    if not profile:  # Create a minimal profile entry for this session
        upsert_profile(session_id)
        profile = get_profile(session_id)
    return profile


@router.put("/profile/{session_id}")
async def update_profile_route(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed_fields = {field for field in PROFILE_FIELDS if field not in {"updated_at"}}
    fields = {key: value for key, value in payload.items() if key in allowed_fields}
    if not fields:
        raise HTTPException(status_code=400, detail="at least one valid profile field is required")
    try:
        return update_profile(session_id, fields)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/scrape")
async def scrape(background_tasks: BackgroundTasks, x_admin_token: str = Header(default="")) -> Dict[str, str]:
    expected_token = os.environ.get("ADMIN_TOKEN", "")
    if not expected_token or x_admin_token != expected_token:
        raise HTTPException(status_code=401, detail="unauthorized")

    background_tasks.add_task(_run_scrape_and_embed)
    return {"status": "started"}
