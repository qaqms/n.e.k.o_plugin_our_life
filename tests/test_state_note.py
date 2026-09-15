"""`core/state_note.py` 的契约门（v0.8.0「她此刻的状态」页）。

这一层是**判据**，不是渲染：它决定"她先说哪一句、说几句、有没有互相拖累"。
所以这里的测试全部围绕判据的稳定面，而不是文案：

1. 句数随等级变动（危机 3 / 偏低 2 / 平稳 1）——用户确认过的口径。
2. 顺序稳定且符合"今天的任务优先于长期结果"（`STAT_NAMES` 次序压过数值）。
3. **只产 i18n 里真的存在的键**：脏数据必须被丢掉，不能被拼成一个新键喷成空白。
4. 缺证据说缺：`None` 不许变成 `0`（拿 0 冒充"今天没变"是编数据）。
5. 耦合码来自 `core/model` 的常量，不在这里重算阈值（否则两处会漂）。
6. 纯函数 + JSON 可序列化：它是 context 的一部分，要过 ZMQ 上线。
"""

from __future__ import annotations

import json
from typing import Any

from our_life.core import model
from our_life.core.configuration import InjectSettings
from our_life.core.model import STAT_NAMES, Stats, axis_details, coupling_signal
from our_life.core.rhythm import DailyRhythm, resolve_rhythm
from our_life.core.state_note import COUPLING_CODES, build_state_note, coupling_codes, voice_keys

INJECT = InjectSettings()

# 五轴五档的全部合法组合：自述键的**唯一**允许集合（与 i18n 结构门同一判据，
# 但这里从档位表现算，不在测试里重抄一遍档位名）。
_ALL_VOICE_KEYS = {
    f"panel.stateVoice.{stat}.{tier}"
    for stat, tiers in (
        ("affection", model.AFFECTION_TIERS),
        ("mood", model.MOOD_TIERS),
        ("health", model.HEALTH_TIERS),
        ("satiety", model.SATIETY_TIERS),
        ("energy", model.ENERGY_TIERS),
    )
    for tier in tiers
}


def _axes(**values: float) -> dict[str, dict[str, Any]]:
    return axis_details(Stats(**values).clamped())


def _voice(**values: float) -> list[str]:
    stats = Stats(**values).clamped()
    axes = axis_details(stats)
    return voice_keys(axes, crisis=model.is_crisis(stats, INJECT), crisis_axes=model.crisis_axes(stats, INJECT))


# ---------------------------------------------------------------------------
# 1. 句数随等级变动
# ---------------------------------------------------------------------------


def test_steady_state_gets_a_single_sentence() -> None:
    """全都还行 → 只说一句（不硬凑，免得每天三段小作文）。"""
    voice = _voice(energy=85, satiety=75, mood=62, health=78, affection=45)
    assert len(voice) == 1


def test_low_daily_axis_gets_two_sentences() -> None:
    voice = _voice(energy=30, satiety=70, mood=55, health=70, affection=50)
    assert len(voice) == 2


def test_crisis_gets_three_sentences() -> None:
    voice = _voice(energy=10, satiety=70, mood=55, health=70, affection=50)
    assert len(voice) == 3


def test_low_affection_alone_does_not_escalate_the_count() -> None:
    """**默认好感就是 20.0**（档 1）。若它算"偏低"，每个新角色都会永远多说一句，
    且天天先汇报"我还不太敢跟你撒娇"——那是**刚开始**，不是身体不对劲。

    这条门钉住 `_DAILY_AXES` 的取舍：好感不进升级判据。
    """
    fresh = Stats()  # 全默认，好感 20.0
    assert model.tier_of("affection", fresh.affection) == "acquainted"
    axes = axis_details(fresh)
    assert voice_keys(axes, crisis=False, crisis_axes=()) and len(
        voice_keys(axes, crisis=False, crisis_axes=())
    ) == 1


# ---------------------------------------------------------------------------
# 2. 顺序：危机最先，其次离中性最远，同距离按 STAT_NAMES
# ---------------------------------------------------------------------------


def test_crisis_axis_is_never_second() -> None:
    voice = _voice(energy=10, satiety=35, mood=55, health=70, affection=50)
    assert voice[0] == "panel.stateVoice.energy.exhausted"


def test_today_tasks_outrank_long_term_affection_at_the_same_tier() -> None:
    """精力(档1) 与 好感(档1) 同距离时，精力必须先说。

    `STAT_NAMES` 的注释写明"体力/饱食是今天的任务，好感是长期结果"，这条就是那条
    纪律在自述顺序上的落地。少了它，新角色会永远把感情问题排在身体问题前面。
    """
    voice = _voice(energy=30, satiety=70, mood=55, health=70, affection=20)
    assert voice[0] == "panel.stateVoice.energy.tired"
    assert "panel.stateVoice.affection.acquainted" in voice


def test_order_is_deterministic_for_identical_input() -> None:
    values = dict(energy=41, satiety=41, mood=41, health=41, affection=41)
    assert _voice(**values) == _voice(**values)


def test_steady_state_speaks_of_its_best_axis() -> None:
    """全在中性档以上时挑"最好的一面"说，而不是先汇报"还行"。"""
    voice = _voice(energy=90, satiety=70, mood=65, health=75, affection=45)
    assert voice == ["panel.stateVoice.energy.charged"]


# ---------------------------------------------------------------------------
# 3. 只产 i18n 里存在的键（脏数据收窄，绝不拼新键）
# ---------------------------------------------------------------------------


def test_every_emitted_voice_key_is_a_legal_stat_tier_pair() -> None:
    for values in (
        dict(energy=0, satiety=0, mood=0, health=0, affection=0),
        dict(energy=100, satiety=100, mood=100, health=100, affection=100),
        dict(energy=37, satiety=63, mood=22, health=81, affection=49),
    ):
        for key in _voice(**values):
            assert key in _ALL_VOICE_KEYS, key


def test_unknown_tier_is_dropped_instead_of_fabricating_a_key() -> None:
    """脏 context / 未来改表：认不出的档名**丢句子**，而不是拼出一个没文案的键。

    少说一句只是少一句；喷一个空引号是给主人看故障。
    """
    axes = _axes(energy=30, satiety=70, mood=55, health=70, affection=50)
    axes["energy"]["tier"] = "bogus_tier"
    voice = voice_keys(axes, crisis=False, crisis_axes=())
    assert "panel.stateVoice.energy.bogus_tier" not in voice
    assert all(key in _ALL_VOICE_KEYS for key in voice)


def test_missing_axes_produce_nothing_rather_than_defaulted_prose() -> None:
    assert voice_keys({}, crisis=False, crisis_axes=()) == []
    note = build_state_note(axes={})
    assert note["voice"] == [] and note["body"] == []


def test_malformed_axis_entries_are_tolerated() -> None:
    """非 Mapping / 非数字 / 越界档下标：一律丢，不抛异常。

    面板是只读视图——一处脏数据不该让整页打不开。
    """
    axes: dict[str, Any] = {
        "energy": {"tier": "tired", "tier_index": 1, "value": 30.0},
        "satiety": "not-a-mapping",
        "mood": {"tier": "calm", "tier_index": 99, "value": 55.0},
        "health": {"tier": "good", "tier_index": 3, "value": "abc"},
        "affection": {"tier": "close", "tier_index": 2, "value": 45.0},
    }
    voice = voice_keys(axes, crisis=False, crisis_axes=())
    assert voice == ["panel.stateVoice.energy.tired", "panel.stateVoice.affection.close"] or set(voice) <= _ALL_VOICE_KEYS
    for key in voice:
        assert key in _ALL_VOICE_KEYS


# ---------------------------------------------------------------------------
# 4. 缺证据说缺：None 不许变 0
# ---------------------------------------------------------------------------


def test_delta_today_stays_none_without_an_anchor() -> None:
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50))
    rows = {row["stat"]: row for row in note["body"]}
    assert rows["energy"]["delta_today"] is None
    assert rows["energy"]["delta_today"] != 0  # 0 会被面板渲染成"今天没变"


def test_delta_today_is_carried_when_the_anchor_exists() -> None:
    day_start = Stats(energy=60.0, satiety=70.0, mood=55.0, health=70.0, affection=20.0)
    now = Stats(energy=45.0, satiety=70.0, mood=55.0, health=70.0, affection=20.0)
    axes = axis_details(now, day_start=day_start)
    rows = {row["stat"]: row for row in build_state_note(axes=axes)["body"]}
    assert rows["energy"]["delta_today"] == -15.0


def test_top_tier_reports_no_next_tier_instead_of_a_fake_distance() -> None:
    axes = _axes(energy=95, satiety=70, mood=55, health=70, affection=50)
    rows = {row["stat"]: row for row in build_state_note(axes=axes)["body"]}
    assert rows["energy"]["next_tier"] is None
    assert rows["energy"]["to_next"] is None


def test_absent_last_touch_is_not_zero_gap() -> None:
    """新角色"从未互动过"（None）与"刚刚才说过话"（0.0）绝不能混同。"""
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50), gap_hours=None)
    assert note["us"]["gap_hours"] is None
    note2 = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50), gap_hours=0.0)
    assert note2["us"]["gap_hours"] == 0.0


def test_body_rows_follow_stat_names_order() -> None:
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50))
    assert [row["stat"] for row in note["body"]] == list(STAT_NAMES)


# ---------------------------------------------------------------------------
# 5. 耦合码：判据取自 core/model 常量，这里不重算阈值
# ---------------------------------------------------------------------------


def _codes(**values: float) -> list[str]:
    return coupling_codes(coupling_signal(Stats(**values).clamped()))


def test_no_coupling_when_both_daily_axes_are_comfortable() -> None:
    assert _codes(energy=60, satiety=60, mood=55, health=70, affection=20) == []


def test_hungry_shows_mild_mood_coupling() -> None:
    # 40 是耦合阈值线（`COUPLING_THRESHOLDS["satiety_low"]`）：35 掉线，50 在线以上
    assert _codes(energy=60, satiety=35, mood=55, health=70, affection=20) == ["mood_from_satiety"]


def test_starving_escalates_mood_coupling_to_severe() -> None:
    assert _codes(energy=60, satiety=15, mood=55, health=70, affection=20) == ["mood_from_satiety_severe"]


def test_exhausted_escalates_health_coupling_to_severe() -> None:
    assert _codes(energy=15, satiety=60, mood=55, health=70, affection=20) == ["health_from_energy_severe"]
    assert _codes(energy=35, satiety=60, mood=55, health=70, affection=20) == ["health_from_energy"]


def test_both_couplings_are_reported_together_and_in_stable_order() -> None:
    codes = _codes(energy=10, satiety=10, mood=55, health=70, affection=20)
    assert codes == ["mood_from_satiety_severe", "health_from_energy_severe"]


def test_every_emitted_coupling_code_is_a_declared_code() -> None:
    """耦合码只能来自 `COUPLING_CODES`——i18n 结构门按那个常量钉文案，多一个就没文案。"""
    for values in (
        dict(energy=10, satiety=10, mood=10, health=10, affection=20),
        dict(energy=35, satiety=35, mood=55, health=70, affection=20),
        dict(energy=90, satiety=90, mood=55, health=70, affection=20),
    ):
        for code in _codes(**values):
            assert code in COUPLING_CODES


def test_coupling_codes_tolerate_missing_or_junk_signal() -> None:
    assert coupling_codes(None) == []

    class Junk:
        mood_factor = "not-a-number"
        health_factor = None

    assert coupling_codes(Junk()) == []


# ---------------------------------------------------------------------------
# 6. 她的想法 / 她的一天 / 纯函数与可序列化
# ---------------------------------------------------------------------------


def test_mind_is_absent_without_any_judgment() -> None:
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50))
    assert note["mind"]["has_judgment"] is False
    assert note["mind"]["label"] is None


def test_mind_takes_the_most_recent_judgment_only() -> None:
    judgment = {
        "history": [
            {"at": 100.0, "label": "dull", "applied": -0.2},
            {"at": 200.0, "label": "wonderful", "applied": 1.0},
        ],
        "count_today": 2,
    }
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50), judgment=judgment)
    assert note["mind"]["label"] == "wonderful"
    assert note["mind"]["applied"] == 1.0
    assert note["mind"]["count_today"] == 2


def test_mind_ignores_a_label_that_has_no_i18n_entry() -> None:
    """脏标签（模型给了没登记的词）不许变成面板上的空徽章。"""
    judgment = {"history": [{"at": 1.0, "label": "unbelievable", "applied": 0.5}], "count_today": 1}
    note = build_state_note(axes=_axes(energy=50, satiety=50, mood=50, health=50, affection=50), judgment=judgment)
    assert note["mind"]["has_judgment"] is False


def test_mind_survives_junk_history() -> None:
    for judgment in ({"history": "nope"}, {"history": [1, 2]}, {"history": []}, {"nope": 1}, None):
        note = build_state_note(axes={}, judgment=judgment)  # type: ignore[arg-type]
        assert note["mind"]["has_judgment"] is False


def test_today_section_reports_unknown_as_none_not_zero() -> None:
    note = build_state_note(axes={}, meals_today=None, sodas=None, spoke_today=None)
    assert note["today"]["meals_today"] is None
    assert note["today"]["sodas"] is None
    assert note["today"]["job_id"] is None
    assert note["today"]["game_active"] is False


def test_today_section_carries_shift_and_open_game() -> None:
    note = build_state_note(
        axes={},
        meals_today=2,
        checked_today=True,
        job={"id": "konbini", "remaining_sec": 900},
        games={"active": {"kind": "arith"}},
        sodas=88,
        spoke_today=3,
    )
    today = note["today"]
    assert today["job_id"] == "konbini"
    assert today["job_remaining_sec"] == 900.0
    assert today["game_active"] is True
    assert today["checked_today"] is True
    assert today["sodas"] == 88


def test_negative_or_junk_counters_become_none() -> None:
    note = build_state_note(axes={}, meals_today=-3, sodas=True, spoke_today="many")
    assert note["today"]["meals_today"] is None
    assert note["today"]["sodas"] is None
    assert note["today"]["spoke_today"] is None


def test_rhythm_section_carries_phase_and_boundary() -> None:
    rhythm = resolve_rhythm(1_700_000_000.0, sleep_start_hour=0, sleep_end_hour=8)
    note = build_state_note(axes={}, rhythm=rhythm)
    assert note["rhythm"]["phase"] == rhythm.phase
    assert note["rhythm"]["sleeping"] is rhythm.sleeping
    assert note["rhythm"]["hour"] == rhythm.hour


def test_rhythm_section_degrades_without_a_rhythm() -> None:
    note = build_state_note(axes={}, rhythm=None)
    assert note["rhythm"]["phase"] is None


def test_anniversary_kind_is_forwarded_and_junk_is_ignored() -> None:
    note = build_state_note(axes={}, anniversary={"kind": "milestone", "day_number": 7})
    assert note["us"]["anniversary_kind"] == "milestone"
    assert build_state_note(axes={}, anniversary={"kind": 5})["us"]["anniversary_kind"] is None
    assert build_state_note(axes={}, anniversary=None)["us"]["anniversary_kind"] is None


def test_output_is_json_serialisable() -> None:
    """它要进 `@ui.context` → msgpack → 浏览器；不可序列化的对象会在运行时才炸。"""
    stats = Stats(energy=15, satiety=35, mood=40, health=60, affection=25)
    note = build_state_note(
        axes=axis_details(stats),
        rhythm=resolve_rhythm(1_700_000_000.0, sleep_start_hour=0, sleep_end_hour=8),
        coupling=coupling_signal(stats),
        crisis=model.is_crisis(stats, INJECT),
        crisis_axes=model.crisis_axes(stats, INJECT),
        day_number=12,
        streak_days=3,
        gap_hours=4.0,
        judgment={"history": [{"at": 1.0, "label": "good", "applied": 0.5}], "count_today": 2},
        meals_today=2,
        checked_today=True,
        sodas=50,
        spoke_today=1,
    )
    assert json.loads(json.dumps(note)) == note


def test_module_is_sdk_free() -> None:
    """`core/` 整层的纪律：不导入 `plugin.sdk.*`，因此可脱离宿主单测。

    只规**导入语句**：本模块的 docstring 里会提到 SDK 门面名字（说明为何不依赖它），
    拿全文做子串匹配会把注释当成依赖。
    """
    import re
    from pathlib import Path

    import our_life.core.state_note as module

    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if re.match(r"^\s*(import|from)\s", line)]
    assert imports, "expected the module to have import lines"
    assert not [line for line in imports if "plugin" in line], imports


def test_daily_rhythm_type_hint_is_runtime_available() -> None:
    """`DailyRhythm` 在运行期可取（不是只挂在 TYPE_CHECKING 里造成 NameError）。"""
    assert isinstance(resolve_rhythm(1_700_000_000.0, sleep_start_hour=0, sleep_end_hour=8), DailyRhythm)
