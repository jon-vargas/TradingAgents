import json
import re
from typing import Any, Dict, List, Optional


POSITIVE_KEYWORDS = [
    "buy",
    "bull",
    "bullish",
    "upside",
    "outperform",
    "strong",
    "positive",
    "accelerate",
    "growth",
]

NEGATIVE_KEYWORDS = [
    "sell",
    "bear",
    "bearish",
    "downside",
    "underperform",
    "weak",
    "negative",
    "risk",
    "headwind",
]


SECTION_FIELDS = {
    "Market": "market_report",
    "Fundamentals": "fundamentals_report",
    "News": "news_report",
    "Sentiment": "sentiment_report",
    "Research": "investment_plan",
    "Trading Plan": "trader_investment_plan",
}

VALID_STANCE_TOKENS = ("bull", "bear", "buy", "sell", "positive", "negative", "neutral", "hold")


def strip_signal_json(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    marker = "SIGNAL_JSON:"
    while marker in cleaned:
        idx = cleaned.find(marker)
        if idx == -1:
            break
        after_idx = idx + len(marker)
        brace_start = cleaned.find("{", after_idx)
        end_pos = None
        if brace_start != -1:
            depth = 0
            for i in range(brace_start, len(cleaned)):
                ch = cleaned[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
        if end_pos is not None:
            cleaned = (cleaned[:idx] + cleaned[end_pos + 1 :]).strip()
        else:
            # Malformed or unterminated signal: drop it entirely.
            cleaned = cleaned[:idx].rstrip()
            break
    return cleaned.strip()


def _normalize_confidence(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        if 0 <= value <= 1:
            return float(value)
        if 1 < value <= 100:
            return float(value) / 100.0
    return None


def _is_valid_stance(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in VALID_STANCE_TOKENS)


def _find_signal_json_block(text: str) -> str:
    if not text:
        return ""
    marker = "SIGNAL_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return ""
    after_idx = idx + len(marker)
    brace_start = text.find("{", after_idx)
    if brace_start == -1:
        return ""
    depth = 0
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : i + 1]
    return ""


def _sanitize_signal_json(data: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    section = data.get("section")
    if isinstance(section, str) and section.strip():
        cleaned["section"] = section.strip()

    stance = data.get("stance")
    if isinstance(stance, str) and stance.strip():
        cleaned["stance"] = stance.strip()

    confidence = _normalize_confidence(data.get("confidence"))
    if confidence is not None:
        cleaned["confidence"] = confidence

    key_factors = data.get("key_factors")
    if isinstance(key_factors, list):
        cleaned_factors = [str(item).strip() for item in key_factors if str(item).strip()]
        if cleaned_factors:
            cleaned["key_factors"] = cleaned_factors

    return cleaned


def _extract_signal_json(text: str) -> Dict[str, Any]:
    raw = _find_signal_json_block(text)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return _sanitize_signal_json(data)


def lint_signal_json(text: str, expected_section: Optional[str] = None) -> List[str]:
    issues: List[str] = []
    if not text or "SIGNAL_JSON:" not in text:
        return ["missing_signal_json"]

    raw = _find_signal_json_block(text)
    if not raw:
        return ["invalid_signal_json"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ["invalid_signal_json"]

    if not isinstance(data, dict):
        return ["invalid_signal_json"]

    section = data.get("section")
    if not isinstance(section, str) or not section.strip():
        issues.append("missing_section")
    elif expected_section and section.strip().lower() != expected_section.lower():
        issues.append("section_mismatch")

    stance = data.get("stance")
    if not isinstance(stance, str) or not stance.strip():
        issues.append("missing_stance")
    elif not _is_valid_stance(stance):
        issues.append("invalid_stance")

    confidence = _normalize_confidence(data.get("confidence"))
    if confidence is None:
        issues.append("missing_confidence" if "confidence" not in data else "invalid_confidence")

    key_factors = data.get("key_factors")
    if not isinstance(key_factors, list) or not key_factors:
        issues.append("invalid_key_factors")
    else:
        if not all(isinstance(item, str) and item.strip() for item in key_factors):
            issues.append("invalid_key_factors")

    return issues


def lint_signal_json_sections(state: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    for section, field in SECTION_FIELDS.items():
        text = state.get(field, "")
        if not text:
            continue
        for issue in lint_signal_json(text, expected_section=section):
            issues.append(f"signal:{section}:{issue}")
    return issues


def _count_keywords(text: str, keywords: List[str]) -> int:
    if not text:
        return 0
    count = 0
    lowered = text.lower()
    for keyword in keywords:
        count += len(re.findall(rf"\b{re.escape(keyword)}\b", lowered))
    return count


def _infer_alignment(text: str, decision: str) -> Dict[str, Any]:
    signal = _extract_signal_json(text)
    stance = str(signal.get("stance", "")).lower()
    confidence = signal.get("confidence")

    if stance:
        if "bull" in stance or "buy" in stance or "positive" in stance:
            signal_alignment = "bullish"
        elif "bear" in stance or "sell" in stance or "negative" in stance:
            signal_alignment = "bearish"
        else:
            signal_alignment = "neutral"
    else:
        signal_alignment = ""

    pos_count = _count_keywords(text, POSITIVE_KEYWORDS)
    neg_count = _count_keywords(text, NEGATIVE_KEYWORDS)

    if not decision:
        decision = "HOLD"
    decision = decision.upper()

    if signal_alignment:
        if "BUY" in decision:
            score = 2 if signal_alignment == "bullish" else -2 if signal_alignment == "bearish" else 0
        elif "SELL" in decision:
            score = 2 if signal_alignment == "bearish" else -2 if signal_alignment == "bullish" else 0
        else:
            score = 0
    else:
        if "BUY" in decision:
            score = pos_count - neg_count
        elif "SELL" in decision:
            score = neg_count - pos_count
        else:
            score = 0

    if signal_alignment:
        if "BUY" in decision:
            alignment = "supports" if signal_alignment == "bullish" else "contradicts" if signal_alignment == "bearish" else "mixed"
        elif "SELL" in decision:
            alignment = "supports" if signal_alignment == "bearish" else "contradicts" if signal_alignment == "bullish" else "mixed"
        else:
            alignment = "mixed"
    else:
        if score > 1:
            alignment = "supports"
        elif score < -1:
            alignment = "contradicts"
        elif pos_count or neg_count:
            alignment = "mixed"
        else:
            alignment = "neutral"

    if isinstance(confidence, (int, float)) and confidence >= 0:
        strength = round(confidence * 100)
    else:
        strength = abs(score)
    return {
        "alignment": alignment,
        "signal_strength": strength,
        "positive_hits": pos_count,
        "negative_hits": neg_count,
    }


def compute_section_attribution(state: Dict[str, Any], decision: str) -> Dict[str, Any]:
    results = {}
    for section, field in SECTION_FIELDS.items():
        text = state.get(field, "")
        if text:
            results[section] = _infer_alignment(text, decision)
        else:
            results[section] = {"alignment": "missing", "signal_strength": 0, "positive_hits": 0, "negative_hits": 0}

    return results


def format_section_attribution(attribution: Dict[str, Any]) -> str:
    if not attribution:
        return ""

    lines = [
        "### Section Attribution",
        "| Section | Alignment | Signal Strength |",
        "| --- | --- | --- |",
    ]

    for section, metrics in attribution.items():
        alignment = metrics.get("alignment", "neutral")
        strength = metrics.get("signal_strength", 0)
        lines.append(f"| {section} | {alignment} | {strength} |")

    return "\n".join(lines)


def attribution_to_json(attribution: Dict[str, Any]) -> str:
    try:
        return json.dumps(attribution)
    except TypeError:
        return "{}"


def summarize_long_horizon_attribution(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate deterministic attribution slices for long-horizon workflows."""
    total = len(rows or [])
    if total == 0:
        return {
            "count": 0,
            "by_horizon": {},
            "by_regime": {},
            "by_bucket": {},
            "go_no_go": {"ready_for_automation": False, "reason": "insufficient_data"},
        }

    def _accumulate(key: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in rows:
            val = str(row.get(key, "unknown") or "unknown")
            out[val] = out.get(val, 0) + 1
        return out

    by_horizon = _accumulate("horizon")
    by_regime = _accumulate("regime")
    by_bucket = _accumulate("primary_bucket")
    matured = int(sum(1 for r in rows if r.get("is_matured")))
    per_regime_min = min(by_regime.values()) if by_regime else 0
    ready = matured >= 40 and per_regime_min >= 10

    return {
        "count": total,
        "matured_count": matured,
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "by_bucket": by_bucket,
        "go_no_go": {
            "ready_for_automation": ready,
            "reason": "pass" if ready else "sample_gate_not_met",
            "min_matured_required": 40,
            "min_per_regime_required": 10,
        },
    }
