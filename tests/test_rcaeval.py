"""The RCAEval scorer reads both dataset layouts and ranks the faulty service first.

The real dataset is downloaded on demand (see evals/rcaeval.py); these tests
use small cases built in the same format.
"""

from __future__ import annotations

import json

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from incident_agent.evals import rcaeval  # noqa: E402

SERVICES = ["frontend", "cartservice", "paymentservice", "emailservice"]
INJECT = 1_700_000_600


def _metrics(fault_service: str, seed: int = 0, spike_service: str = "") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(INJECT - 600, INJECT + 300)
    data = {"time": t}
    for s in SERVICES:
        cpu = rng.normal(0.3, 0.02, len(t))
        lat = rng.normal(50, 3, len(t))
        if s == fault_service:
            cpu[t >= INJECT] += 0.5  # the fault: CPU jumps and stays up
        if s == "frontend" and fault_service:
            lat[t >= INJECT] += 20  # callers get a bit slower too
        if s == spike_service:
            cpu[t == INJECT + 10] = 50.0  # one absurd sample, gone right after
        data[f"{s}_cpu"] = cpu
        data[f"{s}_latency-90"] = lat
    return pd.DataFrame(data)


def _write_case(path, df: pd.DataFrame, parquet: bool) -> None:
    path.mkdir(parents=True)
    (path / "inject_time.txt").write_text(str(INJECT))
    if parquet:
        df.to_parquet(path / "metrics.parquet") if _has_parquet() else df.to_csv(path / "metrics.csv", index=False)
    else:
        cols = {c: [[int(t), float(v)] for t, v in zip(df["time"], df[c])] for c in df.columns if c != "time"}
        (path / "metrics.json").write_text(json.dumps(cols))


def _has_parquet() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def test_both_layouts_are_found_with_their_ground_truth(tmp_path):
    _write_case(tmp_path / "re1ob_paymentservice_cpu_1", _metrics("paymentservice"), parquet=True)
    _write_case(tmp_path / "RE1" / "RE1-OB" / "cartservice_cpu" / "2", _metrics("cartservice", 1), parquet=False)
    cases = {c.name: c for c in rcaeval.find_cases(tmp_path)}
    assert cases["re1ob_paymentservice_cpu_1"].service == "paymentservice"
    assert cases["re1ob_paymentservice_cpu_1"].suite == "RE1-OB"
    assert (cases["2"].service, cases["2"].fault, cases["2"].suite) == ("cartservice", "cpu", "RE1-OB")
    assert all(c.inject_time == INJECT for c in cases.values())


def test_both_rankers_put_the_faulty_service_first_and_scores_add_up(tmp_path):
    for i, svc in enumerate(["paymentservice", "cartservice", "emailservice"]):
        _write_case(tmp_path / f"re1ob_{svc}_cpu_{i}", _metrics(svc, i), parquet=False)
    results = rcaeval.run(rcaeval.find_cases(tmp_path), ["baro", "ours"], progress=False)
    for r in results:
        assert r.ranks["baro"][0] == r.case.service
        assert r.ranks["ours"][0] == r.case.service
    s = rcaeval.score(results)
    assert s["all"]["cases"] == 3
    assert s["all"]["ours"]["AC@1"] == 1.0 and s["all"]["ours"]["Avg@5"] == 1.0


def test_one_noisy_sample_misleads_baro_but_not_ours(tmp_path):
    # emailservice is at fault; cartservice has a single absurd CPU sample.
    _write_case(tmp_path / "re1ob_emailservice_cpu_1", _metrics("emailservice", 3, spike_service="cartservice"),
                parquet=False)
    (r,) = rcaeval.run(rcaeval.find_cases(tmp_path), ["baro", "ours"], progress=False)
    assert r.ranks["baro"][0] == "cartservice"
    assert r.ranks["ours"][0] == "emailservice"


def test_failing_traces_point_at_where_errors_start(tmp_path):
    case_dir = tmp_path / "re2ob_paymentservice_loss_1"
    _write_case(case_dir, _metrics("", 4), parquet=False)  # metrics alone show nothing
    rows = []
    for i in range(40):
        at = (INJECT + 5 + i) * 1000
        rows += [  # frontend -> checkout -> payment; payment fails, the error passes up
            {"traceID": f"t{i}", "spanID": f"a{i}", "parentSpanID": None, "serviceName": "frontend",
             "startTimeMillis": at, "statusCode": 500},
            {"traceID": f"t{i}", "spanID": f"b{i}", "parentSpanID": f"a{i}", "serviceName": "cartservice",
             "startTimeMillis": at, "statusCode": 500},
            {"traceID": f"t{i}", "spanID": f"c{i}", "parentSpanID": f"b{i}", "serviceName": "paymentservice",
             "startTimeMillis": at, "statusCode": 13},  # a gRPC error code
        ]
    pd.DataFrame(rows).to_csv(case_dir / "traces.csv", index=False)
    (case,) = rcaeval.find_cases(tmp_path)
    assert rcaeval._trace_ranking(case)[0] == "paymentservice"
    (r,) = rcaeval.run([case], ["ours"], progress=False)
    assert r.ranks["ours"][0] == "paymentservice"


def test_names_match_across_sources():
    assert rcaeval.service_of("ts-auth-service_latency-90") == "ts-auth-service"
    assert rcaeval.hit_at(["frontend", "checkout"], "checkoutservice", 2)
    assert rcaeval._canonical("frontendservice", ["frontend", "cartservice"]) == "frontend"
