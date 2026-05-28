"""Knowledge-Engine — Task Classifier.

Tiny model classifies incoming tasks by domain and complexity. Structured
JSON output from a fast classification call.

Output: {domain, complexity, confidence, routing_note}

Env vars:
    KE_CLASSIFIER_ENDPOINT  (default: http://127.0.0.1:11434/api/generate)
    KE_CLASSIFIER_MODEL     (default: qwen3:4b)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ..foundation import config  # noqa: F401 — loads .env so KE_* vars are visible

CLASSIFIER_MODEL_DEFAULT = os.environ.get("KE_CLASSIFIER_MODEL", "qwen3:4b")
CLASSIFIER_ENDPOINT_DEFAULT = os.environ.get(
    "KE_CLASSIFIER_ENDPOINT", "http://127.0.0.1:11434/api/generate"
)

CLASSIFIER_PROMPT = """\
You are a task classifier for a distributed processing pipeline.
Given a task description, classify it into exactly one domain and one complexity level.

Domains (pick one):
- "code": code generation, testing, refactoring, static analysis, debugging
- "research": literature review, evidence synthesis, analytical writing, fact-checking
- "legal": legal analysis, case review, regulation, compliance, filings
- "creative": creative writing, brainstorming, narrative, storytelling
- "structured": data extraction, formatting, classification, form filling, parsing
- "general": anything that doesn't fit the above

Complexity (pick one):
- "simple": short input, straightforward task, <2000 tokens expected
- "moderate": medium input, some reasoning needed, 2000-8000 tokens
- "complex": large input, deep reasoning, >8000 tokens

Output EXACTLY one JSON object with these fields:
- "domain": one of the domains above
- "complexity": one of the complexity levels above
- "confidence": float 0.0-1.0 (how confident you are)
- "routing_note": brief one-line explanation

Nothing else. No explanation, no markdown, just the JSON object.\
"""


def classify_task(
    description: str,
    context_hint: str = "",
    endpoint: str | None = None,
    model: str | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Classify a task description into domain + complexity.

    Returns dict with: domain, complexity, confidence, routing_note.
    On error/timeout, returns a safe default (domain=general, complexity=moderate).
    """
    if endpoint is None:
        endpoint = CLASSIFIER_ENDPOINT_DEFAULT

    assert isinstance(endpoint, str)
    if not endpoint.endswith("/api/generate"):
        endpoint = endpoint.rstrip("/") + "/api/generate"

    if model is None:
        model = CLASSIFIER_MODEL_DEFAULT

    prompt = f"Classify this task:\n\n{description}"
    if context_hint:
        prompt += f"\n\nAdditional context: {context_hint}"

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "system": CLASSIFIER_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 2048,
            "num_predict": 256,
        },
    }).encode()

    try:
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read())

        response_text = raw.get("response", "")
        return _parse_classification(response_text)

    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return _default_classification()


def _parse_classification(text: str) -> dict[str, Any]:
    """Extract classification JSON from model response."""
    text = text.strip()

    try:
        result = json.loads(text)
        return _validate_classification(result)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            return _validate_classification(result)
        except json.JSONDecodeError:
            pass

    return _default_classification()


VALID_DOMAINS = {"code", "research", "legal", "creative", "structured", "general"}
VALID_COMPLEXITY = {"simple", "moderate", "complex"}


def _validate_classification(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a classification result."""
    domain = result.get("domain", "general")
    if domain not in VALID_DOMAINS:
        domain = "general"

    complexity = result.get("complexity", "moderate")
    if complexity not in VALID_COMPLEXITY:
        complexity = "moderate"

    confidence = result.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        confidence = 0.5

    return {
        "domain": domain,
        "complexity": complexity,
        "confidence": float(confidence),
        "routing_note": str(result.get("routing_note", ""))[:200],
    }


def _default_classification() -> dict[str, Any]:
    """Safe default when classification fails."""
    return {
        "domain": "general",
        "complexity": "moderate",
        "confidence": 0.0,
        "routing_note": "classification failed -- defaulting to general/moderate",
    }
