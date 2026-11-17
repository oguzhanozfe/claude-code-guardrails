"""`python -m guardrails <command>` from the repo root, same as `python3 hook.py <command>`."""
import sys

from hook import main

sys.exit(main())
