import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return None


def _fuzzy_match(actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    if expected in actual or actual in expected:
        return True
    return SequenceMatcher(None, actual, expected).ratio() >= 0.8


def _split_candidates(value: Any) -> List[str]:
    if value is None:
        return []
    parts = re.split(r"[|,;/]", str(value))
    return [part.strip() for part in parts if part.strip()]


def _evaluate_criterion(
    profile: Dict[str, Any],
    criterion: Dict[str, Any],
) -> Tuple[str, str, Optional[str]]:
    field = str(criterion.get("field") or "").strip()
    operator = str(criterion.get("operator") or "equals").strip().lower()
    expected_raw = criterion.get("value")

    if not field:
        return "met", "", None

    actual = profile.get(field)
    if actual is None:
        return "missing", field, None

    if expected_raw is None or (isinstance(expected_raw, str) and not expected_raw.strip()):
        return "met", field, None

    actual_num = _parse_number(actual)
    expected_num = _parse_number(expected_raw)
    actual_bool = _parse_bool(actual)
    expected_bool = _parse_bool(expected_raw)
    actual_text = _normalize_text(actual)
    expected_text = _normalize_text(expected_raw)

    def fail_reason() -> str:
        label = field.replace("_", " ")
        expected_display = "unspecified" if expected_raw is None else str(expected_raw)
        return f"{label} does not meet required value ({operator} {expected_display})."

    if operator in {"less_than", "lt"}:
        if actual_num is None or expected_num is None:
            return "failed", field, fail_reason()
        return ("met", field, None) if actual_num < expected_num else ("failed", field, fail_reason())
    if operator in {"less_than_or_equal", "lte"}:
        if actual_num is None or expected_num is None:
            return "failed", field, fail_reason()
        return ("met", field, None) if actual_num <= expected_num else ("failed", field, fail_reason())
    if operator in {"greater_than", "gt"}:
        if actual_num is None or expected_num is None:
            return "failed", field, fail_reason()
        return ("met", field, None) if actual_num > expected_num else ("failed", field, fail_reason())
    if operator in {"greater_than_or_equal", "gte"}:
        if actual_num is None or expected_num is None:
            return "failed", field, fail_reason()
        return ("met", field, None) if actual_num >= expected_num else ("failed", field, fail_reason())

    if operator in {"equals", "eq"}:
        if actual_bool is not None and expected_bool is not None:
            return ("met", field, None) if actual_bool == expected_bool else ("failed", field, fail_reason())
        if actual_num is not None and expected_num is not None:
            return ("met", field, None) if actual_num == expected_num else ("failed", field, fail_reason())
        return ("met", field, None) if _fuzzy_match(actual_text, expected_text) else ("failed", field, fail_reason())

    if operator in {"not_equals", "neq"}:
        if actual_bool is not None and expected_bool is not None:
            return ("met", field, None) if actual_bool != expected_bool else ("failed", field, fail_reason())
        if actual_num is not None and expected_num is not None:
            return ("met", field, None) if actual_num != expected_num else ("failed", field, fail_reason())
        return ("met", field, None) if not _fuzzy_match(actual_text, expected_text) else ("failed", field, fail_reason())

    if operator in {"contains"}:
        return ("met", field, None) if _fuzzy_match(actual_text, expected_text) else ("failed", field, fail_reason())

    if operator in {"in", "one_of", "any_of"}:
        candidates = [_normalize_text(item) for item in _split_candidates(expected_raw)]
        matched = any(_fuzzy_match(actual_text, candidate) for candidate in candidates)
        return ("met", field, None) if matched else ("failed", field, fail_reason())

    if operator in {"not_in"}:
        candidates = [_normalize_text(item) for item in _split_candidates(expected_raw)]
        matched = any(_fuzzy_match(actual_text, candidate) for candidate in candidates)
        return ("met", field, None) if not matched else ("failed", field, fail_reason())

    matched = _fuzzy_match(actual_text, expected_text)
    return ("met", field, None) if matched else ("failed", field, fail_reason())


def verify_eligibility(profile: Dict[str, Any], schemes: List[Dict[str, Any]]) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []

    for scheme in schemes:
        criteria = scheme.get("eligibility_criteria", []) or []
        missing_fields: List[str] = []
        failed_reasons: List[str] = []

        for criterion in criteria:
            status, field, reason = _evaluate_criterion(profile, criterion)
            if status == "missing" and field:
                if field not in missing_fields:
                    missing_fields.append(field)
            elif status == "failed" and reason:
                failed_reasons.append(reason)

        if failed_reasons:
            status = "not_eligible"
            reason = failed_reasons[0]
        elif missing_fields:
            status = "needs_more_info"
            missing_display = ", ".join([field.replace("_", " ") for field in missing_fields])
            reason = f"Need more information to verify eligibility: {missing_display}."
        else:
            status = "eligible"
            reason = "Profile matches the listed eligibility criteria."
            if not criteria:
                reason = "No eligibility criteria listed for this scheme."

        results.append(
            {
                "scheme_id": scheme.get("id"),
                "scheme_name": scheme.get("name"),
                "description": scheme.get("description"),
                "benefits": scheme.get("benefits"),
                "application_url": scheme.get("application_url"),
                "source_url": scheme.get("source_url"),
                "youtube_url": scheme.get("youtube_url"),
                "status": status,
                "reason": reason,
                "missing_fields": missing_fields,
            }
        )

    return {"results": results}
