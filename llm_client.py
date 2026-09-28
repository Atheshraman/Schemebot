import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_API_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_MODEL = "local-model"

# Qwen3 reasoning models need a large token budget:
#   ~500-800 tokens for the <think> block
#   ~200-400 tokens for the actual reply
# Total safe minimum = 2048
CHAT_MAX_TOKENS = 2048


def _get_loaded_model(api_url: str) -> Optional[str]:
    """Ask LM Studio which model is actually loaded right now."""
    models_url = api_url.replace("/v1/chat/completions", "/v1/models")
    try:
        resp = httpx.get(models_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data") or []
            if models:
                model_id = models[0].get("id") or models[0].get("name")
                print(f"[LLM] Auto-detected model: {model_id}", flush=True)
                return model_id
    except Exception as exc:
        print(f"[LLM] Could not auto-detect model: {exc}", flush=True)
    return None


def _post_with_retry(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> httpx.Response:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            # 600s timeout: reasoning models can take a long time to think + respond on some hardware
            return httpx.post(url, json=payload, headers=headers, timeout=600)
        except httpx.RequestError as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Failed to call LM Studio API")


def _strip_thinking_tags(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models (e.g. Qwen3).
    Also strips incomplete <think> blocks if the model ran out of tokens mid-think.
    """
    # Remove complete <think>...</think> blocks (allowing for attributes, spaces, and tag variants)
    text = re.sub(r"<\s*(think|thinking|thought|reasoning)[^>]*>.*?</\s*\1\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove incomplete <think>... (model ran out of tokens inside thinking block)
    text = re.sub(r"<\s*(think|thinking|thought|reasoning)[^>]*>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove markdown-style thinking blocks like "Thought: ...\n\n" or "**Thinking Process:** ...\n\n"
    # We look for "Thought:" followed by anything up to a double newline.
    text = re.sub(r"(?i)^[\*\s]*(?:thought|thinking process|reasoning)[\*\s]*:.*?(\n\s*\n|$)", "", text, flags=re.DOTALL)
    
    return text.strip()


def _extract_content(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            finish_reason = first.get("finish_reason", "unknown")
            if finish_reason == "length":
                print("[LLM] Warning: response was cut off (finish_reason=length). "
                      "Consider increasing max_tokens.", flush=True)

            message = first.get("message") or {}
            if isinstance(message, dict):
                # Primary content field
                content = message.get("content")
                # reasoning_content: some LM Studio builds put the reply here
                # when enable_thinking interacts badly with certain models
                reasoning = message.get("reasoning_content") or ""

                raw = str(content) if content is not None else ""
                print(f"[LLM] Raw response ({len(raw)} chars, finish={finish_reason}): "
                      f"{raw[:120]!r}  | reasoning_content={str(reasoning)[:60]!r}",
                      flush=True)

                stripped = _strip_thinking_tags(raw)
                if stripped:
                    return stripped
                
                # If content is empty after stripping, we should NOT return reasoning_content 
                # because reasoning_content contains the raw thoughts!
                # We return empty string so call_llm can trigger a retry with larger max_tokens.
                return ""

            if first.get("text") is not None:
                return _strip_thinking_tags(str(first["text"]))

    if data.get("content") is not None:
        return _strip_thinking_tags(str(data["content"]))

    raise ValueError("Unexpected response format from LM Studio API")


def _build_payload(
    messages: List[Dict[str, str]],
    max_tokens: int,
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    model = (model_override or os.environ.get("LMSTUDIO_MODEL", DEFAULT_MODEL) or "").strip()
    temperature = float(os.environ.get("LMSTUDIO_TEMPERATURE", "0.1"))

    payload: Dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    reasoning_effort = os.environ.get("LMSTUDIO_REASONING_EFFORT", "").strip()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    # Disable thinking mode for Qwen3 and similar reasoning models.
    # NOTE: chat_template_kwargs with enable_thinking:false can cause LM Studio
    # to return an empty 'content' field (putting output in reasoning_content instead).
    # We intentionally DO NOT set enable_thinking:false here — instead we strip
    # <think> tags from whatever the model outputs.
    # if os.environ.get("LMSTUDIO_DISABLE_THINKING", "").strip().lower() in {"1", "true", "yes"}:
    #     payload["chat_template_kwargs"] = {"enable_thinking": False}

    if model and model.lower() not in {"none", ""}:
        payload["model"] = model

    return payload


def call_llm(messages: List[Dict[str, str]], max_tokens: int = CHAT_MAX_TOKENS) -> str:
    api_url = os.environ.get("LMSTUDIO_API_URL", DEFAULT_API_URL).strip()

    headers = {"Content-Type": "application/json"}
    auth_header = os.environ.get("LMSTUDIO_AUTH_HEADER", "").strip()
    auth_value = os.environ.get("LMSTUDIO_AUTH_VALUE", "").strip()
    if auth_header and auth_value:
        headers[auth_header] = auth_value

    # Auto-detect the currently loaded model so the model name in .env
    # doesn't have to exactly match LM Studio's internal identifier.
    configured_model = os.environ.get("LMSTUDIO_MODEL", "").strip()
    detected_model = _get_loaded_model(api_url)
    model_to_use = detected_model or configured_model or None

    # Enforce a minimum token budget so the model can complete thinking + reply.
    # Qwen3 think blocks alone can be 500-800 tokens, so we need at least 2048 total.
    effective_max = max(max_tokens, CHAT_MAX_TOKENS)

    print(f"[LLM] Calling with model={model_to_use!r}, max_tokens={effective_max}", flush=True)

    payload = _build_payload(messages, effective_max, model_override=model_to_use)
    try:
        response = _post_with_retry(api_url, payload, headers)
    except httpx.RequestError as exc:
        raise RuntimeError(
            "Cannot reach LM Studio at " + api_url + ". Make sure LM Studio is open "
            "and its local server is started (green play button)."
        ) from exc

    response.raise_for_status()
    data = response.json()
    content = _extract_content(data).strip()

    # If still empty (think block consumed all tokens), retry with 4× budget
    if not content:
        doubled = effective_max * 4
        print(f"[LLM] Empty after stripping. Retrying with max_tokens={doubled}", flush=True)
        retry_payload = _build_payload(messages, doubled, model_override=model_to_use)
        retry_response = _post_with_retry(api_url, retry_payload, headers)
        retry_response.raise_for_status()
        content = _extract_content(retry_response.json()).strip()

    if not content:
        raise ValueError(
            "LM Studio returned an empty response. "
            f"Model detected: {model_to_use!r}. "
            "The model may need more tokens. Try setting a larger context window in LM Studio."
        )
    return content
