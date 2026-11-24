"""A private copy of the shared memory module for one caller.

The shared `memory.py` keeps its settings and its database path in module globals: one process, one house. The kit
makes a store per house and its tests make hundreds, each with its own database, names and clock. So a caller
does not import the module, it loads a copy: `load()` runs the same two source files (`qq_identity.py`, then
`memory.py`) again under a package name of their own, bound to the roster given. Nothing in the files is changed
for this, and the copies share nothing but the code.
"""

import importlib.util
import itertools
import os
import sys
import threading
import types

from . import roster as _empty_roster

_HERE = os.path.dirname(os.path.abspath(__file__))
_count = itertools.count(1)
_lock = threading.Lock()


def load(roster=None):
    """-> a fresh `memory` module whose `ID` and `R` belong to it alone. roster: a module-like object with the
    names of `roster.py` (see `roster.make`); the empty roster when left out. The settings are read from the
    environment as the file does at import; the caller sets what it needs on the module afterwards."""
    with _lock:
        pkg_name = f"{__package__}._copy{next(_count)}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []  # a package, so that `from . import roster` works inside the copies
    pkg.__package__ = pkg_name
    pkg.roster = roster if roster is not None else _empty_roster
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".roster"] = pkg.roster
    try:
        for name in ("qq_identity", "memory"):
            spec = importlib.util.spec_from_file_location(f"{pkg_name}.{name}", os.path.join(_HERE, name + ".py"))
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            setattr(pkg, name, mod)
    except BaseException:
        unload(pkg)
        raise
    pkg.memory.__copy__ = pkg_name
    return pkg.memory


def unload(memory):
    """Drop a copy made by load() from the import table (the module objects live as long as they are used)."""
    pkg_name = memory.__name__ if isinstance(getattr(memory, "__path__", None), list) \
        else getattr(memory, "__copy__", "")
    if pkg_name.startswith(f"{__package__}._copy"):
        for key in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
            sys.modules.pop(key, None)
