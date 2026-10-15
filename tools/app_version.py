"""
Utility to print the application version and build number.
Used by CI and build scripts to keep version information in sync.
"""

import sys
from pathlib import Path

# Ensure we can import the version.py file
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from src.nano_offline.version import APP_VERSION, BUILD_NUMBER
except ImportError:
    # Fallback in case the structure is different or it's called from a different context
    try:
        from nano_offline.version import APP_VERSION, BUILD_NUMBER
    except ImportError:
        # Hardcoded fallback to prevent total failure if module structure is unknown
        print("0.1.0")
        print("1")
        sys.exit(0)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(APP_VERSION)
    elif args.build:
        print(BUILD_NUMBER)
    else:
        # Default format: "version build"
        print(f"{APP_VERSION} {BUILD_NUMBER}")
