# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 the Klepsydra authors
"""Fixtures shared by the browser tests: the built site with the fixture timers, and a headless Chromium."""
import functools
import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    # Serve what build.sh publishes (so a file missing from its list fails here), with the fixture timers.
    src = tmp_path_factory.mktemp("src") / "app"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "tests", "public", ".pytest_cache", "__pycache__", ".env.local"))
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_SHA", "GOOGLE_CLIENT_ID")}
    subprocess.run(["sh", "build.sh"], cwd=src, env=env, check=True)
    site = src / "public"
    shutil.copy(ROOT / "tests" / "fixtures" / "timers.json", site / "timers.json")
    handler = functools.partial(QuietHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()
