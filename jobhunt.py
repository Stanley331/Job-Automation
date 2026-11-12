"""JobHunt entry point.

Usage:
    python jobhunt.py doctor
    python jobhunt.py run --prepare-top 3
    python jobhunt.py dashboard
    python jobhunt.py list
    python jobhunt.py prepare <job-id>
    python jobhunt.py apply <job-id>        # never submits
    python jobhunt.py mark <job-id> applied

The implementation lives in the jobhunt_engine package.
"""

from jobhunt_engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
