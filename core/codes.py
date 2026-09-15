"""稳定错误码 / 事件码常量。

契约（沿用仓库既有惯例，见 `.agent/skills/neko-plugin/references/core-plugin-contract.md` 与
forever_companion 台账第九轮结论）：

- 面板可达入口返回的 `Err(SdkError(code))` 与 `note` 字段**只允许稳定 ASCII 码**，
  形态 `^[a-z][a-z0-9_]*$`；动态细节（数值、角色名、异常文本）一律进日志，不进文案。
- 前端 `ui/utils.ts` 的 `errorText()` 按 `panel.errors.<camelCase(码)>` 翻译；
  非码文本（宿主自身错误 / 超时）原样直出。
- 因此：**新增码必须同时**出现在本模块的 `PANEL_ERROR_CODES` 与两份 i18n 的
  `panel.errors.*` 里，由 `tests/test_i18n_contract.py` 的码↔键同步门守住。

EXEMPT: 开发者面向的调试入口与给模型的行为指令（本插件暂无）不走面板 toast 通路。
"""

from __future__ import annotations

import re

__all__ = ["CODE_PATTERN", "PANEL_ERROR_CODES", "is_panel_code", "camel_case"]

# 面板可达入口可见的稳定码集合（保持 ASCII、语义自明）。
PANEL_ERROR_CODES: frozenset[str] = frozenset(
    {
        # 总开关 / 状态
        "not_enabled",  # 总开关关闭：数值冻结中
        "enabled",  # 总开关已打开
        "disabled",  # 总开关已关闭
        "stats_loaded",  # 查询成功（成功档也用码，前端按码渲染 toast）
        "stat_updated",  # 手动纠偏成功
        "stats_reset",  # 重置成功
        # 面板焦点（v0.4.2，成功档）
        "focus_set",  # 焦点已切到指定分片
        "focus_cleared",  # 焦点已清除，退回自动判定
        # 参数非法
        "invalid_stat",  # 未知数值名
        "invalid_value",  # 数值不是 0..100 的数字
        "invalid_lanlan",  # 角色标识为空 / 非法
        "invalid_item",  # 未知物品 id
        "invalid_quantity",  # 数量不是正整数
        "insufficient_sodas",  # 金币不够
        "carry_full",  # 携带上限已满
        "over_daily_limit",  # 触发当日消费上限
        # 商店 / 照料（成功档）
        "shop_purchased",  # 买到了
        "care_applied",  # 用掉了一件东西
        "coin_updated",  # 手动补了一笔金币
        # 运行时故障
        "store_unavailable",  # PluginStore 不可用（未启用或通道故障）
        "config_unavailable",  # 配置不可读
        # 「索取陪伴」工具冷却
        "company_cooldown",  # 冷却中，还没到可以撒娇的时机
        # 她在睡觉：非危机的"索取陪伴"会被挡下（不是错误，是作息）
        "sleeping",  # 她已经睡了，等她醒了再说
        # 反馈闭环（v0.3.0）：她自己的判断回流被四道闸门挡下的情形。
        # 这三个码**刻意不进工具返回值**给模型（那会把噪声引回对话），
        # 而是留给面板/诊断面——工具返回的是 `reason=judgment_*` 那种内部原因码。
        "invalid_judgment",  # 标签不在白名单里（已被收敛成 neutral，不修正）
        "judgment_capped",  # 当日修正预算用尽
        "judgment_throttled",  # 会话内递减到不足以修正
        # 签到与补签（v0.7.0）
        "checkin_done",  # 签到成功（成功档）
        "checkin_disabled",  # [our_life.checkin].enabled = false
        "already_checked_in",  # 今天已经签过了
        "makeup_done",  # 补签成功（成功档）
        "makeup_invalid_day",  # 日子非法、是今天、或是未来（今天该走正常签到）
        "makeup_expired",  # 超出补签窗口
        "makeup_exhausted",  # 本周补签额度用完
    }
)

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def is_panel_code(value: str) -> bool:
    """码形门：面板可见文案必须是稳定 ASCII 码。"""
    return bool(CODE_PATTERN.match(value))


def camel_case(code: str) -> str:
    """`not_enabled` → `notEnabled`（与前端 `panel.errors.<camelCase>` 对齐）。"""
    head, *tail = code.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)
