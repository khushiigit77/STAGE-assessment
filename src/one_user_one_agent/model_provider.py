"""Model provider interface.

The model's only job is to *rank* goals that deterministic eligibility already allows. Its output is an untrusted
proposal: it is schema-validated, and the proposed goal must be in the deterministic candidate list. Model confidence
and reason text are recorded for audit but are never evidence.

The default ``MockModelProvider`` needs no API key. It is deterministic, reproduces ``replay/model_faults.jsonl``
and honours ``dependency_unavailable`` windows for ``model_provider``. A real LLM would implement ``ModelProvider``
and receive the same structured context: catalogue descriptions and other free text are never sent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ModelResponse:
    ok: bool
    output: dict | None
    raw: object
    error: str | None
    latency_s: float


class ModelProvider:
    name = "abstract"

    def propose_goal(self, context: dict, trigger_event_id: str, at: datetime) -> ModelResponse:
        raise NotImplementedError


class MockModelProvider(ModelProvider):
    """Deterministic stand-in: proposes the first candidate (the deterministic priority order)."""

    name = "mock-deterministic-v1"

    def __init__(self, faults: dict[str, dict] | None = None, outages: list[tuple[datetime, datetime]] | None = None,
                 nominal_latency_s: float = 0.4):
        self.faults = faults or {}
        self.outages = outages or []
        self.nominal_latency_s = nominal_latency_s

    def propose_goal(self, context: dict, trigger_event_id: str, at: datetime) -> ModelResponse:
        if any(s <= at < e for s, e in self.outages):
            return ModelResponse(False, None, None, "HTTP 503 model_provider unavailable", 0.1)
        fault = self.faults.get(trigger_event_id)
        if fault:
            if fault.get("fault") == "timeout":
                return ModelResponse(False, None, None, "timeout", float(fault.get("seconds", 60)))
            raw = fault.get("model_output")
            return ModelResponse(True, None, raw, None, self.nominal_latency_s)
        cands = context.get("candidate_goals") or []
        out = ({"goal_id": cands[0], "eligible": True, "confidence": 0.6,
                "reason": "first goal in the deterministic priority order"} if cands else
               {"goal_id": None, "eligible": False, "confidence": 0.6, "reason": "no eligible goal supplied"})
        return ModelResponse(True, None, out, None, self.nominal_latency_s)


def parse_model_output(raw: object) -> tuple[dict | None, str | None]:
    """Strict schema validation. Returns (proposal, error)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"malformed model output: {exc.msg}"
    if not isinstance(raw, dict):
        return None, "model output is not an object"
    gid, elig, conf, reason = raw.get("goal_id"), raw.get("eligible"), raw.get("confidence"), raw.get("reason")
    if gid is not None and not isinstance(gid, str):
        return None, "goal_id must be a string or null"
    if not isinstance(elig, bool):
        return None, "eligible must be boolean"
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return None, "confidence must be a number in [0, 1]"
    if not isinstance(reason, str) or len(reason) > 500:
        return None, "reason must be a string of at most 500 chars"
    extra = set(raw) - {"goal_id", "eligible", "confidence", "reason"}
    if extra:
        return None, f"unexpected fields {sorted(extra)}"
    return {"goal_id": gid, "eligible": elig, "confidence": float(conf), "reason": reason}, None
