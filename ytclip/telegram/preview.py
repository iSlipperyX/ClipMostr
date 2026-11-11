"""The old name of streamer.py (ClipMonster 3.5 to 3.11): the same module, so code and tests that knew it as
`preview` still reach the same functions and settings."""

import sys

from . import streamer

sys.modules[__name__] = streamer
