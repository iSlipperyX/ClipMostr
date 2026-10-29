"""ClipMonster (ytclip) - download YouTube videos and cut them into their most interesting clips."""

__version__ = "2.9.0"

try:  # a dev build's own number ("2.8.0+dev.3"), written by build.py --version
    from ._build_info import BUILD_VERSION as _build_version
except ImportError:  # running from source, or a release build
    _build_version = ""
if _build_version:
    __version__ = _build_version
