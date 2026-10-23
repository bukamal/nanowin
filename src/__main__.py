"""Windows/flet bootstrap fallback entry point.

Some versions of serious_python invoke the app differently; this ensures
`main` module is importable/executable regardless of entry resolution.
"""
from main import main

if __name__ == "__main__":
    main()
