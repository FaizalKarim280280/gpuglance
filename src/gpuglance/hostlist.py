"""Expand Slurm compressed lists without calling `scontrol show hostnames`.

    expand_hostlist("gh[101-103,105],ga131") -> ["gh101", "gh102", "gh103", "gh105", "ga131"]
    expand_indices("0-3,6")                  -> [0, 1, 2, 3, 6]
"""

from __future__ import annotations

import re
from typing import List


def _split_top_level(expr: str) -> List[str]:
    """Split on commas that are not inside [...]."""
    parts, depth, cur = [], 0, []
    for ch in expr:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _expand_range(body: str) -> List[str]:
    out = []
    for piece in body.split(","):
        if "-" in piece:
            lo, hi = piece.split("-", 1)
            width = len(lo)  # keep zero padding: csr[048-050]
            out.extend(str(i).zfill(width) for i in range(int(lo), int(hi) + 1))
        elif piece:
            out.append(piece)
    return out


def expand_hostlist(expr: str) -> List[str]:
    hosts: List[str] = []
    for part in _split_top_level(expr or ""):
        m = re.match(r"^([^\[]*)\[([^\]]+)\](.*)$", part)
        if not m:
            hosts.append(part)
            continue
        prefix, body, rest = m.groups()
        for mid in _expand_range(body):
            # rest may itself hold another bracket group (rare: a[1-2]b[3-4])
            hosts.extend(expand_hostlist(prefix + mid + rest) if "[" in rest else [prefix + mid + rest])
    return hosts


def expand_indices(expr: str) -> List[int]:
    if not expr or expr.upper() == "N/A":
        return []
    return [int(x) for x in _expand_range(expr)]


def count_array_tasks(task_string: str) -> int:
    """'0-7%4' -> 8, '3,5,9-10' -> 4. Empty -> 1 (a plain job)."""
    s = (task_string or "").split("%", 1)[0]
    if not s:
        return 1
    n = 0
    for piece in s.split(","):
        if "-" in piece:
            lo, hi = piece.split("-", 1)
            hi = hi.split(":", 1)[0]  # step syntax 0-15:4 (approximate)
            n += int(hi) - int(lo) + 1
        elif piece.strip():
            n += 1
    return max(n, 1)
