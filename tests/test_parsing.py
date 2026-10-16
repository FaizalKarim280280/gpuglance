"""Run with `python tests/test_parsing.py` (or pytest). No Slurm needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpuglance.hostlist import count_array_tasks, expand_hostlist, expand_indices  # noqa: E402
from gpuglance.snapshot import build, parse_node_gres, parse_used_indices, tres_gpus  # noqa: E402


def test_hostlist():
    assert expand_hostlist("gh[101-103,105],ga131") == ["gh101", "gh102", "gh103", "gh105", "ga131"]
    assert expand_hostlist("csr[048-050]") == ["csr048", "csr049", "csr050"]
    assert expand_hostlist("") == []
    assert expand_indices("0-3,6") == [0, 1, 2, 3, 6]
    assert expand_indices("N/A") == []
    assert count_array_tasks("0-7%4") == 8
    assert count_array_tasks("3,5,9-10") == 4
    assert count_array_tasks("") == 1


def test_gres():
    assert parse_node_gres("gpu:h100:4(S:0-1)") == ("h100", 4)
    assert parse_node_gres("gpu:2g.20gb:24") == ("2g.20gb", 24)
    assert parse_node_gres("(null)") == (None, 0)
    assert parse_used_indices("gpu:a100:3(IDX:0-1,5)") == ({0, 1, 5}, 3)
    assert parse_used_indices("gpu:b200:0(IDX:N/A)") == (set(), 0)
    assert tres_gpus("cpu=8,mem=64G,gres/gpu=4,gres/gpu:h100=4") == (4, "h100")
    assert tres_gpus("cpu=8,mem=64G") == (0, None)


def _n(v):
    return {"set": True, "infinite": False, "number": v}


def test_build_slots():
    node = lambda name, used, states=("MIXED",), resv="": {  # noqa: E731
        "name": name, "gres": "gpu:h100:4", "gres_used": used, "state": list(states), "partitions": ["h100"],
        "effective_cpus": 64, "alloc_cpus": 8, "real_memory": _n(512000), "alloc_memory": _n(64000),
        "reservation": resv, "reason": "",
    }
    raw = {
        "nodes": [
            node("gh1", "gpu:h100:2(IDX:0,2)"),
            node("gh2", "gpu:h100:0(IDX:N/A)", ("IDLE", "DRAIN")),
            node("gh3", "gpu:h100:0(IDX:N/A)", ("IDLE", "RESERVED"), resv="r1"),
            {"name": "cpu1", "gres": "", "state": ["IDLE"]},
        ],
        "jobs": [{
            "job_id": 7, "name": "train", "user_name": "me", "job_state": ["RUNNING"], "partition": "h100",
            "nodes": "gh1", "gres_detail": ["gpu:h100:2(IDX:0,2)"], "tres_alloc_str": "cpu=8,gres/gpu=2,gres/gpu:h100=2",
            "start_time": _n(1000), "end_time": _n(4600), "time_limit": _n(60), "array_job_id": _n(0),
        }, {
            "job_id": 8, "name": "wait", "user_name": "other", "job_state": ["PENDING"], "partition": "h100",
            "nodes": "", "tres_req_str": "cpu=8,gres/gpu=1", "state_reason": "Priority", "array_job_id": _n(8),
            "array_task_id": {"set": False}, "array_task_string": "0-3", "start_time": _n(0),
        }],
        "reservations": [{"name": "r1", "users": "someone", "end_time": _n(9999)}],
        "partitions": ["h100"],
    }
    s = build(raw, user="me", now=2000)
    slots = {n["name"]: n["slots"] for n in s["nodes"]}
    assert slots["gh1"] == [["busy", "7"], ["free", None], ["busy", "7"], ["free", None]]
    assert [x[0] for x in slots["gh2"]] == ["down"] * 4
    assert [x[0] for x in slots["gh3"]] == ["reserved"] * 4
    h = s["summary"]["models"]["h100"]
    assert (h["total"], h["free"], h["busy"], h["down"], h["reserved"]) == (12, 2, 2, 4, 4)
    assert h["queued_gpus"] == 4 and h["next_free"] == 4600 and h["free_nodes"] == [["gh1", 2]]
    assert s["jobs"][0]["mine"] and s["jobs"][1]["id"] == "8_[0-3]"


def test_jobinfo_parsers():
    from gpuglance.jobinfo import JOB_ID_RE, clean_log, parse_bytes, parse_duration, parse_tres

    assert parse_duration("1-02:03:04") == 93784
    assert parse_duration("25:08") == 1508
    assert abs(parse_duration("00:03.500") - 3.5) < 1e-9
    assert parse_bytes("23217484K") == 23217484 * 1024 and parse_bytes("2G") == 2 * 1024 ** 3
    assert parse_tres("cpu=00:25:08,fs/disk=37,mem=18G") == {"cpu": "00:25:08", "fs/disk": "37", "mem": "18G"}
    assert clean_log(b"step 1\rstep 2\rstep 3\r\nok \x1b[32mgreen\x1b[0m\n") == ["step 3", "ok green", ""]
    for good in ("1114856", "123_4", "123_[0-7%4]"):
        assert JOB_ID_RE.match(good)
    for bad in ("1;rm -rf", "../etc", "", "12 3"):
        assert not JOB_ID_RE.match(bad)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
