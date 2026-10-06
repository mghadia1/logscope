"""Run the API: ``python -m logscope.serve``."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "logscope.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
