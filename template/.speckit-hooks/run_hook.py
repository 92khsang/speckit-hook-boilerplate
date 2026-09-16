"""Launcher for the hook package.

Invoking a file rather than `python3 -m` is deliberate: with `-m`, Python puts the
current working directory on `sys.path`, and the hook runs with the repository as
its working directory. A repository could then shadow a standard-library module.
Running this file instead makes `sys.path[0]` the directory holding the package.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from speckit_prehook.__main__ import main  # noqa: E402  (must follow the path setup)

if __name__ == "__main__":
    sys.exit(main())
