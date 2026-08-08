"""Allow ``python -m sockpost`` (used by the daemon autostart path)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
