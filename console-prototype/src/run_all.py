"""Run experiment A, experiment B, or both."""

from __future__ import annotations

import sys

from .experiment_a import run as run_a
from .experiment_b import run as run_b


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "all"
    if mode in ("A", "a"):
        run_a()
    elif mode in ("B", "b"):
        run_b()
    elif mode in ("all", "ALL", "All"):
        results = [run_a(), run_b()]
        print(
            "ALL_PASS="
            + ("true" if all(r.get("pass") for r in results) else "false")
        )
    else:
        print("usage: python -m src.run_all [A|B|all]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
