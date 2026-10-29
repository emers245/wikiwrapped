"""Start the installed WikiWrapped: what the app's shortcuts run.

    <install folder>/share/wikiwrapped/launch.py [launcher options]

packaging/stage_app.py copies this file to the top of the installed code
folder, next to config.py, so Python puts that folder on sys.path and the
app imports exactly as it does in a checkout. Everything after the setup
below is app.launcher (`--self-test` included).

The setup is what a conda installation normally gets from activating its
environment in a terminal, which a double-clicked shortcut never does. It
runs before the app is imported because some of it only takes effect then.
"""

import os
import sys

# Must match the shortcut's app_user_model_id in packaging/menu/
# (tests/test_packaging.py checks): Windows groups a running program under
# the shortcut that started it, with that shortcut's name and icon, only
# when the two IDs agree. Otherwise the taskbar shows a generic Python.
APP_USER_MODEL_ID = "emers245.WikiWrapped"

# Bytecode caches are never written: Python would otherwise put __pycache__
# folders into the install folder, which an app must not write to (it may
# be read-only, and an upgrade replaces it). The installer precompiles the
# app instead (packaging/installer/*/post_install). The environment variable
# carries the same setting to the background jobs' child processes.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

if sys.platform == "win32":
    import ctypes

    # The folders `conda activate` puts first on PATH. Conda's Windows
    # libraries keep their DLLs there rather than beside the Python modules
    # that load them. PATH is inherited by the job children;
    # add_dll_directory covers this process, since Python 3.8 no longer
    # looks for a module's DLLs on PATH. The handles are kept: closing one
    # removes its folder again.
    _prefix = sys.prefix
    _dll_dirs = [os.path.join(_prefix, *parts) for parts in (
        (), ("Library", "mingw-w64", "bin"), ("Library", "usr", "bin"),
        ("Library", "bin"), ("Scripts",), ("bin",),
    )]
    os.environ["PATH"] = os.pathsep.join([*_dll_dirs, os.environ.get("PATH", "")])
    _dll_handles = [os.add_dll_directory(d) for d in _dll_dirs if os.path.isdir(d)]
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)

from app import launcher  # noqa: E402  (after the setup above, on purpose)

sys.exit(launcher.main())
