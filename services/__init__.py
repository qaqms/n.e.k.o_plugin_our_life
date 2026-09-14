"""`our_life` · services：有状态层。

这里才允许接触宿主通道（PluginStore / 总线 / push_message）；纯逻辑一律在 `core/`。
"""

from __future__ import annotations

from .injector import DRIFT_THRESHOLD, InjectionPlan, Injector
from .sampler import BehaviorSampler
from .state import (
    INJECT_HISTORY_MAX,
    JUDGMENT_HISTORY_MAX,
    KEY_PREFIX,
    MEAL_DAYS_KEEP,
    SEEN_IDS_MAX,
    ShardState,
    StateStore,
    day_number_for,
    lanlan_from_key,
    now_seconds,
    shard_key,
)

__all__ = [
    "BehaviorSampler",
    "DRIFT_THRESHOLD",
    "INJECT_HISTORY_MAX",
    "JUDGMENT_HISTORY_MAX",
    "InjectionPlan",
    "Injector",
    "KEY_PREFIX",
    "MEAL_DAYS_KEEP",
    "SEEN_IDS_MAX",
    "ShardState",
    "StateStore",
    "day_number_for",
    "lanlan_from_key",
    "now_seconds",
    "shard_key",
]
