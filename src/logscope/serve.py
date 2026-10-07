"""Run the API: ``python -m logscope.serve``.

One process, one worker: the store keeps its memtable in memory and takes a
lock on the data directory, so a second worker would fail to start.
"""

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        "logscope.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
