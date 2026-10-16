# gpuglance

See a Slurm cluster's GPUs at a glance: which GPUs are free right now, where they are, and when busy ones free up.
It has a web dashboard and a terminal summary, and uses only the Python standard library (3.8+). It reads `scontrol` / `squeue` JSON, so it works on any Slurm 21.08+ cluster without configuration.

## Run

```bash
# without installing (from this folder)
PYTHONPATH=src python -m gpuglance serve

# or install once, then run from anywhere
pip install --user -e .          # puts `gpuglance` in ~/.local/bin
gpuglance serve                  # web page on http://localhost:8765
gpuglance                        # terminal summary
gpuglance status h100 a100       # only these models
gpuglance json                   # the snapshot, for scripts
```

Viewing the page: VS Code Remote forwards the port automatically (Ports tab).
From a laptop terminal, run `ssh -L 8765:localhost:8765 <login-node>` and open http://localhost:8765.
The server binds to `127.0.0.1`, so only you (through forwarding) can reach it.

The page updates every 30s (it checks every 5s and redraws when the server has a new Slurm query).
The server stops querying Slurm 2 minutes after the last page closes.

Options: `--port`, `--interval` (seconds between Slurm queries, default 30), `--partitions a100,h100`
(default: every partition with GPU nodes, or `$GPUGLANCE_PARTITIONS`).

## What the page shows

| Section | Question it answers |
|---|---|
| Big number + bar | How many GPUs are free right now, and what the rest are doing |
| Your jobs | Time left on your running jobs, and estimated starts for your queued ones |
| Type cards / chips | Free GPUs per model, the node with the most free GPUs (matters for multi-GPU jobs), when the next one frees, and the queue size. Click a card to filter the whole page. |
| Nodes | One square per GPU. Green = free. Blue = busy, lighter when it frees sooner. Hatched = reserved, idle on a full node, or down. Hover for the job / node details. |
| When GPUs free up | Cumulative GPUs released by running jobs over the next 6h / 24h / 3d, with the queue size as a reference line |
| Jobs | Running jobs sorted by soonest end. Queued jobs with Slurm's estimated start. Filter by user, job or node. |

Caveats: "ends in" uses each job's **time limit**, so jobs often finish earlier. "Queued" counts only jobs waiting
on Priority/Resources, and a job that accepts several GPU types counts toward each.

## Layout

```
src/gpuglance/
  slurm.py      run scontrol/squeue --json (the only code that touches Slurm)
  snapshot.py   raw JSON -> per-GPU slot states, jobs, per-model summary (pure, testable)
  hostlist.py   expand gh[101-103] and IDX:0-3 strings
  server.py     stdlib HTTP server; queries Slurm every --interval s while a page is open (one query for all tabs)
  term.py       terminal summary
  cli.py        `gpuglance` entry point
  static/       index.html, style.css, app.js (vanilla JS, no build step)
tests/test_parsing.py
```

Offline development: `gpuglance dump-raw /tmp/raw` saves Slurm's JSON, then
`gpuglance serve --raw-dir /tmp/raw` serves it without touching Slurm.
