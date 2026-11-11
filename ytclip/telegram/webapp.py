"""The old name of mini_app.py (ClipMonster 3.10 and 3.11): the same module, so code and tests that knew it as
`webapp` still reach the same server."""

import sys

from . import mini_app

sys.modules[__name__] = mini_app
