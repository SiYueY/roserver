"""``python -m roserver`` uvicorn entrypoint (single worker only)."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roserver")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="must remain 1; roboagent uses a same-event-loop contract",
    )
    args = parser.parse_args(argv)
    if args.workers != 1:
        print(
            "roserver requires exactly one uvicorn worker: RunProjection and "
            "PublicEventBuffer are in-process only and roboagent uses a "
            "same-event-loop contract.",
            file=sys.stderr,
        )
        return 2
    settings = Settings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, workers=1)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
