"""`gpuglance status`: the same summary as the web page, in the terminal."""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

ANSI = {"free": "32", "busy": "34", "reserved": "35", "stranded": "90", "down": "31"}
GLYPH = {"free": "█", "busy": "█", "reserved": "▒", "stranded": "░", "down": "×"}


def fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _paint(text: str, state: str, color: bool) -> str:
    return f"\033[{ANSI[state]}m{text}\033[0m" if color else text


def bar(counts: Dict[str, int], total: int, width: int, color: bool) -> str:
    """Proportional, so every model's bar has the same width."""
    order = ["free", "busy", "reserved", "stranded", "down"]
    scale = width / total if total else 0
    out = []
    for st in order:
        n = counts.get(st, 0)
        k = round(n * scale) if n else 0
        if n and k == 0:
            k = 1
        out.append(_paint(GLYPH[st] * k, st, color))
    return "".join(out)


def render(snap: Dict[str, Any], models: Optional[List[str]] = None, color: Optional[bool] = None) -> str:
    color = sys.stdout.isatty() if color is None else color
    now = snap["generated_at"]
    summ = snap["summary"]
    lines = []
    tot = summ["total"]
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    lines.append(f"{snap['cluster'] or 'cluster'} · {tot['free']} of {tot['total']} GPUs free · "
                 f"{tot['queued_gpus']} GPUs queued · {stamp}")
    lines.append("")
    hdr = f"{'GPU':<14}{'free':>9}  {'':<34}{'best node':<14}{'next frees':>11}{'queued':>8}"
    lines.append(hdr)
    for m in snap["models"]:
        key = m["key"]
        if models and key not in models and m["label"].lower() not in models:
            continue
        s = summ["models"][key]
        best = f"{s['free_nodes'][0][0]} ×{s['free_nodes'][0][1]}" if s["free_nodes"] else "-"
        nxt = "now" if s["free"] else (fmt_dur(s["next_free"] - now) if s["next_free"] else "-")
        free_txt = f"{s['free']}/{s['total']}"
        lines.append(f"{m['label']:<14}{free_txt:>9}  {bar(s, s['total'], 32, color):<32}  "
                     f"{best:<14}{nxt:>11}{s['queued_gpus']:>8}")
    legend = "  ".join(_paint(GLYPH[k], k, color) + " " + k for k in ANSI)
    lines.append("")
    lines.append(legend + "   (next frees = latest end by time limit)")

    mine = [j for j in snap["jobs"] if j["mine"]]
    if mine:
        lines.append("")
        lines.append(f"your jobs ({snap['user']}):")
        for j in sorted(mine, key=lambda j: (j["state"] != "R", j["end"] or 0)):
            label = next((x["label"] for x in snap["models"] if x["key"] == (j["models"] or [""])[0]), "?")
            if j["state"] == "R":
                where = ",".join(j["nodes"])
                lines.append(f"  {j['id']:<12} {j['name'][:22]:<22} {j['gpus']}× {label:<10} {where:<10} "
                             f"ends in {fmt_dur(j['end'] - now) if j['end'] else '-'}")
            else:
                est = f"starts ~{fmt_dur(j['est_start'] - now)}" if j["est_start"] else j["reason"]
                lines.append(f"  {j['id']:<12} {j['name'][:22]:<22} {j['gpus']}× {label:<10} {'queued':<10} {est}")
    return "\n".join(lines)
