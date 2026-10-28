"""Cut a release: bump the version, commit, tag and push. GitHub Actions then builds and publishes it.

    python release.py 2.8.0          # a release, for everyone (needs release-notes/v2.8.0.md)
    python release.py 2.9.0-beta.1   # a beta: only the Beta and Dev update channels get it
    python release.py 2.8.0 --no-push   # commit and tag only; push yourself with: git push --follow-tags

Run it on main. Dev builds need nothing: every push to main publishes one for the Dev channel.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT = ROOT / "ytclip" / "__init__.py"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", help="like 2.8.0, or 2.9.0-beta.1 for a beta")
    p.add_argument("--no-push", action="store_true", help="don't push the commit and tag")
    args = p.parse_args(argv)
    version = args.version.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(-(alpha|beta|rc)\.?\d*)?", version):
        print(__doc__)
        return 2
    tag = f"v{version}"
    beta = "-" in version
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        print(f"You're on {branch}. Releases are made from main: git checkout main")
        return 1
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("There are changes that aren't committed. Commit or undo them first (git status shows them).")
        return 1
    if not args.no_push:
        pulled = git("pull", "--ff-only", check=False)
        if pulled.returncode:
            print(f"Couldn't bring main up to date with GitHub:\n{pulled.stderr.strip()}")
            return 1
    if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
        print(f"Tag {tag} already exists. Pick a new version number.")
        return 1
    notes = ROOT / "release-notes" / f"{tag}.md"
    if not notes.is_file():
        if not beta:
            print(f"{notes.relative_to(ROOT)} is missing: write what's new first (it's what the app shows).")
            return 1
        print(f"(No {notes.relative_to(ROOT)}: the beta's page lists its changes only.)")
    text = INIT.read_text(encoding="utf-8")
    INIT.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', text, count=1), encoding="utf-8")
    # Releasing the version that's already current (a PR set it) has nothing to commit.
    if git("diff", "--quiet", "--", str(INIT), check=False).returncode != 0:
        git("commit", "-m", f"Release {version}", "--", str(INIT))
    git("tag", "-a", tag, "-m", f"ClipMonster {version}")
    if args.no_push:
        print(f"Tagged {tag}. Publish it with: git push --follow-tags")
        return 0
    for what in (["push", "origin", "main"], ["push", "origin", tag]):
        pushed = git(*what, check=False)
        if pushed.returncode:
            print(f"Tagged {tag}, but `git {' '.join(what)}` failed:\n{pushed.stderr.strip()}\n"
                  f"Fix that, then run: git {' '.join(what)}")
            return 1
    print(f"Released {tag}: GitHub is building it now (about 15 minutes), then installed copies on the "
          f"{'Beta and Dev channels' if beta else 'every channel'} offer it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
