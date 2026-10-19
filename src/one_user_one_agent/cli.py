"""Command-line entry point.

    python -m one_user_one_agent replay --out sample_run          # pack found automatically
    python -m one_user_one_agent replay --pack <pack> --out <dir> --seed <seed>
    python -m one_user_one_agent pilot
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import AgentConfig, Pack, find_pack
from .impact_analysis import analyse_pilot
from .replay import run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="one_user_one_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="run the replay day through the agent, the simulator and the outcome loop")
    r.add_argument("--pack", default=None, help="challenge pack root (default: OUA_PACK, then this folder or a parent)")
    r.add_argument("--out", default="sample_run", help="output directory")
    r.add_argument("--seed", default="oua-practice", help="simulator seed")
    r.add_argument("--skip-sim", action="store_true", help="only produce decisions/actions")
    p = sub.add_parser("pilot", help="print the randomized pilot analysis as JSON")
    p.add_argument("--pack", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    try:
        pack = find_pack(a.pack)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.cmd == "replay":
        res = run(pack, a.out, seed=a.seed, skip_sim=a.skip_sim, config=AgentConfig())
        print(json.dumps(res, indent=2))
    else:
        pk = Pack(pack)
        print(json.dumps(analyse_pilot(pk.path("pilot", "pilot_assignment.csv"),
                                       pk.path("pilot", "pilot_outcomes.csv")), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
