"""Details for one of *your* jobs: metadata, batch script, usage, log tail, live GPU/process probe.

Everything here runs only when you click a job in the page, never on the 30 s refresh.
Costs (measured on skipjack): scontrol ~0.05 s, batch script ~0.01 s, sstat ~0.2 s,
live probe (srun --overlap into the job) ~0.2 s. Each live probe adds one short finished
step (e.g. 1234.5 "bash") to the job's sacct history.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .hostlist import expand_hostlist, expand_indices
from .snapshot import job_display_id, num, tres_gpus

JOB_ID_RE = re.compile(r"^(\d+)(?:_(\d+|\[[0-9,\-%:]+\]))?$")
LOG_TAIL_BYTES = 96 * 1024
LOG_TAIL_LINES = 120
SCRIPT_MAX_BYTES = 200 * 1024


class JobAccessError(Exception):
    """Unknown job, or not the server owner's job."""


def _run(argv: List[str], timeout: float = 20) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _slurm_id(job_id: str) -> str:
    """'123_4' stays as is (scontrol understands it); a pending array record '123_[0-7]' becomes '123'."""
    m = JOB_ID_RE.match(job_id or "")
    if not m:
        raise JobAccessError("bad job id")
    return job_id if m.group(2) and m.group(2).isdigit() else m.group(1)


def _load_job(job_id: str, user: str) -> Dict[str, Any]:
    rc, out, err = _run(["scontrol", "show", "job", _slurm_id(job_id), "--json"])
    if rc != 0:
        raise JobAccessError(err.strip() or "job not found (it may have finished)")
    jobs = json.loads(out).get("jobs", [])
    if not jobs:
        raise JobAccessError("job not found (it may have finished)")
    j = jobs[0]
    if j.get("user_name") != user:
        raise JobAccessError("only your own jobs can be opened")
    return j


# ---------------------------------------------------------------- parsing helpers

def parse_duration(s: str) -> float:
    """Slurm durations: 'DD-HH:MM:SS', 'HH:MM:SS', 'MM:SS.mmm'."""
    s = (s or "").strip()
    if not s:
        return 0.0
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + sec


def parse_bytes(s: str) -> float:
    m = re.match(r"^([\d.]+)([KMGTP]?)$", (s or "").strip())
    if not m:
        return 0.0
    return float(m.group(1)) * 1024 ** "_KMGTP".index(m.group(2) or "_")


def parse_tres(s: str) -> Dict[str, str]:
    out = {}
    for seg in (s or "").split(","):
        if "=" in seg:
            k, v = seg.split("=", 1)
            out[k] = v
    return out


def usage(job_numeric_id: int) -> Optional[Dict[str, Any]]:
    """CPU seconds, memory and disk I/O summed over the job's steps (sstat; running jobs only)."""
    rc, out, _ = _run(["sstat", "-a", "-j", str(job_numeric_id), "-P", "--noheader",
                       "--format=JobID,TRESUsageInTot,TRESUsageInMax,TRESUsageInAve,TRESUsageOutTot"])
    if rc != 0 or not out.strip():
        return None
    cpu = mem_max = mem_ave = read = written = 0.0
    steps = 0
    for line in out.strip().splitlines():
        cols = line.split("|")
        if len(cols) < 5 or cols[0].endswith(".extern"):
            continue
        tot, mx, ave, outt = (parse_tres(c) for c in cols[1:5])
        steps += 1
        cpu += parse_duration(tot.get("cpu", ""))
        mem_max += parse_bytes(mx.get("mem", ""))
        mem_ave += parse_bytes(ave.get("mem", ""))
        read += float(tot.get("fs/disk", 0) or 0)
        written += float(outt.get("fs/disk", 0) or 0)
    if not steps:
        return None
    return {"cpu_seconds": cpu, "mem_peak": mem_max, "mem_avg": mem_ave,
            "disk_read": read, "disk_written": written, "steps": steps}


def clean_log(raw: bytes) -> List[str]:
    """Decode, drop ANSI colour codes, keep only the final state of \\r-redrawn lines (tqdm)."""
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return [ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in text.split("\n")]


def tail_file(path: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": path}
    try:
        st = os.stat(path)
        info["size"] = st.st_size
        info["mtime"] = st.st_mtime
        with open(path, "rb") as f:
            if st.st_size > LOG_TAIL_BYTES:
                f.seek(st.st_size - LOG_TAIL_BYTES)
                data = f.read()
                data = data[data.find(b"\n") + 1:]  # drop the partial first line
                info["truncated"] = True
            else:
                data = f.read()
        lines = clean_log(data)
        while lines and not lines[-1].strip():
            lines.pop()
        info["lines"] = lines[-LOG_TAIL_LINES:]
    except OSError as e:
        info["error"] = e.strerror or str(e)
    return info


# ---------------------------------------------------------------- public API

def details(job_id: str, user: str) -> Dict[str, Any]:
    j = _load_job(job_id, user)
    state = (j.get("job_state") or ["?"])[0]
    running = state == "RUNNING"
    jid = int(j["job_id"])
    hosts = expand_hostlist(j.get("nodes", "")) if running else []
    gpu_idx = OrderedDict()
    for host, detail in zip(hosts, j.get("gres_detail") or []):
        m = re.search(r"IDX:([^)]+)", detail)
        gpu_idx[host] = expand_indices(m.group(1)) if m else []
    tres = j.get("tres_alloc_str") if running else j.get("tres_req_str")
    gpus, model = tres_gpus(tres or "")
    limit = num(j.get("time_limit"))
    mem_node = num(j.get("memory_per_node"))
    mem_cpu = num(j.get("memory_per_cpu"))
    cpus = int(num(j.get("cpus")) or 0)
    nnodes = int(num(j.get("node_count")) or 1)
    mem_alloc = (mem_node * nnodes if mem_node else (mem_cpu or 0) * cpus) * 1024 * 1024
    start = num(j.get("start_time")) or None

    out: Dict[str, Any] = {
        "id": job_display_id(j),
        "job_id": jid,
        "name": j.get("name", ""),
        "state": state,
        "reason": j.get("state_reason", ""),
        "user": j.get("user_name", ""),
        "account": j.get("account", ""),
        "qos": j.get("qos", ""),
        "partition": j.get("partition", ""),
        "priority": num(j.get("priority")),
        "nodes": hosts,
        "gpu_idx": gpu_idx,
        "gpus": gpus,
        "gpu_model": model,
        "cpus": cpus,
        "mem_alloc": mem_alloc,
        "submit": num(j.get("submit_time")),
        "start": start,
        "end": num(j.get("end_time")) or None,
        "limit": limit * 60 if limit else None,
        "est_start": start if (state == "PENDING" and start and start > time.time() - 60) else None,
        "submit_line": j.get("submit_line", ""),
        "command": j.get("command", ""),
        "workdir": j.get("current_working_directory", ""),
        "dependency": j.get("dependency", ""),
        "array": j.get("array_task_string", ""),
        "batch": bool(j.get("batch_flag")),
        "generated_at": time.time(),
    }

    if out["batch"]:
        rc, script, err = _run(["scontrol", "write", "batch_script", _slurm_id(job_id), "-"], timeout=10)
        out["script"] = script[:SCRIPT_MAX_BYTES] if rc == 0 else None

    logs = []
    for kind in ("stdout", "stderr"):
        path = j.get(f"{kind}_expanded") or ""
        if path and path != "/dev/null" and all(path != l["path"] for l in logs):
            info = tail_file(path) if state != "PENDING" else {"path": path, "lines": []}
            info["kind"] = kind
            logs.append(info)
    out["logs"] = logs

    if running:
        out["usage"] = usage(jid)
    return out


PROBE = r"""
echo "##NODE $(hostname -s)"
nvidia-smi --query-gpu=pci.bus_id,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | sed 's/^/##GPU /'
for p in $(pgrep -u "$(id -un)"); do
  c=$(cat /proc/$p/cgroup 2>/dev/null) || continue
  case "$c" in
    *job_${SLURM_JOB_ID}/step_${SLURM_STEP_ID}/*|*job_${SLURM_JOB_ID}/step_extern/*) ;;
    *job_${SLURM_JOB_ID}/*) ps -o pcpu=,rss=,etimes=,args= -p "$p" 2>/dev/null | sed 's/^/##PROC /' ;;
  esac
done
"""


def live(job_id: str, user: str) -> Dict[str, Any]:
    """GPU utilization / memory / power and the processes running inside the job, per node."""
    j = _load_job(job_id, user)
    if (j.get("job_state") or [""])[0] != "RUNNING":
        return {"nodes": [], "note": "job is not running"}
    nnodes = int(num(j.get("node_count")) or 1)
    hosts = expand_hostlist(j.get("nodes", ""))
    idx_by_host = {}
    for host, detail in zip(hosts, j.get("gres_detail") or []):
        m = re.search(r"IDX:([^)]+)", detail)
        idx_by_host[host] = expand_indices(m.group(1)) if m else []
    t0 = time.time()
    rc, out, err = _run(["srun", f"--jobid={j['job_id']}", "--overlap", "--quiet", f"-N{nnodes}",
                         "--ntasks-per-node=1", "--cpus-per-task=1", "--mem=0", "--time=1",
                         "bash", "-c", PROBE], timeout=45)
    nodes: Dict[str, Dict[str, Any]] = OrderedDict()
    cur = None
    for line in out.splitlines():
        if line.startswith("##NODE "):
            cur = nodes.setdefault(line[7:].strip(), {"gpus": [], "procs": {}})
        elif cur is None:
            continue
        elif line.startswith("##GPU "):
            f = [x.strip() for x in line[6:].split(",")]
            if len(f) >= 7:
                def fnum(x: str) -> Optional[float]:
                    try:
                        return float(x)
                    except ValueError:
                        return None
                cur["gpus"].append({"bus": f[0], "name": f[1], "util": fnum(f[2]), "mem_used": fnum(f[3]),
                                    "mem_total": fnum(f[4]), "power": fnum(f[5]), "temp": fnum(f[6])})
        elif line.startswith("##PROC "):
            parts = line[7:].split(None, 3)
            if len(parts) == 4:
                pcpu, rss, et, args = parts
                g = cur["procs"].setdefault(args[:400], {"args": args[:400], "count": 0, "pcpu": 0.0, "rss": 0, "etime": 0})
                g["count"] += 1
                g["pcpu"] += float(pcpu)
                g["rss"] += int(rss) * 1024
                g["etime"] = max(g["etime"], int(et))
    result = []
    for host, d in nodes.items():
        idx = idx_by_host.get(host, [])
        for k, g in enumerate(d["gpus"]):  # nvidia-smi inside the job sees only its GPUs, in bus order
            g["index"] = idx[k] if k < len(idx) else k
        procs = sorted(d["procs"].values(), key=lambda p: (-p["pcpu"], -p["rss"]))
        result.append({"host": host, "gpus": d["gpus"], "procs": procs[:12]})
    res: Dict[str, Any] = {"nodes": result, "seconds": round(time.time() - t0, 2), "at": time.time()}
    if rc != 0 and not result:
        res["error"] = (err.strip().splitlines() or ["srun failed"])[-1][:300]
    return res
