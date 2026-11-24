#!/usr/bin/env python3
"""Entrypoint script for the Antigravity Discord Gateway."""

import sys
from agy_gateway.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
