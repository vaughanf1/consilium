"""Vercel entrypoint: the same FastAPI app `consilium serve` runs locally.

Vercel's rewrite hands the function its *destination* path (`/api/index`), not
the path the browser asked for, so `vercel.json` forwards the real path in a
query parameter and this shim restores it before FastAPI routes the request.
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CONSILIUM_SERVERLESS", "1")

from consilium.server.app import create_app  # noqa: E402

_fastapi = create_app()


async def app(scope, receive, send):
    if scope["type"] == "http":
        params = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        rest = [(k, v) for k, v in params if k != "__path"]
        forwarded = next((v for k, v in params if k == "__path"), None)
        if forwarded is not None:
            scope = dict(scope)
            scope["path"] = "/api/" + forwarded.lstrip("/")
            scope["raw_path"] = scope["path"].encode()
            scope["query_string"] = urlencode(rest).encode()
    await _fastapi(scope, receive, send)
