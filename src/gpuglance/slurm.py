"""Thin I/O layer: run Slurm commands and return their raw JSON.

Everything else (snapshot.py) is pure and works on these dicts, so it can be
tested from saved files (`gpuglance dump-raw`, `--raw-dir`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class SlurmError(RuntimeError):
    pass


def _run_json(argv: List[str], timeout: float = 60) -> Dict[str, Any]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise SlurmError(f"{argv[0]} not found: is this a Slurm cluster node?") from e
    except subprocess.TimeoutExpired as e:
        raise SlurmError(f"{' '.join(argv)} timed out after {timeout}s") from e
    if p.returncode != 0:
        raise SlurmError(f"{' '.join(argv)} failed: {p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


def fetch_nodes() -> List[Dict[str, Any]]:
    return _run_json(["scontrol", "show", "node", "--json"]).get("nodes", [])


def fetch_reservations() -> List[Dict[str, Any]]:
    try:
        return _run_json(["scontrol", "show", "reservation", "--json"]).get("reservations", [])
    except SlurmError:
        return []  # reservations are decoration; never fail the page over them


def fetch_jobs(partitions: List[str]) -> List[Dict[str, Any]]:
    argv = ["squeue", "--json", "-t", "RUNNING,PENDING"]
    if partitions:
        argv += ["-p", ",".join(partitions)]
    return _run_json(argv, timeout=120).get("jobs", [])


def gpu_partitions(nodes: List[Dict[str, Any]]) -> List[str]:
    """Every partition that contains at least one node with GPU GRES."""
    parts = set()
    for n in nodes:
        if "gpu" in (n.get("gres") or ""):
            parts.update(n.get("partitions") or [])
    return sorted(parts)


def collect(partitions: Optional[List[str]] = None, raw_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return {"nodes", "jobs", "reservations", "partitions"} as raw Slurm JSON.

    raw_dir: read nodes.json / jobs.json / reservations.json from a folder
    instead of calling Slurm (offline development and tests).
    """
    if raw_dir:
        d = Path(raw_dir)
        load = lambda name, key: json.loads((d / name).read_text()).get(key, [])  # noqa: E731
        nodes = load("nodes.json", "nodes")
        res_path = d / "reservations.json"
        reservations = json.loads(res_path.read_text()).get("reservations", []) if res_path.exists() else []
        jobs = load("jobs.json", "jobs")
        parts = partitions or gpu_partitions(nodes)
        return {"nodes": nodes, "jobs": jobs, "reservations": reservations, "partitions": parts}

    env_parts = os.environ.get("GPUGLANCE_PARTITIONS", "").strip()
    if partitions is None and env_parts:
        partitions = [p for p in env_parts.split(",") if p]
    nodes = fetch_nodes()
    parts = partitions or gpu_partitions(nodes)
    return {
        "nodes": nodes,
        "jobs": fetch_jobs(parts),
        "reservations": fetch_reservations(),
        "partitions": parts,
    }


def dump_raw(out_dir: str, partitions: Optional[List[str]] = None) -> None:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    raw = collect(partitions)
    (d / "nodes.json").write_text(json.dumps({"nodes": raw["nodes"]}))
    (d / "jobs.json").write_text(json.dumps({"jobs": raw["jobs"]}))
    (d / "reservations.json").write_text(json.dumps({"reservations": raw["reservations"]}))
