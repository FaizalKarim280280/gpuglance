"""Command line entry point.

    gpuglance                 terminal summary (same as `gpuglance status`)
    gpuglance serve           web dashboard on http://localhost:8765
    gpuglance json            print the snapshot as JSON
    gpuglance dump-raw DIR    save raw Slurm JSON (for offline work: --raw-dir DIR)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__, slurm, snapshot


def _parts(s: Optional[str]) -> Optional[List[str]]:
    return [p for p in s.split(",") if p] if s else None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="gpuglance", description="See the cluster's GPUs at a glance.")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--partitions", help="comma-separated partitions (default: every partition with GPU nodes; "
                                             "or $GPUGLANCE_PARTITIONS)")
    common.add_argument("--raw-dir", help="read saved Slurm JSON from this folder instead of calling Slurm")
    sp = ap.add_subparsers(dest="cmd")

    p = sp.add_parser("serve", parents=[common], help="run the web dashboard")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1: only you, via port forwarding)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--interval", type=float, default=30, help="seconds between Slurm queries (default 30)")

    p = sp.add_parser("status", parents=[common], help="terminal summary")
    p.add_argument("models", nargs="*", help="only these GPU models, e.g. h100 a100")
    p.add_argument("--no-color", action="store_true")

    sp.add_parser("json", parents=[common], help="print the snapshot as JSON")

    p = sp.add_parser("dump-raw", parents=[common], help="save raw Slurm JSON to a folder")
    p.add_argument("dir")

    args = ap.parse_args(argv)
    cmd = args.cmd or "status"
    parts = _parts(getattr(args, "partitions", None))
    raw_dir = getattr(args, "raw_dir", None)

    try:
        if cmd == "serve":
            from .server import serve
            serve(args.host, args.port, args.interval, parts, raw_dir)
            return 0
        if cmd == "dump-raw":
            slurm.dump_raw(args.dir, parts)
            print(f"wrote nodes.json, jobs.json, reservations.json to {args.dir}")
            return 0
        snap = snapshot.build(slurm.collect(parts, raw_dir=raw_dir))
        if cmd == "json":
            json.dump(snap, sys.stdout, indent=1)
            print()
            return 0
        from .term import render
        models = [m.lower() for m in getattr(args, "models", [])]
        print(render(snap, models or None, color=False if getattr(args, "no_color", False) else None))
        return 0
    except slurm.SlurmError as e:
        print(f"gpuglance: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
