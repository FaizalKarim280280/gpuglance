# gpuglance

Check the cluster's GPUs before you submit a job: which GPUs are free right now, on which nodes, and when the
busy ones will free up. There is a web dashboard and a terminal summary.

It reads the same information `squeue` and `scontrol` give every user, and needs no setup: GPU partitions are found
automatically. It uses only the Python standard library, so any Python 3.8+ works, including the cluster's system `python3`.

## Install

On the cluster (login node or any compute node):

```bash
/usr/bin/python3 -m pip install --user git+https://github.com/FaizalKarim280280/gpuglance.git
gpuglance
```

The second command prints a summary like this:

```
skipjack · 83 of 652 GPUs free · 903 GPUs queued · 18:31:30

GPU                free                                    best node      next frees  queued
B200              8/128  ██████████████████████████████▒▒  gb213 ×3              now     184
H100              0/128  ████████████████████████████████  -                     10m     193
A100             13/104  ███████████████████████████████░  ga134 ×4              now     133
...
```

Notes:

- Installing with the system Python (`/usr/bin/python3`) keeps `gpuglance` working whichever conda environment is
  active. Any other Python 3.8+ works too.
- `--user` installs into your home directory (`~/.local`), so no admin rights are needed.
- **`gpuglance: command not found`?** `~/.local/bin` is not on your `PATH`. The cluster's default `~/.bashrc` adds it.
  If you use zsh or a custom `~/.bashrc`, add this line to it, then open a new shell:

  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  ```

## Use it in the terminal

```bash
gpuglance                  # all GPU types, plus your own jobs
gpuglance status h100 a100 # only these types
gpuglance json             # everything as JSON, for your own scripts
```

## Open the web dashboard

The dashboard runs on the cluster and you view it in your laptop's browser. Pick one of these two ways.

**A. VS Code (Remote-SSH).** If VS Code is connected to the cluster, run this in its terminal:

```bash
gpuglance serve
```

It prints a link like `http://localhost:8765/?token=…`. Ctrl/Cmd-click it: VS Code forwards the port and opens it in
your browser.

**B. Any terminal on your laptop.** One command starts the dashboard and connects your laptop to it:

```bash
ssh -t -L 8421:localhost:8421 <you>@<login-address> 'bash -lc "gpuglance serve --port 8421"'
```

Then open the `http://localhost:8421/?token=…` link it prints. Replace `<you>@<login-address>` with what you normally
`ssh` to, and `8421` with any number from 8000 to 9999. Press Ctrl-C in that terminal when you're done; it stops both.

**About ports.** Login nodes are shared, so another user may already be using your port number. When that happens,
`gpuglance serve` moves to the next free port and prints the port it actually used. With option B, the browser would
then reach the wrong server (possibly someone else's dashboard), so press Ctrl-C and pick another number. The
overview only shows what `squeue` already shows every user.

**About the token.** The `?token=…` part of the link unlocks the details of your own jobs (logs, batch scripts,
processes). Without it, the overview still works but your job details stay locked, so other users on the same node
can't read your logs through your server. The token is stored in `~/.config/gpuglance/token`, readable only by you,
and stays the same across restarts, so you can bookmark the link. Your browser remembers it after the first visit.
To change it, delete that file and restart the server.

The page updates itself every 30 seconds. It's fine to leave the server running in `tmux`: it only queries Slurm
while a dashboard tab is open, and stops 2 minutes after the last one closes.

## Reading the dashboard

| Part | What it tells you |
|---|---|
| Big number and bar | GPUs free right now, and what all the others are doing |
| Your jobs | Time left for each of your running jobs, and Slurm's start estimate for your queued ones. **Click one of your jobs** (here, in the node map or in the job list) for its details panel, described below. |
| One card per GPU type | Free / total; **best node**, the node with the most free GPUs (what matters for a multi-GPU job); **next frees**, when the next busy GPU is released; and how many GPUs queued jobs are waiting for. Click a card (or a chip at the top) to filter the whole page to that type. |
| Nodes | One square per GPU, shown below. The plum bar under each node shows how much of its CPU is allocated (a full bar means no CPUs left for a new job). Hover a square to see the job, user and time left; the job's other GPUs light up. Hover a node for exact CPU and memory numbers. |
| When GPUs free up | How many GPUs running jobs will have released over the next 6 hours, 24 hours or 3 days |
| Jobs | Running jobs, soonest to finish first, and queued jobs with estimated start. Type in the filter box to search by user, job name or node. |

GPU squares in the node map:

| Square | Meaning |
|---|---|
| Green | Free: you can get this GPU now |
| Blue | In use. Lighter means it frees sooner: under 1 hour, 1–6 hours, 6–24 hours, over a day. |
| Violet stripes | Reserved for specific users or accounts |
| Grey stripes | Idle, but the node's CPUs or memory are fully booked, so a job can't use it |
| Red stripes | Node is down or draining |
| Ring around the square | Your job |

Small dots on a node: amber = the scheduler is holding the node for a queued job, violet = reservation,
red = drain / down.

Things to know:

- **End times are time limits.** Jobs often finish earlier, so "ends in" is the latest time a GPU will be released.
- **Queued** counts only jobs waiting for resources or priority. Held jobs and jobs waiting on dependencies are left
  out. A job that accepts several GPU types counts toward each of them.
- **Estimated start** times come from Slurm's scheduler and change as other jobs finish early or get submitted.

### Job details panel

Clicking one of your jobs opens a panel with:

- **Time:** time left, end time, and how much of the time limit is used.
- **Resources:** CPU cores actually busy (average since the start) vs allocated, peak and average RAM vs allocated,
  disk read and written. This shows whether you asked for far more than the job uses.
- **GPUs:** utilization, memory, power and temperature of each of the job's GPUs, from `nvidia-smi` run inside the job.
- **What's running:** the job's processes with CPU and memory, e.g. `16× python -m train …` (one main process plus
  data-loader workers).
- **Log:** the last 120 lines of the job's stdout / stderr, refreshed every 15 s, and when the file was last written
  (an old timestamp on a running job often means it's stuck).
- **Submission:** the `sbatch` / `salloc` command, working directory, the batch script as submitted, and a copyable
  `scancel` line.

The GPU and process check runs `srun --overlap` inside your job when the panel opens and when you click
**refresh**. It takes well under a second and doesn't disturb the job, but each check adds a short finished step
(e.g. `1234.5 bash`) to the job's `sacct` history. Only your own jobs can be opened.

## Is it OK to run on a login node?

Yes. Measured on this cluster with a dashboard open:

- **The server process** uses about 2% of one CPU core and 50–70 MB of memory, including the Slurm commands it runs.
- **The Slurm controller** gets one `squeue` and one `scontrol show node` query every 30 s, but only while a page is
  open. The server stops querying 2 minutes after the last tab closes, so leaving it running in `tmux` costs nothing
  while you're away.
- **Job details** are only fetched when you click a job: about 0.1 s of Slurm queries, plus a sub-second check
  inside the job.

If many people use it at once, it would be kinder to the controller to share one server, or to lengthen the
interval (`--interval 60`).

## Update or uninstall

```bash
/usr/bin/python3 -m pip install --user --force-reinstall --no-deps git+https://github.com/FaizalKarim280280/gpuglance.git
/usr/bin/python3 -m pip uninstall gpuglance
```

Use `--force-reinstall` to update: plain `--upgrade` skips the reinstall when the version number hasn't changed.

## Troubleshooting

| Problem | Fix |
|---|---|
| `gpuglance: command not found` | Add `~/.local/bin` to your `PATH` (see Install), or run `/usr/bin/python3 -m gpuglance` |
| Browser says "connection refused" (option B) | The ssh session has ended, or the server printed a different port than the one in your `-L`. Rerun it with another port number. |
| `bind: Address already in use` on your laptop | Something on your laptop already uses that port. Use another number on both sides. |
| Header says "update failed" and the data stops changing | Slurm didn't answer, which happens briefly when the controller is busy. The page keeps the last data and retries every 30 s. |
| `squeue not found` | Run it on a cluster node (login or compute), not on your laptop |
| Clicking a job says "Job details are locked" | Open the link with `?token=…` that `gpuglance serve` printed (once per browser) |
| Panel says it couldn't look inside the job | The `srun --overlap` check failed, for example because the node is down or the job just ended. Details, usage and log still work. |

## Command reference

```
gpuglance                                       terminal summary, all types
gpuglance status [TYPE ...] [--no-color]        terminal summary, only these types
gpuglance serve [--port 8765] [--host 127.0.0.1] [--interval 30]
gpuglance json                                  snapshot as JSON
gpuglance dump-raw DIR                          save Slurm's raw JSON (for development)

common options:
  --partitions a100,h100   only these partitions (default: every partition with GPU nodes;
                           or set GPUGLANCE_PARTITIONS)
  --raw-dir DIR            read saved Slurm JSON instead of calling Slurm
```

## Development

```bash
git clone git@github.com:FaizalKarim280280/gpuglance.git && cd gpuglance
/usr/bin/python3 -m pip install --user -e .   # editable: changes to static/ show up on page reload;
                                              # restart `gpuglance serve` after Python changes
python3 tests/test_parsing.py                 # no Slurm needed
```

```
src/gpuglance/
  slurm.py      runs scontrol/squeue --json (the only code that touches Slurm)
  snapshot.py   raw JSON -> per-GPU slot states, jobs, per-type summary (pure, testable)
  hostlist.py   expands gh[101-103] and IDX:0-3 strings
  server.py     stdlib HTTP server; queries Slurm every --interval seconds while a page is open;
                token-protected /api/job/<id> endpoints for your own jobs
  jobinfo.py    one job's details: scontrol, batch script, sstat usage, log tail, srun --overlap probe
  term.py       terminal summary
  cli.py        `gpuglance` entry point
  static/       index.html, style.css, app.js, panel.js (plain JavaScript, no build step)
tests/test_parsing.py
```

To work without Slurm (e.g. on a laptop), run `gpuglance dump-raw /tmp/raw` on the cluster, copy the folder over,
then run `gpuglance serve --raw-dir /tmp/raw`.
