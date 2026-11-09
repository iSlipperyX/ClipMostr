import os
import sys

# A Mac app started from Finder may have nowhere to print to
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

from ytclip.mac.app import main  # noqa: E402

raise SystemExit(main())
