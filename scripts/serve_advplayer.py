from __future__ import annotations

import argparse
import http.server
import socketserver
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src" / "AdvPlayer"


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the static ADV player.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    args = parser.parse_args()

    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer((args.host, args.port), lambda *a, **kw: handler(*a, directory=str(ROOT), **kw)) as server:
        print(f"Serving {ROOT.as_posix()} at http://{args.host}:{args.port}/")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
