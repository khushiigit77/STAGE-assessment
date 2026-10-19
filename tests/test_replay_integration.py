"""End-to-end: the CLI against the actual supplied pack, including the unmodified simulator."""
import hashlib
import json
import os

import pytest

from conftest import PACK_ROOT

from one_user_one_agent.cli import main


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    assert main(["replay", "--pack", PACK_ROOT, "--out", str(out)]) == 0
    return out


def _jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_replay_cli_produces_consistent_artifacts(run_dir):
    for name in ("decisions.jsonl", "actions.jsonl", "outcomes.jsonl", "latency_report.json", "impact_report.md",
                 "final_report.md", "viewer_state/viewers.jsonl", "viewer_state/goals.jsonl", "sim/simulator_report.json"):
        assert os.path.exists(run_dir / name), name
    decisions = _jsonl(run_dir / "decisions.jsonl")
    actions = _jsonl(run_dir / "actions.jsonl")
    with open(os.path.join(PACK_ROOT, "replay", "triggers.jsonl"), encoding="utf-8") as f:
        n_trig = sum(1 for line in f if line.strip())
    assert len(decisions) == n_trig  # every delivery, including duplicates, is recorded
    assert {d["status"] for d in decisions} <= {"PUBLISHED", "NO_ACTION", "REJECTED", "FAILED", "DUPLICATE"}
    ids = {d["decision_id"] for d in decisions}
    assert all(a["decision_id"] in ids for a in actions)
    holdout = {d["viewer_id"] for d in decisions if d.get("arm") == "holdout"}
    assert not [a for a in actions if a["user_id"] in holdout]
    assert all(d["latency"]["within_budget"] for d in decisions)
    assert max(d["calls"]["model"] for d in decisions) <= 8 and max(d["calls"]["tool"] for d in decisions) <= 20
    sim = json.loads((run_dir / "sim" / "simulator_report.json").read_text())
    assert sim["policy_problem_count"] == 0
    assert sim["counts"].get("holdout_actions_ignored", 0) == 0
    # the withdrawn title is never published after its withdrawal time
    for a in actions:
        if a["type"] == "home_page" and a["published_at"] >= "2026-10-01T07:00:00.000Z":
            assert "T0001" not in [t for r in a["rows"] for t in r["title_ids"]] + [a.get("hero_title_id")]


def test_replay_is_deterministic(run_dir, tmp_path):
    assert main(["replay", "--pack", PACK_ROOT, "--out", str(tmp_path), "--skip-sim"]) == 0

    def digest(p):
        rows = _jsonl(p)
        for r in rows:  # measured wall-clock values legitimately vary between runs
            r.pop("latency", None), r.pop("created_at", None), r.pop("published_at", None)
            for v in r.get("verification", []):  # check details quote measured publish times
                v["checks"] = [(c["check"], c["passed"]) for c in v["checks"]]
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    a1 = [{k: v for k, v in a.items() if k != "published_at"} for a in _jsonl(run_dir / "actions.jsonl")]
    a2 = [{k: v for k, v in a.items() if k != "published_at"} for a in _jsonl(tmp_path / "actions.jsonl")]
    assert a1 == a2
    assert digest(run_dir / "decisions.jsonl") == digest(tmp_path / "decisions.jsonl")
