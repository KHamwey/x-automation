"""Allow running as ``python -m x_automation``.

Delegates to :func:`x_automation.cli.main` so the package can be invoked
without installing a console script entry point.
"""

from x_automation.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
