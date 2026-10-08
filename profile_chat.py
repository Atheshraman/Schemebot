import json
import re
from typing import Any, Dict, List

from llm_client import call_llm
from local_db import get_chat_history, get_missing_fields, get_profile, save_chat_message, upsert_profile
MAX_HISTORY = 6
SUPPORTED_LANGUAGES = {"en": "English", "ta": "Tamil", "ml": "Malayalam", "kn": "Kannada"}


class ProfileChatbot:
    def __init__(self, session_id: str, language: str = "en") -> None:
        self.session_id = session_id
        self.language = language if language in SUPPORTED_LANGUAGES else "en"
        self.profile = get_profile(session_id) or {"session_id": session_id}
        self.history: List[Dict[str, str]] = get_chat_history(session_id)

    def _build_system_prompt(self, missing_fields: List[str]) -> str:
        lang = SUPPORTED_LANGUAGES[self.language]
        known = {k: v for k, v in self.profile.items() if v is not None and k != "session_id" and k != "updated_at"}
        return (
            f"You are a government scheme eligibility assistant. Reply ONLY in {lang}. "
            "Keep replies SHORT (1-2 sentences). Ask only ONE question at a time.\n"
            "NEVER ask for Aadhaar, PAN, or bank details.\n"
            "Do NOT invent scheme names or amounts.\n"
            "If user wants to see schemes or check eligibility, include TRIGGER_CHECK.\n\n"
            f"PROFILE SO FAR: {json.dumps(known, ensure_ascii=True)}\n"
            f"STILL NEED: {json.dumps(missing_fields[:5], ensure_ascii=True)}\n\n"
            "After your reply, on a NEW LINE add:\n"
            "PROFILE_UPDATE: {\"field\": \"value\"} — only for fields the user just gave.\n"
            "Add PROFILE_COMPLETE when age/gender/state/caste_category/annual_income/occupation are all known.\n"
            "Add TRIGGER_CHECK if user asks to see eligible schemes."
        )

    def _extract_profile_update(self, text: str) -> Dict[str, Any]:
        match = re.search(r"PROFILE_UPDATE:\s*(\{.*\})", text, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}

    def chat(self, user_message: str) -> Dict[str, Any]:
        missing_fields = get_missing_fields(self.session_id)
        system_prompt = self._build_system_prompt(missing_fields)
        messages = [{"role": "system", "content": system_prompt}] + self.history + [
            {"role": "user", "content": user_message}
        ]
        response_text = call_llm(messages)

        update_fields = self._extract_profile_update(response_text)
        if update_fields:
            upsert_profile(self.session_id, **update_fields)
            self.profile = get_profile(self.session_id) or self.profile

        reply_text = re.sub(r"\n?PROFILE_UPDATE:.*", "", response_text, flags=re.DOTALL)
        reply_text = re.sub(r"\n?(PROFILE_COMPLETE|TRIGGER_CHECK)", "", reply_text).strip()

        profile_complete = "PROFILE_COMPLETE" in response_text
        trigger_check = "TRIGGER_CHECK" in response_text

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply_text})
        save_chat_message(self.session_id, "user", user_message)
        save_chat_message(self.session_id, "assistant", reply_text)
        # Keep only the last MAX_HISTORY message pairs to reduce prompt size
        if len(self.history) > MAX_HISTORY * 2:
            self.history = self.history[-MAX_HISTORY * 2:]

        return {
            "reply": reply_text,
            "profile_complete": profile_complete,
            "trigger_check": trigger_check,
        }
