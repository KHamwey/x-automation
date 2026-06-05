"""Allow running as python -m x_automation."""

from x_automation.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
