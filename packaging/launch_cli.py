import sys

if sys.argv[1:2] == ["--apply-update"]:  # the updater's install step: start quickly, load nothing it doesn't need
    from ytclip.swap import main

    raise SystemExit(main(sys.argv[2:]))

from ytclip.cli import main  # noqa: E402

raise SystemExit(main())
