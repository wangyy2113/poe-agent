"""工具实现。导入本包即完成注册（副作用在各模块的装饰器里）。"""
from __future__ import annotations

from tools.impl import ailments as _ailments  # noqa: F401  注册副作用
from tools.impl import attack as _attack  # noqa: F401  注册副作用
from tools.impl import bench as _bench  # noqa: F401  注册副作用
from tools.impl import boss as _boss  # noqa: F401  注册副作用
from tools.impl import harvest as _harvest  # noqa: F401  注册副作用
from tools.impl import craft as _craft  # noqa: F401  注册副作用
from tools.impl import crit as _crit  # noqa: F401  注册副作用
from tools.impl import cluster as _cluster  # noqa: F401  注册副作用
from tools.impl import craft_pool as _craft_pool  # noqa: F401  注册副作用
from tools.impl import defence as _defence  # noqa: F401  注册副作用
from tools.impl import essence as _essence  # noqa: F401  注册副作用
from tools.impl import gems as _gems  # noqa: F401  注册副作用
from tools.impl import kb_search as _kb_search  # noqa: F401  注册副作用
from tools.impl import maxhit as _maxhit  # noqa: F401  注册副作用
from tools.impl import metamods as _metamods  # noqa: F401  注册副作用
from tools.impl import monsters as _monsters  # noqa: F401  注册副作用
from tools.impl import mods as _mods  # noqa: F401  注册副作用
from tools.impl import passives as _passives  # noqa: F401  注册副作用
from tools.impl import thresholds as _thresholds  # noqa: F401  注册副作用
from tools.impl import recovery as _recovery  # noqa: F401  注册副作用
from tools.impl import reservation as _reservation  # noqa: F401  注册副作用

__all__ = ["_ailments", "_bench", "_boss", "_craft", "_crit", "_craft_pool", "_defence", "_essence", "_gems", "_harvest", "_kb_search", "_maxhit", "_cluster", "_metamods", "_monsters", "_mods", "_passives", "_recovery", "_reservation", "_thresholds"]
