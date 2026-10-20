"""PyInstaller entry point for the portable Charon.exe.

Kept outside the ``charon`` package so PyInstaller's analysis
starts from a plain script (the same pattern as Argus's
``packaging/launch_argus.py``).

Routing is deliberately small:

- No arguments (a person double-clicking the portable exe, or
  running it from the Sentinel IT Toolkit USB) opens Charon's
  window: the whole Crossing as one guided journey.
- Any argument hands the launch to the normal command line
  (:func:`charon.cli.main`), so ``Charon.exe scan``,
  ``Charon.exe --version`` and the other verbs behave exactly as
  they do from source.
"""

import sys

from charon.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["gui"]
    sys.exit(main(argv))
