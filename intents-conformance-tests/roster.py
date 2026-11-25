"""Who is who for the tests in this directory: made-up housemates, with the numbers and names the tests write.

The shared code reads its housemates from a roster module (see src/ai_house/common/roster.py); this one is built with
that file's `make()` and is the one table every shim here hands to the loader.
"""

import sys

from ai_house.common import roster as _kit

_ROWS = (
    {"actor": "wanda", "consumer": "wanda_c", "qq": 1000000037, "name": "婉妲", "account": ("Wanda_c",)},
    {"actor": "nell", "qq": 1000000074, "name": "奈尔"},
    {"actor": "elsa", "qq": 1000000111, "name": "艾莎", "nicknames": ("小艾",)},
    {"actor": "alma", "qq": 1000000148, "name": "阿尔玛", "nicknames": ("Alma",)},
    {"actor": "mina", "qq": 1000000185, "name": "米娜"},
    {"actor": "yuki", "qq": 1000000222, "name": "由纪", "former": ("由纪旧名",), "nicknames": ("小由", "小由1")},
)
sys.modules[__name__] = _kit.make(_ROWS, owner={"qq": 1000000259, "name": "房主", "aliases": ("房主别名1",)},
                                  name="roster")
