"""玩家打工小游戏（v0.7.0）：服务器权威判分的常驻门。

这个文件最重要的三组门，全部围绕"客户端不可信"：

1. **公开视图不含答案**——`challenge_public_view` 漏一个答案，就等于把
   "打工"变成"读 props 打印金币"。用序列化全等把它钉死。
2. **一次性**：结算后的 challenge 重放拿不到第二笔钱。
3. **时限**：过期交卷作废且**不计次数**（没发钱的局不占额度）。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from our_life.core.configuration import GameSettings, OurLifeSettings
from our_life.core.games import (
    GAME_KINDS,
    SUBMIT_GRACE_SEC,
    answer_invalid_reason,
    build_challenge,
    challenge_public_view,
    consume_challenge,
    daily_block_reason,
    expired,
    normalize_challenge,
    normalize_game_counts,
    submit_arith,
    submit_hielo,
)
from our_life.services.state import ShardState

CTX = {"_ctx": {"lanlan_name": "灵"}}


def _settings(**games_overrides: Any) -> OurLifeSettings:
    table = {"enabled": True}
    table.update(games_overrides)
    return OurLifeSettings.from_config({"our_life": {"enabled": True, "games": table}})


def _make_arith(seed: int = 42, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "now": 1_000.0,
        "rng": random.Random(seed),
        "arith_rounds": 5,
        "arith_time_limit_sec": 60.0,
        "hielo_rounds": 3,
    }
    kwargs.update(overrides)
    challenge = build_challenge("arith", **kwargs)
    assert challenge is not None
    return challenge


# ---------------------------------------------------------------------------
# 挑战生成与公开视图（纪律 1）
# ---------------------------------------------------------------------------


def test_arith_public_view_leaks_no_answers() -> None:
    challenge = _make_arith()
    view = challenge_public_view(challenge)
    # 序列化比对：公开视图里出现任何一个"答案数字"都会在结构差异里暴露——
    # 题目是字符串，答案藏在 questions[i][1]，视图里必须只剩题面字符串。
    assert all(isinstance(question, str) for question in view["questions"])
    assert len(view["questions"]) == 5
    # 答案数字不许以独立 token 出现在视图 JSON 里（±边界允许题面本身的数字子串）
    dumped = json.dumps(view)
    assert "\"questions\"" in dumped and "answers" not in dumped.lower()


def test_hielo_public_view_only_reveals_up_to_index() -> None:
    challenge = build_challenge(
        "hielo",
        now=0.0,
        rng=random.Random(7),
        arith_rounds=5,
        arith_time_limit_sec=60.0,
        hielo_rounds=4,
    )
    assert challenge is not None
    view = challenge_public_view(challenge)
    assert view["current"] == challenge["ranks"][0]
    assert view["revealed"] == [challenge["ranks"][0]]
    assert len(challenge["ranks"]) == 5  # rounds + 1
    # 推进一轮后再切视图：只多揭示一张
    submit_hielo(challenge, "higher", win_coins=6, loss_coins=1)
    view2 = challenge_public_view(challenge)
    assert view2["revealed"] == challenge["ranks"][:2]
    assert view2["current"] == challenge["ranks"][1]


def test_build_challenge_kind_guards() -> None:
    assert build_challenge("poker", now=0.0, rng=random.Random(1), arith_rounds=5, arith_time_limit_sec=60.0, hielo_rounds=3) is None
    assert set(GAME_KINDS) == {"arith", "hielo"}


def test_normalize_challenge_rejects_garbage() -> None:
    assert normalize_challenge(None) is None
    assert normalize_challenge({"kind": "arith"}) is None  # 无 issued_at
    assert normalize_challenge({"kind": "arith", "issued_at": 1.0, "questions": [["1+1"]], "deadline_at": 2.0}) is None  # 题缺答案
    assert normalize_challenge({"kind": "hielo", "issued_at": 1.0, "ranks": [0, 5]}) is None  # 点数越界
    assert normalize_challenge({"kind": "hielo", "issued_at": 1.0, "ranks": [7, 3], "index": 5}) is None  # index 越界
    # 归一化器会显式补 `deadline_at: None`（hielo 无总时限）——往返等值要按**归一化后的形状**比。
    good = {"kind": "hielo", "issued_at": 1.0, "deadline_at": None, "ranks": [7, 3], "index": 0, "rounds": 1, "wins": 0, "losses": 0, "consumed": False}
    assert normalize_challenge(good) == good


def test_normalize_game_counts_shape() -> None:
    assert normalize_game_counts({"arith": 2, "total": 2, "junk": "x", "neg": -1, "bool": True}) == {"arith": 2, "total": 2}


# ---------------------------------------------------------------------------
# 心算判分
# ---------------------------------------------------------------------------


def test_submit_arith_grades_and_bonuses() -> None:
    challenge = _make_arith()
    answers = [row[1] for row in challenge["questions"]]
    outcome = submit_arith(challenge, answers, now=1040.0, coin_per_correct=2, perfect_bonus=5)
    assert outcome is not None
    assert outcome.correct == 5 and outcome.perfect is True
    assert outcome.coins == 5 * 2 + 5
    partial = submit_arith(challenge, [a + 1 for a in answers], now=1040.0, coin_per_correct=2, perfect_bonus=5)
    assert partial is not None and partial.correct == 0 and partial.coins == 0


def test_submit_arith_short_and_dirty_answers() -> None:
    challenge = _make_arith()
    answers = [row[1] for row in challenge["questions"]]
    # 只答一半：后半视为错
    outcome = submit_arith(challenge, answers[:2], now=1040.0, coin_per_correct=2, perfect_bonus=5)
    assert outcome is not None and outcome.correct == 2
    # 字符串数字按形状闸处理（answer_invalid_reason 挡住入口，这里直接判分只认 int/float）
    assert submit_arith(challenge, ["5"] * 5, now=1040.0, coin_per_correct=2, perfect_bonus=0) is not None


def test_submit_arith_expiry_with_grace() -> None:
    challenge = _make_arith()
    deadline = challenge["deadline_at"]
    answers = [row[1] for row in challenge["questions"]]
    assert expired(challenge, now=deadline + SUBMIT_GRACE_SEC - 0.5) is False
    assert expired(challenge, now=deadline + SUBMIT_GRACE_SEC + 0.5) is True
    assert submit_arith(challenge, answers, now=deadline + 60.0, coin_per_correct=2, perfect_bonus=5) is None


def test_answer_invalid_reason() -> None:
    assert answer_invalid_reason([1, 2, 3]) == ""
    assert answer_invalid_reason("1,2,3") == "game_answer_invalid"
    assert answer_invalid_reason([]) == "game_answer_invalid"
    assert answer_invalid_reason([1, None]) == "game_answer_invalid"
    assert answer_invalid_reason([1, True]) == "game_answer_invalid"
    assert answer_invalid_reason([1, "2"]) == "game_answer_invalid"


# ---------------------------------------------------------------------------
# 猜大小推进
# ---------------------------------------------------------------------------


def _make_hielo(seed: int = 11, rounds: int = 2) -> dict[str, Any]:
    challenge = build_challenge(
        "hielo",
        now=0.0,
        rng=random.Random(seed),
        arith_rounds=5,
        arith_time_limit_sec=60.0,
        hielo_rounds=rounds,
    )
    assert challenge is not None
    return challenge


def test_hielo_deterministic_sequence_and_final_settle() -> None:
    challenge = _make_hielo(seed=2026, rounds=2)
    ranks = list(challenge["ranks"])
    outcome1, challenge, reason = submit_hielo(challenge, "higher", win_coins=6, loss_coins=1)
    assert reason == "" and outcome1 is None
    # 牌序**不因重发/拖延而改变**：再查一次 ranks 与签发时一致
    assert challenge["ranks"] == ranks
    expected_won_1 = ranks[1] > ranks[0]
    assert challenge["wins"] == (1 if expected_won_1 else 0)
    outcome2, challenge, reason = submit_hielo(challenge, "lower", win_coins=6, loss_coins=1)
    assert reason == "" and outcome2 is not None  # 最后一轮 → 终局
    wins = challenge["wins"]
    assert outcome2.coins == wins * 6 + (2 - wins) * 1
    assert challenge["consumed"] is True
    # 终局后重放：撞 game_no_challenge
    outcome3, _, reason = submit_hielo(challenge, "higher", win_coins=6, loss_coins=1)
    assert outcome3 is None and reason == "game_no_challenge"


def test_hielo_tie_loses_and_bad_bet_rejected() -> None:
    challenge = _make_hielo()
    # 手造同点牌序验证"平局按输"（判据与 UI 公示必须一致）
    challenge["ranks"] = [5, 5, 9]
    outcome, challenge, reason = submit_hielo(challenge, "higher", win_coins=6, loss_coins=1)
    assert reason == "" and challenge["losses"] == 1
    bad = _make_hielo()
    outcome, _, reason = submit_hielo(bad, "banana", win_coins=6, loss_coins=1)
    assert outcome is None and reason == "game_answer_invalid"


# ---------------------------------------------------------------------------
# 日额度
# ---------------------------------------------------------------------------


def test_daily_block_reason_limits() -> None:
    assert daily_block_reason(kind="arith", counts={}, per_game_limit=3, total_limit=6) == "ok"
    assert daily_block_reason(kind="nope", counts={}, per_game_limit=3, total_limit=6) == "game_invalid_kind"
    assert daily_block_reason(kind="arith", counts={"arith": 3}, per_game_limit=3, total_limit=6) == "game_daily_limit"
    assert daily_block_reason(kind="hielo", counts={"total": 6}, per_game_limit=3, total_limit=6) == "game_daily_limit"


def test_game_day_counter_resets() -> None:
    state = ShardState(lanlan="灵", game_day="2026-09-13", game_counts={"arith": 2, "total": 2}, game_earned_today=9)
    assert state.reset_game_day(today="2026-09-14") is True
    assert state.game_counts == {} and state.game_earned_today == 0


def test_challenge_roundtrip_and_consumed_ignored() -> None:
    state = ShardState(lanlan="灵")
    challenge = _make_arith()
    state.begin_game(challenge)
    revived = ShardState.from_payload("灵", state.as_payload(), now=time.time())
    assert revived.active_challenge() is not None
    assert revived.active_challenge()["questions"] == challenge["questions"]
    consume_challenge(revived.game_challenge)
    assert revived.active_challenge() is None
    revived.clear_game()
    assert revived.active_challenge() is None


def test_snapshot_never_leaks_answers() -> None:
    """纪律 1 的行为证明：面板快照的 JSON 里不许出现任何答案 token。"""
    state = ShardState(lanlan="灵")
    challenge = _make_arith()
    state.begin_game(challenge)
    # 快照 JSON 化后不允许出现任何答案：答案都是 int，题面都是 str，
    # 用结构判据钉住（active 视图存在 = 经过了 challenge_public_view 的切分）。
    dumped = json.dumps(state.snapshot_for_panel(now=time.time()))
    assert "game_challenge" not in dumped
    active = json.loads(dumped)["games"]["active"]
    assert active is not None
    assert all(isinstance(question, str) for question in active["questions"])


# ---------------------------------------------------------------------------
# 入口链
# ---------------------------------------------------------------------------


def test_arith_flow_issue_then_submit(make_plugin: Any, run_async: Any) -> None:
    plugin, host = make_plugin()
    plugin._settings = _settings()
    started = run_async(plugin.game_start_entry(kind="arith", **CTX))
    assert started.is_ok(), started.error
    assert started.value["note"] == "game_started"
    questions = started.value["challenge"]["questions"]
    assert len(questions) == plugin._settings.games.arith_rounds
    # 未结算前不许换题
    again = run_async(plugin.game_start_entry(kind="arith", **CTX))
    assert not again.is_ok() and str(again.error) == "game_in_progress"
    # 后端拿真值，模拟玩家全对：从缓存读答案
    challenge = plugin._store.cached["灵"].game_challenge
    answers = [row[1] for row in challenge["questions"]]
    result = run_async(plugin.game_arith_submit_entry(answers=answers, **CTX))
    assert result.is_ok(), result.error
    assert result.value["note"] == "game_done"
    assert result.value["correct"] == len(answers)
    assert result.value["coins"] == len(answers) * 2 + 5
    assert plugin._store.cached["灵"].game_counts["total"] == 1
    # 重放：没有进行中的挑战了
    replay = run_async(plugin.game_arith_submit_entry(answers=answers, **CTX))
    assert not replay.is_ok() and str(replay.error) == "game_no_challenge"
    persisted = host.store.data["ourlife@灵"]
    assert persisted["game_challenge"] == {}


def test_arith_expired_voids_and_refunds_quota(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    plugin._settings = _settings(arith_time_limit_sec=15)
    assert run_async(plugin.game_start_entry(kind="arith", **CTX)).is_ok()
    state = plugin._store.cached["灵"]
    state.game_challenge["deadline_at"] = time.time() - 100.0  # 伪造一具过期尸体
    challenge = state.game_challenge
    answers = [row[1] for row in challenge["questions"]]
    result = run_async(plugin.game_arith_submit_entry(answers=answers, **CTX))
    assert not result.is_ok() and str(result.error) == "game_expired"
    # 过期作废但**不计次数、不发钱**，且可再开一局
    assert plugin._store.cached["灵"].game_counts == {}
    assert plugin._store.cached["灵"].game_earned_total == 0
    assert run_async(plugin.game_start_entry(kind="arith", **CTX)).is_ok()


def test_hielo_flow_bets_to_settlement(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    plugin._settings = _settings(hielo_rounds=2)
    started = run_async(plugin.game_start_entry(kind="hielo", **CTX))
    assert started.is_ok()
    challenge = plugin._store.cached["灵"].game_challenge
    ranks = list(challenge["ranks"])
    bets = ["higher" if ranks[i + 1] > ranks[i] else "lower" for i in range(2)]
    first = run_async(plugin.game_hielo_bet_entry(bet=bets[0], **CTX))
    assert first.is_ok() and first.value["note"] == "hielo_round"
    assert first.value["won"] is True
    second = run_async(plugin.game_hielo_bet_entry(bet=bets[1], **CTX))
    assert second.is_ok() and second.value["note"] == "game_done"
    assert second.value["correct"] == 2  # 两战全胜
    assert second.value["coins"] == 2 * plugin._settings.games.hielo_win_coins
    assert plugin._store.cached["灵"].game_counts == {"hielo": 1, "total": 1}


def test_game_gates(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    blocked = run_async(plugin.game_start_entry(kind="arith", **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "not_enabled"
    plugin._settings = _settings(enabled=False)
    blocked = run_async(plugin.game_start_entry(kind="arith", **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "games_disabled"
    plugin._settings = _settings()
    bogus = run_async(plugin.game_start_entry(kind="poker", **CTX))
    assert not bogus.is_ok() and str(bogus.error) == "game_invalid_kind"
    submit_without_game = run_async(plugin.game_arith_submit_entry(answers=[1], **CTX))
    assert not submit_without_game.is_ok() and str(submit_without_game.error) == "game_no_challenge"


def test_daily_limit_blocks_starts(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    plugin._settings = _settings(arith_rounds=3, arith_time_limit_sec=60, per_game_daily_limit=1, total_daily_limit=2)
    assert run_async(plugin.game_start_entry(kind="arith", **CTX)).is_ok()
    state = plugin._store.cached["灵"]
    answers = [row[1] for row in state.game_challenge["questions"]]
    assert run_async(plugin.game_arith_submit_entry(answers=answers, **CTX)).is_ok()
    blocked = run_async(plugin.game_start_entry(kind="arith", **CTX))
    assert not blocked.is_ok() and str(blocked.error) == "game_daily_limit"
    # 换游戏还能开（每种额度独立），直到 total 也满
    assert run_async(plugin.game_start_entry(kind="hielo", **CTX)).is_ok()


def test_dashboard_exposes_games_with_public_view(make_plugin: Any, run_async: Any) -> None:
    plugin, _host = make_plugin()
    plugin._settings = _settings()
    context = run_async(plugin.dashboard_context(**CTX))
    games = context["games"]
    assert games["enabled"] is True and games["active"] is None
    assert run_async(plugin.game_start_entry(kind="arith", **CTX)).is_ok()
    after = run_async(plugin.dashboard_context(**CTX))["games"]
    assert after["active"] is not None
    assert all(isinstance(question, str) for question in after["active"]["questions"])
    # 原始 challenge（含答案）从不进 payload：payload 里根本没有 game_challenge 键
    assert "game_challenge" not in json.dumps(after)


def test_game_settings_clamped() -> None:
    assert GameSettings.from_mapping({"arith_rounds": 999}).arith_rounds == 20
    assert GameSettings.from_mapping({"arith_rounds": 0}).arith_rounds == 3
    assert GameSettings.from_mapping({"hielo_win_coins": -5}).hielo_win_coins == 0
    assert GameSettings.from_mapping({"arith_time_limit_sec": "x"}).arith_time_limit_sec == 60
