"""Turn raw Slurm JSON (slurm.collect) into the compact snapshot the page and CLI use.

Pure functions only: no subprocess calls here.

Each GPU on each node becomes one slot with a state:
    busy      allocated to a job (slot carries the job id)
    free      schedulable now
    reserved  idle, but the node is in a reservation you are not part of
    stranded  idle GPU, but the node has no CPUs or memory left to go with it
    down      idle GPU on a drained / down / unresponsive node
"""

from __future__ import annotations

import getpass
import os
import re
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .hostlist import count_array_tasks, expand_hostlist, expand_indices

# Pending reasons that mean "waiting for GPUs" (real competition for free GPUs).
# Everything else (held, dependency, account limits, ...) is "blocked".
WAITING_REASONS = {"Priority", "Resources", "None"}

DOWN_FLAGS = {"DOWN", "NOT_RESPONDING", "FAIL", "FAILING", "POWERED_DOWN", "POWERING_DOWN", "POWER_DOWN", "REBOOT_ISSUED"}
DRAIN_FLAGS = {"DRAIN", "DRAINING", "DRAINED"}

# Display names and memory for GPU models seen in GRES strings. Unknown models
# fall back to upper-case and no memory, so other clusters still work.
KNOWN_MODELS: Dict[str, Tuple[str, str]] = {
    "b300": ("B300", "288 GB"),
    "b200": ("B200", "180 GB"),
    "gb200": ("GB200", "186 GB"),
    "h200": ("H200", "141 GB"),
    "h100": ("H100", "80–94 GB"),
    "a100": ("A100", "80 GB"),
    "l40s": ("L40S", "48 GB"),
    "l40": ("L40", "48 GB"),
    "a6000": ("RTX A6000", "48 GB"),
    "rtx_pro_6000": ("RTX PRO 6000", "96 GB"),
    "v100": ("V100", "32 GB"),
}
MODEL_ORDER = ["gb200", "b300", "b200", "h200", "h100", "a100", "l40s", "l40", "rtx_pro_6000", "a6000", "v100"]


def num(x: Any) -> Optional[float]:
    """Slurm JSON wraps numbers as {"set": bool, "infinite": bool, "number": n}."""
    if isinstance(x, dict):
        if not x.get("set", True) or x.get("infinite"):
            return None
        return x.get("number")
    return x


def model_info(model: str) -> Dict[str, Any]:
    m = re.match(r"^(\d+)g\.(\d+)gb$", model)
    if m:  # MIG slice such as 2g.20gb
        return {"label": f"MIG {m.group(1)}g", "mem": f"{m.group(2)} GB", "mig": True}
    label, mem = KNOWN_MODELS.get(model, (model.replace("_", " ").upper(), ""))
    return {"label": label, "mem": mem, "mig": False}


def model_sort_key(model: str) -> Tuple[int, str]:
    if model in MODEL_ORDER:
        return (MODEL_ORDER.index(model), model)
    return (len(MODEL_ORDER) + (1 if re.match(r"^\d+g\.", model) else 0), model)


# ---------------------------------------------------------------- parsing

_GRES_RE = re.compile(r"gpu(?::([^:,()]+))?:(\d+)")


def parse_node_gres(gres: str) -> Tuple[Optional[str], int]:
    """'gpu:h100:4(S:0-1)' -> ('h100', 4). Multiple GPU types: first model, summed count."""
    model, total = None, 0
    for m in _GRES_RE.finditer(gres or ""):
        if model is None:
            model = (m.group(1) or "gpu").lower()
        total += int(m.group(2))
    return model, total


def parse_used_indices(gres_used: str) -> Tuple[Set[int], int]:
    """'gpu:h100:3(IDX:0-1,3)' -> ({0,1,3}, 3)."""
    idx: Set[int] = set()
    count = 0
    for m in re.finditer(r"gpu(?::[^:,()]+)?:(\d+)\(IDX:([^)]*)\)", gres_used or ""):
        count += int(m.group(1))
        idx.update(expand_indices(m.group(2)))
    return idx, count


def tres_gpus(tres: str) -> Tuple[int, Optional[str]]:
    """'cpu=8,...,gres/gpu=4,gres/gpu:h100=4' -> (4, 'h100')."""
    total, model = 0, None
    for seg in (tres or "").split(","):
        seg = seg.strip()
        if seg.startswith("gres/gpu="):
            total = int(seg.split("=", 1)[1] or 0)
        elif seg.startswith("gres/gpu:"):
            model = seg[len("gres/gpu:"):].split("=", 1)[0].lower()
    return total, model


def tres_value(tres: str, key: str) -> str:
    for seg in (tres or "").split(","):
        if seg.startswith(key + "="):
            return seg.split("=", 1)[1]
    return ""


def mem_to_mb(s: str) -> int:
    m = re.match(r"^([\d.]+)([KMGT]?)$", s or "")
    if not m:
        return 0
    v = float(m.group(1))
    return int(v * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024, "": 1}[m.group(2)])


def node_health(states: Iterable[str]) -> str:
    st = set(states)
    if st & DOWN_FLAGS:
        return "down"
    if st & DRAIN_FLAGS:
        return "drain"
    return "ok"


def job_display_id(j: Dict[str, Any]) -> str:
    arr = num(j.get("array_job_id")) or 0
    task = num(j.get("array_task_id"))
    if arr and task is not None:
        return f"{arr}_{task}"
    if arr and j.get("array_task_string"):
        return f"{arr}_[{j['array_task_string']}]"
    return str(j.get("job_id"))


# ---------------------------------------------------------------- build

def build(raw: Dict[str, Any], user: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    user = user or os.environ.get("USER") or getpass.getuser()
    reservations = {r.get("name"): r for r in raw.get("reservations", [])}

    # --- nodes with GPUs
    nodes: List[Dict[str, Any]] = []
    node_by_name: Dict[str, Dict[str, Any]] = {}
    part_models: Dict[str, Set[str]] = defaultdict(set)
    for n in raw.get("nodes", []):
        model, gpus = parse_node_gres(n.get("gres", ""))
        if not gpus:
            continue
        states = n.get("state") or []
        used_idx, used_count = parse_used_indices(n.get("gres_used", ""))
        if len(used_idx) < used_count:  # IDX missing: assume the first k are used
            used_idx |= set(range(used_count))
        cpus = int(n.get("effective_cpus") or n.get("cpus") or 0)
        mem = int(num(n.get("real_memory")) or 0)
        resv_name = n.get("reservation") or ""
        resv = reservations.get(resv_name, {})
        resv_users = {u for u in (resv.get("users") or "").split(",") if u}
        node = {
            "name": n["name"],
            "model": model,
            "gpus": gpus,
            "partitions": n.get("partitions") or [],
            "states": states,
            "health": node_health(states),
            "planned": "PLANNED" in states,
            "reason": n.get("reason") or "",
            "reservation": resv_name,
            "reservation_end": num(resv.get("end_time")) if resv else None,
            "reserved_for_me": bool(resv_name) and user in resv_users,
            "cpus": cpus,
            "cpus_alloc": int(n.get("alloc_cpus") or 0),
            "mem_mb": mem,
            "mem_alloc_mb": int(num(n.get("alloc_memory")) or 0),
            "cpu_load": (num(n.get("cpu_load")) or 0) / 100.0,
            "_used": used_idx,
            "slots": [],
        }
        nodes.append(node)
        node_by_name[node["name"]] = node
        for p in node["partitions"]:
            part_models[p].add(model)

    # --- jobs that want GPUs
    jobs: List[Dict[str, Any]] = []
    slot_owner: Dict[Tuple[str, int], str] = {}
    cluster = ""
    for j in raw.get("jobs", []):
        cluster = cluster or j.get("cluster", "")
        state = (j.get("job_state") or ["?"])[0]
        if state not in ("RUNNING", "PENDING"):
            continue
        running = state == "RUNNING"
        tres = j.get("tres_alloc_str") if running else j.get("tres_req_str")
        gpus, req_model = tres_gpus(tres or "")
        if not gpus:
            continue
        jid = job_display_id(j)
        hosts = expand_hostlist(j.get("nodes", "")) if running else []
        alloc: Dict[str, List[int]] = {}
        for host, detail in zip(hosts, j.get("gres_detail") or []):
            m = re.search(r"IDX:([^)]+)", detail)
            idx = expand_indices(m.group(1)) if m else []
            alloc[host] = idx
            for i in idx:
                slot_owner[(host, i)] = jid
        partitions = [p for p in (j.get("partition") or "").split(",") if p]
        if running and hosts and hosts[0] in node_by_name:
            models = [node_by_name[hosts[0]]["model"]]
        elif req_model:
            models = [req_model]
        else:
            models = sorted({m for p in partitions for m in part_models.get(p, ())}, key=model_sort_key)
        tasks = 1 if running else count_array_tasks(j.get("array_task_string", ""))
        limit_min = num(j.get("time_limit"))
        start = num(j.get("start_time")) or None
        reason = j.get("state_reason") or ""
        jobs.append({
            "id": jid,
            "name": j.get("name", ""),
            "user": j.get("user_name", ""),
            "account": j.get("account", ""),
            "qos": j.get("qos", ""),
            "state": "R" if running else "PD",
            "reason": reason,
            "waiting": (not running) and reason in WAITING_REASONS,
            "models": models,
            "partitions": partitions,
            "gpus": gpus,
            "tasks": tasks,
            "nodes": hosts,
            "node_count": int(num(j.get("node_count")) or len(hosts) or 1),
            "alloc": alloc,
            "cpus": int(num(j.get("cpus")) or 0),
            "mem": tres_value(tres or "", "mem"),
            "submit": num(j.get("submit_time")),
            "start": start,
            "end": (num(j.get("end_time")) or None) if running else None,
            "limit": limit_min * 60 if limit_min else None,
            # For pending jobs Slurm's backfill writes its start estimate into start_time.
            "est_start": start if (not running and start and start > now - 60) else None,
            "mine": j.get("user_name") == user,
        })

    # --- slots
    for node in nodes:
        free_cpus = node["cpus"] - node["cpus_alloc"]
        free_mem = node["mem_mb"] - node["mem_alloc_mb"]
        stranded = free_cpus <= 0 or free_mem < 1024
        for i in range(node["gpus"]):
            if i in node["_used"]:
                node["slots"].append(["busy", slot_owner.get((node["name"], i))])
            elif node["health"] != "ok":
                node["slots"].append(["down", None])
            elif node["reservation"] and not node["reserved_for_me"]:
                node["slots"].append(["reserved", None])
            elif stranded:
                node["slots"].append(["stranded", None])
            else:
                node["slots"].append(["free", None])
        del node["_used"]

    nodes.sort(key=lambda n: (model_sort_key(n["model"]), n["name"]))
    models = sorted({n["model"] for n in nodes}, key=model_sort_key)
    summary = summarize(nodes, jobs, models, now)

    return {
        "cluster": cluster,
        "generated_at": now,
        "user": user,
        "partitions": raw.get("partitions", []),
        "models": [dict(key=m, **model_info(m)) for m in models],
        "nodes": nodes,
        "jobs": jobs,
        "summary": summary,
    }


def summarize(nodes: List[Dict[str, Any]], jobs: List[Dict[str, Any]], models: List[str], now: float) -> Dict[str, Any]:
    per: Dict[str, Dict[str, Any]] = {}
    for m in models:
        per[m] = {k: 0 for k in ("total", "free", "busy", "reserved", "stranded", "down", "nodes",
                                 "queued_gpus", "queued_jobs", "blocked_gpus", "mine_gpus")}
        per[m].update({"free_nodes": [], "next_free": None})
    for n in nodes:
        s = per[n["model"]]
        s["nodes"] += 1
        s["total"] += n["gpus"]
        free_here = 0
        for state, _ in n["slots"]:
            s[state] += 1
            free_here += state == "free"
        if free_here:
            s["free_nodes"].append([n["name"], free_here])
    for s in per.values():
        s["free_nodes"].sort(key=lambda x: (-x[1], x[0]))
    for j in jobs:
        for m in j["models"]:
            if m not in per:
                continue
            s = per[m]
            if j["state"] == "R":
                if j["end"] and (s["next_free"] is None or j["end"] < s["next_free"]):
                    s["next_free"] = j["end"]
                if j["mine"]:
                    s["mine_gpus"] += j["gpus"]
            elif j["waiting"]:
                s["queued_gpus"] += j["gpus"] * j["tasks"]
                s["queued_jobs"] += j["tasks"]
            else:
                s["blocked_gpus"] += j["gpus"] * j["tasks"]
    total = {k: sum(per[m][k] for m in models) for k in ("total", "free", "busy", "reserved", "stranded", "down", "nodes")}
    waiting = [j for j in jobs if j["waiting"]]
    total["queued_gpus"] = sum(j["gpus"] * j["tasks"] for j in waiting)
    total["queued_jobs"] = sum(j["tasks"] for j in waiting)
    return {"models": per, "total": total}
