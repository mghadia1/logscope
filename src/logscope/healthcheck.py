"""Docker's health check: ``python -m logscope.healthcheck``.

Exits 0 if the local API answers ``/api/health`` with 200, else 1. The image
has no curl, and does not need one.
"""

import os
import sys
import urllib.request


def main() -> int:
    url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 0 if response.status == 200 else 1
    except OSError as error:
        print(f"unhealthy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
