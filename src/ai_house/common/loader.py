"""A private copy of the shared memory module for one caller.

The shared `memory.py` keeps its settings and its database path in module globals: one process, one house. The kit
makes a store per house and its tests make hundreds, each with its own database, names and clock. So a caller
does not import the module, it loads a copy: `load()` runs the same four source files (`qq_identity.py`,
`world_shim.py`, `memory.py`, then `intents.py`) again under a package name of their own, bound to the roster given.
Nothing in the files is changed for this, and the copies share nothing but the code. `load()` returns the memory
module; the others of the same copy are reached with `part(memory, "intents")`.
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
    """-> a fresh `memory` module whose `ID` and `R` belong to it alone (and `part()` gives the copy's other modules). roster: a module-like object with the
    names of `roster.py` (see `roster.make`); the empty roster when left out. The settings are read from the
    environment as the files do at import; the caller sets what it needs on the modules afterwards. Nothing is
    switched on: `memory.INTENTS` stays unset."""
    with _lock:
        pkg_name = f"{__package__}._copy{next(_count)}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []  # a package, so that `from . import roster` works inside the copies
    pkg.__package__ = pkg_name
    pkg.roster = roster if roster is not None else _empty_roster
    sys.modules[pkg_name] = pkg
    sys.modules[pkg_name + ".roster"] = pkg.roster
    try:
        for name in ("qq_identity", "world_shim", "memory", "intents"):
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


def part(module, name):
    """-> the module `name` ("qq_identity", "world_shim", "memory" or "intents") of the copy that `module` (the
    memory module load() returned, or any other module of the copy) belongs to."""
    pkg = sys.modules.get(_copy_name(module))
    if pkg is None or not hasattr(pkg, name):
        raise KeyError(f"no module {name!r} in the copy of {getattr(module, '__name__', module)!r}")
    return getattr(pkg, name)


def _copy_name(module):
    if isinstance(getattr(module, "__path__", None), list):
        return module.__name__
    return getattr(module, "__copy__", "") or module.__name__.rpartition(".")[0]


def unload(memory):
    """Drop a copy made by load() from the import table (the module objects live as long as they are used)."""
    pkg_name = _copy_name(memory)
    if pkg_name.startswith(f"{__package__}._copy"):
        for key in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
            sys.modules.pop(key, None)
