"""The fixer: practice run, staged apply, checks with real-metric rules, auto-undo."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from fakes import Clock, FakeExecutor, FakeProm, healthy
from incident_agent.domain.models import ActionSpec, ActionType, Environment, RiskLevel
from incident_agent.remediation.executor import DemoShopExecutor, ExecutorError
from incident_agent.remediation.runner import Runner, Timing, rollout_stages

TIMING = Timing(settle_seconds=60, window_seconds=120, stage_window_seconds=60)


def action(type_: ActionType, service: str = "checkout", **params) -> ActionSpec:
    return ActionSpec(type=type_, service=service, environment=Environment.PRODUCTION, params=params,
                      risk=RiskLevel.MEDIUM, reversible=True, blast_radius_pct=10)


ROLLBACK = action(ActionType.ROLLBACK_DEPLOYMENT, target_version="v2.3.9")
FLAG_10 = action(ActionType.TOGGLE_FEATURE_FLAG, flag="use_backup_payment_provider", value=True, rollout_pct=10)


def make_runner(prom: FakeProm, executor: FakeExecutor | None = None) -> tuple[Runner, FakeExecutor, Clock]:
    clock = Clock()
    executor = executor or FakeExecutor()
    return Runner(executor, prom, TIMING, now=clock.now, sleep=clock.sleep), executor, clock


def test_good_fix_is_verified_and_kept():
    runner, shop, _ = make_runner(FakeProm())
    result = runner.run(ROLLBACK, "checkout")
    assert result.status == "verified"
    assert shop.state["checkout"]["version"] == "v2.3.9"
    assert [s.kind for s in result.steps] == ["preview", "applied", "checked"]


def test_fix_that_adds_new_errors_is_undone_automatically():
    clock_start = Clock().now()
    prom = FakeProm(new_error_from=clock_start + timedelta(seconds=90))
    runner, shop, _ = make_runner(prom)
    result = runner.run(ROLLBACK, "checkout")
    assert result.status == "rolled_back"
    assert shop.state["checkout"]["version"] == "v2.4.0"  # back where it was
    assert result.final_verdict.should_roll_back
    assert [s.kind for s in result.steps][-1] == "undone"


def test_flag_goes_to_10_percent_first_then_everyone():
    runner, shop, _ = make_runner(FakeProm())
    result = runner.run(FLAG_10, "checkout")
    assert result.status == "verified"
    assert [n for n, _ in result.verdicts] == ["10%", "100%"]
    assert [a.params["rollout_pct"] for a in shop.applied] == [10, 100]


def test_harm_at_10_percent_stops_and_undoes_before_reaching_everyone():
    start = Clock().now()
    runner, shop, _ = make_runner(FakeProm(new_error_from=start + timedelta(seconds=70)))
    result = runner.run(FLAG_10, "checkout")
    assert result.status == "rolled_back"
    assert [n for n, _ in result.verdicts] == ["10%"]
    assert shop.state["checkout"]["flags"]["use_backup_payment_provider"] is False


def test_partial_stage_is_judged_on_harm_not_on_full_recovery():
    # Errors are still high at 10% (most traffic still hits the broken path) but no worse.
    def still_failing(signal, at):
        return 0.6 if signal == "http_5xx_rate" else healthy(signal, at)

    clock = Clock()
    prom = FakeProm(values=still_failing)
    runner = Runner(FakeExecutor(), prom, TIMING, now=clock.now, sleep=clock.sleep)
    result = runner.run(FLAG_10, "checkout")
    stage_one = result.verdicts[0][1]
    assert stage_one.status.value == "verified"
    assert result.status == "rolled_back"  # at 100% the error rate must really be fixed


def test_missing_metrics_never_count_as_success_and_nothing_is_widened():
    runner, shop, _ = make_runner(FakeProm(down=True))
    result = runner.run(FLAG_10, "checkout")
    assert result.status == "unverified"
    assert [a.params["rollout_pct"] for a in shop.applied] == [10]  # stayed at 10%
    assert result.steps[-1].kind == "stopped"


def test_failed_apply_is_undone():
    shop = FakeExecutor(fail_on_apply=1)
    runner, _, _ = make_runner(FakeProm(), shop)
    result = runner.run(ROLLBACK, "checkout")
    assert result.status == "apply_failed"
    assert result.steps[-1].kind == "undone"


def test_failed_undo_is_reported_loudly():
    shop = FakeExecutor(fail_on_apply=2)
    start = Clock().now()
    runner, _, _ = make_runner(FakeProm(new_error_from=start + timedelta(seconds=70)), shop)
    result = runner.run(ROLLBACK, "checkout")
    assert result.status == "undo_failed"
    assert "by hand" in result.steps[-1].message


def test_runner_waits_for_settle_and_window_before_judging():
    runner, _, clock = make_runner(FakeProm())
    start = clock.now()
    runner.run(ROLLBACK, "checkout")
    assert clock.now() - start >= timedelta(seconds=TIMING.settle_seconds + TIMING.window_seconds)


def test_only_partial_flags_are_staged():
    assert len(rollout_stages(FLAG_10)) == 2
    assert len(rollout_stages(ROLLBACK)) == 1
    off = action(ActionType.TOGGLE_FEATURE_FLAG, flag="x", value=False, rollout_pct=10)
    assert len(rollout_stages(off)) == 1


# --- the practice-shop executor over HTTP ------------------------------------------------


def shop_transport(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Admin-Token"] == "t"
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/admin/deploy":
            state["version"] = body["version"]
        elif path == "/admin/flags":
            state["flags"][body["flag"]] = body["value"]
            state["flag_rollout"][body["flag"]] = body["rollout_pct"]
        elif path == "/admin/scale":
            state["replicas"] = body["replicas"]
        elif path != "/admin/state":
            return httpx.Response(404)
        return httpx.Response(200, json=state)

    return httpx.MockTransport(handler)


@pytest.fixture
def shop():
    state = {"version": "v2.4.0", "flags": {}, "flag_rollout": {}, "replicas": 4}
    ex = DemoShopExecutor({"checkout": "http://shop"}, "t", httpx.Client(transport=shop_transport(state)))
    return ex, state


def test_practice_run_changes_nothing_and_knows_the_undo(shop):
    ex, state = shop
    preview = ex.preview(ROLLBACK)
    assert state["version"] == "v2.4.0"
    assert preview.before == {"version": "v2.4.0"} and preview.after == {"version": "v2.3.9"}
    assert preview.undo.params == {"target_version": "v2.4.0"}
    assert "v2.4.0 -> v2.3.9" in preview.changes[0]


def test_apply_and_undo_flag_round_trip(shop):
    ex, state = shop
    preview = ex.preview(FLAG_10)
    ex.apply(FLAG_10)
    assert state["flags"]["use_backup_payment_provider"] is True
    assert state["flag_rollout"]["use_backup_payment_provider"] == 10
    ex.apply(preview.undo)
    assert state["flags"]["use_backup_payment_provider"] is False


def test_unsupported_action_and_unknown_service_are_errors(shop):
    ex, _ = shop
    with pytest.raises(ExecutorError):
        ex.preview(action(ActionType.RESTART_SERVICE))
    with pytest.raises(ExecutorError):
        ex.preview(action(ActionType.SCALE_SERVICE, service="search", replicas=8))


def test_a_still_rising_error_rate_at_10_percent_is_not_called_harm():
    # Seen live: the outage was still ramping up when the 10% stage started.
    # Only new errors stop a partial stage; the full check at 100% judges recovery.
    from fakes import T0

    def ramping(signal, at):
        if signal != "http_5xx_rate":
            return healthy(signal, at)
        return 0.6 if at <= T0 else 0.9 if at < T0 + timedelta(seconds=150) else 0.0

    runner, shop, _ = make_runner(FakeProm(values=ramping))
    result = runner.run(FLAG_10, "checkout")
    assert [(n, v.status.value) for n, v in result.verdicts] == [("10%", "partially_verified"), ("100%", "verified")]
    assert result.status == "verified"
