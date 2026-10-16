"""Tiny stdlib web server: serves the static page and /api/snapshot.

A background loop queries Slurm every `interval` seconds while someone is
looking (any request in the last IDLE_AFTER seconds), so one query serves every
open tab. The page polls cheaply with ?v=<version>: the server answers 204 when
nothing changed and sends the full snapshot only when there is a new one.
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Dict, List, Optional

from . import slurm, snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"


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


def make_handler(cache: SnapshotCache):
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
    httpd = None
    for p in range(port, port + port_tries):
        try:
            httpd = ThreadingHTTPServer((host, p), make_handler(cache))
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port in {port}-{port + port_tries - 1}")
    p = httpd.server_address[1]
    import socket
    print(f"gpuglance on http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{p}  (node {socket.gethostname()})")
    print("  VS Code Remote forwards the port automatically (Ports tab).")
    print(f"  From a laptop terminal:  ssh -L {p}:localhost:{p} <cluster-login>  then open http://localhost:{p}")
    print("  Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
