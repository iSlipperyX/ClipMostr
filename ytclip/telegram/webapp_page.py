"""The old name of mini_app_page.py (ClipMonster 3.10 and 3.11): the same module."""

import sys

from . import mini_app_page

sys.modules[__name__] = mini_app_page
