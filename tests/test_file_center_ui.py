"""Drive the shipped file center in an installed Chromium browser; use only loopback fixtures."""
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

TESTS = Path(__file__).resolve().parent
STATIC = TESTS.parent / "adaptive_rag" / "static"
BROWSER = (os.environ.get("RAG_TEST_CHROMIUM") or shutil.which("chromium") or shutil.which("google-chrome")
           or shutil.which("chromium-browser") or shutil.which("msedge"))
if not BROWSER and os.name == "nt":
    edge = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe"
    BROWSER = str(edge) if edge.is_file() else None


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/knowledge":
            text = (STATIC / "knowledge.html").read_text(encoding="utf8")
            text = text.replace("<head>", '<head><script src="/file_center_fixture.js"></script>')
            text = text.replace("</head>", '<script src="/file_center_scenarios.js" defer></script></head>')
            body, mime = text.encode(), "text/html"
        elif path in ("/file_center_fixture.js", "/file_center_scenarios.js"):
            body, mime = (TESTS / path[1:]).read_bytes(), "text/javascript"
        elif path.startswith("/static/") and (STATIC / Path(path).name).is_file():
            asset = STATIC / Path(path).name
            body = asset.read_bytes()
            mime = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[asset.suffix]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(BROWSER, "Set RAG_TEST_CHROMIUM to run browser UI tests")
class FileCenterBrowserTests(unittest.TestCase):
    def run_scenario(self, role):
        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
                    args = [BROWSER, "--headless", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
                            "--disable-sync", "--no-first-run", "--disable-crash-reporter", "--user-data-dir=" + profile,
                            "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{server.server_port}/knowledge?{role}"]
                    run = subprocess.run(args, capture_output=True, timeout=45, check=True)
                    class Result(HTMLParser):
                        value = None
                        def handle_starttag(self, tag, attrs):
                            if tag == "body":
                                self.value = dict(attrs).get("data-result")
                    result = Result()
                    result.feed(run.stdout.decode("utf8"))
                    self.assertEqual(result.value, "pass", run.stderr.decode(errors="replace")[-500:])
            finally:
                server.shutdown()

    def test_member_file_workflows(self):
        self.run_scenario("member")

    def test_admin_file_workflows(self):
        self.run_scenario("admin")
