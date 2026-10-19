"""The practice benchmark runs, and the agent stays rarely wrong on it.

Thresholds are set below the measured results (evals/RESULTS.md) so a change
that makes the investigator worse fails here.
"""

from incident_agent.evals import practice


def test_practice_benchmark_is_rarely_wrong_and_never_false_alarms():
    s = practice.summarize(practice.run(cases=120, seed=99))
    assert s["false_alarms"] == 0
    assert s["wrong"] <= 0.05
    assert s["correct"] >= 0.80
    assert s["fix_ok"] == 1.0


def test_practice_cases_are_reproducible():
    a, b = practice.generate(5, 3), practice.generate(5, 3)
    assert (a.fault, a.alerting, a.expected) == (b.fault, b.alerting, b.expected)
