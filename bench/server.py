"""Local static server for the test pages.

Threaded, quiet, and sends ``Cache-Control: no-store`` so warm browsers always
refetch ``ok.html`` after the S7 loop rewrites it (which is also what any dev
server an agent would use does).
"""
from __future__ import annotations

import http.server
import os
import socket
import threading
from functools import partial


class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # silence
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Pages that call window.close() reset in-flight connections (favicon);
        # that is expected and not worth a traceback.
        pass


class StaticServer:
    def __init__(self, root: str, host: str = "127.0.0.1", port: int = 0):
        self.root = root
        handler = partial(_Handler, directory=root)
        self.httpd = _QuietServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def start(self):
        self._thread.start()
        # Make sure it accepts connections before anyone uses it.
        with socket.create_connection((self.host, self.port), timeout=2):
            pass
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    import sys
    import time

    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "pages")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    s = StaticServer(root, port=port).start()
    print(s.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s.stop()
