import os
from typing import Any, Dict, List, Optional, Set

import httpx
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

from supabase_client import get_supabase_client
from youtube_search import find_youtube_video, youtube_search_fallback

load_dotenv()

MODEL_NAME = "all-MiniLM-L6-v2"
_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _build_profile_summary(profile: Dict[str, Any]) -> str:
    parts: List[str] = []

    if profile.get("occupation"):
        parts.append(str(profile["occupation"]))
    if profile.get("age") is not None:
        parts.append(f"age {profile['age']}")
    if profile.get("caste_category"):
        parts.append(f"{profile['caste_category']} category")
    if profile.get("state"):
        parts.append(str(profile["state"]))
    if profile.get("annual_income") is not None:
        parts.append(f"annual income {profile['annual_income']}")
    if profile.get("bpl_status"):
        parts.append("BPL family")
    if profile.get("gender"):
        parts.append(str(profile["gender"]))

    return ", ".join(parts) if parts else "Indian citizen"


def _structured_filter(profile: Dict[str, Any]) -> Set[str]:
    supabase = get_supabase_client()
    fields = []

    if profile.get("annual_income") is not None:
        fields.append("annual_income")
    if profile.get("caste_category"):
        fields.append("caste_category")
    if profile.get("gender"):
        fields.append("gender")

    if not fields:
        return set()

    response = (
        supabase.table("eligibility_criteria")
        .select("scheme_id,field,operator,value")
        .in_("field", fields)
        .execute()
    )
    criteria_rows = response.data or []

    exclude: Set[str] = set()
    for row in criteria_rows:
        scheme_id = row["scheme_id"]
        field = row["field"]
        operator = row["operator"]
        value = row["value"]

        if field == "annual_income" and profile.get("annual_income") is not None:
            try:
                income_limit = int(value)
            except (TypeError, ValueError):
                continue
            if operator == "less_than" and profile["annual_income"] > income_limit:
                exclude.add(scheme_id)
        elif field == "caste_category" and profile.get("caste_category"):
            if operator == "equals" and value and value.lower() != str(profile["caste_category"]).lower():
                exclude.add(scheme_id)
        elif field == "gender" and profile.get("gender"):
            if operator == "equals" and value and value.lower() != str(profile["gender"]).lower():
                exclude.add(scheme_id)

    all_schemes = supabase.table("schemes").select("id").execute().data or []
    all_ids = {row["id"] for row in all_schemes}
    return all_ids - exclude


def _vector_search(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Perform vector similarity search via direct HTTP call.

    The Supabase Python SDK's .rpc() silently returns 0 results when the
    ivfflat index has too few rows (<= lists*3). Using httpx directly to
    the PostgREST RPC endpoint bypasses the index limitation.
    """
    model = _get_model()
    summary = _build_profile_summary(profile)
    embedding = model.encode([summary])[0]

    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_KEY", "")
    if not supabase_url or not supabase_key:
        return []

    url = supabase_url.rstrip("/") + "/rest/v1/rpc/match_schemes"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    payload = {"query_vector": embedding.tolist(), "match_count": 20}
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json() or []
    except Exception:
        return []


def retrieve_candidate_schemes(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    shortlist_ids = _structured_filter(profile)
    vector_results = _vector_search(profile)
    vector_ids = [row["id"] for row in vector_results]

    supabase = get_supabase_client()

    # If vector search returned nothing (embeddings not yet generated),
    # fall back to all schemes so eligibility check still works.
    if not vector_ids:
        fallback = supabase.table("schemes").select("id").limit(50).execute()
        vector_ids = [row["id"] for row in (fallback.data or [])]

    if shortlist_ids:
        final_ids = [scheme_id for scheme_id in vector_ids if scheme_id in shortlist_ids]
        # If filter excluded everything, relax it and return all vector results
        if not final_ids:
            final_ids = vector_ids
    else:
        final_ids = vector_ids

    if not final_ids:
        return []

    supabase = get_supabase_client()
    schemes_response = (
        supabase.table("schemes")
        .select("id,name,description,benefits,application_url,source_url,youtube_url")
        .in_("id", final_ids)
        .execute()
    )
    schemes = schemes_response.data or []

    criteria_response = (
        supabase.table("eligibility_criteria")
        .select("scheme_id,field,operator,value")
        .in_("scheme_id", final_ids)
        .execute()
    )
    criteria_rows = criteria_response.data or []

    criteria_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in criteria_rows:
        criteria_map.setdefault(row["scheme_id"], []).append(row)

    scheme_map = {scheme["id"]: scheme for scheme in schemes}
    result: List[Dict[str, Any]] = []

    for scheme_id in final_ids:
        scheme = scheme_map.get(scheme_id)
        if not scheme:
            continue
        if not scheme.get("youtube_url"):
            scheme_name = str(scheme.get("name") or "government scheme")
            video_url = find_youtube_video(scheme_name)
            scheme["youtube_url"] = video_url or youtube_search_fallback(scheme_name)
            if video_url:
                supabase.table("schemes").update({"youtube_url": video_url}).eq("id", scheme_id).execute()
        # Older scraped rows (or pages with no dedicated "Apply" link) can
        # have an empty application_url. Fall back to the source page so the
        # card always has at least one working link instead of showing none.
        if not scheme.get("application_url"):
            scheme["application_url"] = scheme.get("source_url")
        scheme["eligibility_criteria"] = criteria_map.get(scheme_id, [])
        result.append(scheme)

    return result