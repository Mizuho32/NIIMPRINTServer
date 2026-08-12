from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from label_print_server.app import create_app


def main() -> None:
    default_config = Path(__file__).resolve().parent / "config.example.json"
    parser = argparse.ArgumentParser(description="Run the label print server")
    parser.add_argument(
        "--config",
        default=str(default_config),
        help="Path to the JSON config file",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Optional SQLite database path override",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = create_app(args.config, args.database)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
