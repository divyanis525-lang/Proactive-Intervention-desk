"""
PII protection (Production Bar Checklist 6.4).

Design decision (research_log.md item R4): PII masking is applied as a
transform at the *data access* boundary (StateBoard.publish / EpisodicMemory
.write / any prompt-builder in agents/base_agent.py's llm_call), not just
sprinkled into individual agents. That way no agent can accidentally leak
raw PII into a log or an LLM prompt simply by forgetting to call a masker
-- the mask is applied on the way *out* of the shared substrate.

This is intentionally a deterministic, regex-based scanner (not an LLM call)
so it can't be reasoned around, matching the "guardrails must be
deterministic and hard-coded" requirement.
"""
import hashlib
import hmac
import re
from typing import Any

_PATTERNS = [
    # card numbers (13-19 digits, optionally grouped) -- check before generic long-digit account ids
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD_REDACTED]"),
    # SSN / national-id-like: 9 digits with optional dashes in 3-2-4 groups
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_REDACTED]"),
    # account ids like ACC_CHK_001, CUST_00042 -> keep type prefix, mask the number
    (re.compile(r"\b(ACC|CUST)_([A-Z]+_)?(\d+)\b"), r"\1_\2[ID_REDACTED]"),
    # emails
    (re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "[EMAIL_REDACTED]"),
    # phone numbers (very loose, international-friendly)
    (re.compile(r"\b\+?\d{1,3}[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"), "[PHONE_REDACTED]"),
]

# Fields that are structurally never forwarded to an LLM prompt or a log
# line at all (not just masked -- dropped), because masking a field like
# a raw account balance to a token loses no information an agent needs
# beyond the *fact* it was present.
DROP_FIELDS = {"ssn", "national_id", "full_account_number", "card_number"}

_PSEUDONYM_SALT = "customer360-mas-local-build-salt"  # NOT a secret-management setup --
# see research_log.md's PII item: a real deployment sources this from a
# secrets manager / KMS, rotated per environment. Hardcoded here only
# because this is a local, offline grading build with no secrets store.


def pseudonymize(value: str) -> str:
    """Deterministic, non-reversible tokenization for identifiers that must
    stay CONSISTENT across a trace/log but must not appear in the clear
    there (Production Checklist 6.4's PII-protection item, refined per the
    "interface-layer" framing in Himansu Saha's dynamic PII masking piece
    and the tokenization strategy it names: mask before the value leaves
    the interface, but keep it stable so downstream correlation --
    "these five spans are the same customer" -- still works without ever
    writing the real ID to disk).
    """
    if not value:
        return value
    h = hmac.new(_PSEUDONYM_SALT.encode(), value.encode(), hashlib.sha256).hexdigest()[:10]
    prefix = value.split("_")[0] if "_" in value else "ID"
    return f"{prefix}_{h}"


def mask_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def mask_payload(payload: Any) -> Any:
    """Recursively mask a dict/list/str payload before it is logged or
    included in any LLM prompt. Drops DROP_FIELDS entirely."""
    if isinstance(payload, dict):
        result = {}
        for k, v in payload.items():
            if k.lower() in DROP_FIELDS:
                result[k] = "[DROPPED]"
                continue
            result[k] = mask_payload(v)
        return result
    if isinstance(payload, list):
        return [mask_payload(v) for v in payload]
    if isinstance(payload, str):
        return mask_text(payload)
    return payload
