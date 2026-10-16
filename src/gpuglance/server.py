"""Tiny stdlib web server: serves the static page and /api/snapshot.

A background loop queries Slurm every `interval` seconds while someone is
looking (any request in the last IDLE_AFTER seconds), so one query serves every
open tab. The page polls cheaply with ?v=<version>: the server answers 204 when
nothing changed and sends the full snapshot only when there is a new one.
"""

from __future__ import annotations

import getpass
import gzip
import hmac
import json
import os
import re
import secrets
import mimetypes
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Dict, List, Optional

from . import jobinfo, slurm, snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"
TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gpuglance" / "token"
JOB_ROUTE = re.compile(r"^/api/job/([0-9_\[\],%:-]+)(/live)?$")


def load_token() -> str:
    """Secret for the job-detail endpoints (they read your logs and scripts).

    Any user on a shared login node can connect to localhost:<port>, so these
    endpoints need this token; it lives in a file only you can read and is
    reused across restarts, so a bookmarked link keeps working.
    """
    try:
        tok = TOKEN_FILE.read_text().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


IDLE_AFTER = 120  # stop querying Slurm this many seconds after the last page request


class SnapshotCache:
    def __init__(self, interval: float, partitions: Optional[List[str]], raw_dir: Optional[str]):
        self.interval = interval
        self.partitions = partitions
        self.raw_dir = raw_dir
        self._lock = threading.Lock()
        self._body: Optional[bytes] = None  # gzipped JSON
        self._version = ""
        self._error: Optional[str] = None
        self._last_request = time.time()
        self._first = threading.Event()  # set once the first refresh finished (ok or not)
        self._wake = threading.Event()   # a request after an idle spell restarts the loop at once
        threading.Thread(target=self._loop, daemon=True).start()

    def _refresh(self) -> None:
        try:
            t0 = time.time()
            raw = slurm.collect(self.partitions, raw_dir=self.raw_dir)
            snap = snapshot.build(raw)
            snap["fetch_seconds"] = round(time.time() - t0, 2)
            snap["refresh_interval"] = self.interval
            version = str(int(snap["generated_at"] * 1000))
            snap["version"] = version
            body = gzip.compress(json.dumps(snap, separators=(",", ":")).encode(), compresslevel=5)
            with self._lock:
                self._body, self._version, self._error = body, version, None
        except Exception as e:  # keep serving the last good snapshot
            traceback.print_exc()
            with self._lock:
                self._error = str(e)
        finally:
            self._first.set()

    def _loop(self) -> None:
        while True:
            if time.time() - self._last_request > IDLE_AFTER:
                self._wake.clear()
                if time.time() - self._last_request > IDLE_AFTER:  # re-check: a request may have just arrived
                    self._wake.wait()  # nobody is looking: sleep until the next request
            t0 = time.time()
            self._refresh()
            # fixed cadence: start a query every `interval` seconds, whatever the fetch took
            time.sleep(max(1.0, self.interval - (time.time() - t0)))

    def get(self) -> Dict[str, Any]:
        self._last_request = time.time()
        self._wake.set()
        if not self._first.is_set():
            self._first.wait(timeout=150)
        with self._lock:
            return {"body": self._body, "version": self._version, "error": self._error}


def make_handler(cache: SnapshotCache, token: str, user: str):
    live_cache: Dict[str, Any] = {}  # job id -> (time, result): repeated clicks within 5 s reuse one probe
    live_lock = threading.Lock()

    def job_api(job_id: str, live: bool) -> Dict[str, Any]:
        if not live:
            return jobinfo.details(job_id, user)
        with live_lock:
            hit = live_cache.get(job_id)
            if hit and time.time() - hit[0] < 5:
                return hit[1]
        res = jobinfo.live(job_id, user)
        with live_lock:
            live_cache[job_id] = (time.time(), res)
        return res

    class Handler(BaseHTTPRequestHandler):
        server_version = "gpuglance"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, gz: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if gz:
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    self.send_header("Content-Encoding", "gzip")
                else:
                    body = gzip.decompress(body)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            m = JOB_ROUTE.match(path)
            if m:
                sent = self.headers.get("X-Gpuglance-Token") or ""
                if not hmac.compare_digest(sent.encode(), token.encode()):
                    msg = json.dumps({"error": "token", "detail": "open the link with ?token=... printed by `gpuglance serve`"})
                    return self._send(401, msg.encode(), "application/json")
                try:
                    res = job_api(m.group(1), bool(m.group(2)))
                except jobinfo.JobAccessError as e:
                    return self._send(404, json.dumps({"error": str(e)}).encode(), "application/json")
                body = gzip.compress(json.dumps(res, separators=(",", ":")).encode(), compresslevel=5)
                return self._send(200, body, "application/json", gz=True)
            if path == "/api/snapshot":
                res = cache.get()
                since = parse_qs(urlsplit(self.path).query).get("v", [""])[0]
                if res["body"] is not None and since == res["version"]:
                    self.send_response(204)  # page already has the newest snapshot
                    self.send_header("Cache-Control", "no-store")
                    if res["error"]:  # last refresh failed: let the page say its data is stale
                        self.send_header("X-Gpuglance-Error", res["error"].splitlines()[0][:200])
                    self.end_headers()
                    return
                if res["body"] is None:
                    msg = json.dumps({"error": res["error"] or "no data yet"}).encode()
                    return self._send(503, msg, "application/json")
                return self._send(200, res["body"], "application/json", gz=True)
            if path in ("/", "/index.html"):
                path = "/index.html"
            f = (STATIC_DIR / path.lstrip("/")).resolve()
            if STATIC_DIR not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

    return Handler


def serve(host: str, port: int, interval: float, partitions: Optional[List[str]] = None,
          raw_dir: Optional[str] = None, port_tries: int = 20) -> None:
    cache = SnapshotCache(interval, partitions, raw_dir)
    token = load_token()
    user = os.environ.get("USER") or getpass.getuser()
    httpd = None
    for p in range(port, port + port_tries):
        try:
            httpd = ThreadingHTTPServer((host, p), make_handler(cache, token, user))
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port in {port}-{port + port_tries - 1}")
    p = httpd.server_address[1]
    import socket
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{p}/?token={token}"
    if p != port:
        print(f"  port {port} is taken on this node, using {p}", flush=True)
    print(f"gpuglance on {url}", flush=True)
    print(f"  node {socket.gethostname()} · open the link above (the token unlocks details of your own jobs)", flush=True)
    print("  VS Code Remote forwards the port automatically: Ctrl/Cmd-click the link.", flush=True)
    print(f"  From a laptop terminal:  ssh -L {p}:localhost:{p} <login-address>, then open the link.", flush=True)
    print("  Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
