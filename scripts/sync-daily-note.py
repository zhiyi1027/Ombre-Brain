#!/usr/bin/env python3
"""Retired entry point: never read local notes, tokens, or pending queues."""
import sys


def main():
    print("Daily-note uploading has been retired. Stop its scheduled caller; "
          "daily impressions are prepared from the OB raw archive.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
