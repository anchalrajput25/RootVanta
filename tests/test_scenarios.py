from pathlib import Path

from incident_agent.evals.runner import main, run_all

SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "scenarios"


def test_every_scenario_variant_matches_its_expected_outcome():
    card = run_all(SCENARIOS)
    assert len(card.results) >= 10
    assert card.mismatches == [], [(r.scenario_id, r.variant_id, r.summary) for r in card.mismatches]


def test_agent_has_zero_false_successes_while_naive_baseline_does_not():
    card = run_all(SCENARIOS)
    assert card.not_recovered, "suite must contain cases where the fix did not really work"
    assert card.agent_false_successes == 0
    assert card.baseline_false_successes > 0


def test_no_unsafe_action_is_executed():
    assert run_all(SCENARIOS).unsafe_actions_executed == 0


def test_cli_exits_cleanly(capsys):
    assert main([str(SCENARIOS)]) == 0
    assert "false-success rate" in capsys.readouterr().out
