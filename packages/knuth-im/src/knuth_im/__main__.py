"""Serve the Knuth IM AG-UI backend."""

from __future__ import annotations

import os

import uvicorn
from knuth_agui import create_app

from knuth_im.runtime_factory import build_runtime, load_dotenv


def main() -> None:
    load_dotenv()
    app = create_app(build_runtime())
    host = os.environ.get("KNUTH_IM_HOST", "127.0.0.1")
    port = int(os.environ.get("KNUTH_IM_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
