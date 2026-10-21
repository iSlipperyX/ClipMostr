"""Cut a release: bump the version, commit, tag. Pushing the tag makes GitHub build it.

    python release.py 1.2.0          # final release
    python release.py 1.3.0-beta.1   # pre-release (only for users who opted into betas)
    git push && git push --tags      # then GitHub Actions builds and publishes it
"""

import re
import subprocess
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent / "ytclip" / "__init__.py"


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+(-(alpha|beta|rc)\.?\d*)?", sys.argv[1]):
        print(__doc__)
        return 2
    version = sys.argv[1]
    tag = f"v{version}"
    if subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], capture_output=True).returncode == 0:
        print(f"Tag {tag} already exists. Pick a new version number.")
        return 1
    text = INIT.read_text(encoding="utf-8")
    INIT.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text), encoding="utf-8")
    # Releasing the version that's already current (e.g. the very first release) has nothing to commit.
    if subprocess.run(["git", "diff", "--quiet", "--", str(INIT)]).returncode != 0:
        subprocess.run(["git", "commit", "-m", f"Release {version}", "--", str(INIT)], check=True)
    subprocess.run(["git", "tag", "-a", tag, "-m", f"ClipMonster {version}"], check=True)
    print(f"\nTagged {tag}. Publish it with:\n  git push && git push --tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
