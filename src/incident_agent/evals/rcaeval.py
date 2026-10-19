"""Score root-cause ranking on RCAEval, a public benchmark of real microservice failures.

RCAEval (Pham et al., WWW 2025; https://github.com/phamquiluan/RCAEval) has
735 failure cases recorded from three demo systems (Online Boutique, Sock
Shop, Train Ticket). In each case a fault was injected into one service at a
known time: CPU, memory, disk, network delay, packet loss, socket, or a code
bug. The task is to rank the services so the faulty one comes first.

    RE1  metrics only                       375 cases
    RE2  metrics, logs and traces           270 cases
    RE3  code-level faults, all three        90 cases

Two rankers are scored on the same cases with the same rules:

- ``baro``: the published BARO method (FSE 2024), re-implemented here from
  RCAEval's code: for every metric, how far it moved after the fault time,
  measured against its normal spread (median and IQR) before it.
- ``ours``: the same per-metric scores, but a change must last at least
  ``PERSIST`` samples (one noisy sample is not a fault, as in the
  investigator), and, when the case has them, two more rankings are fused in
  from error logs and from where failing traces start (the deepest failing
  call, as in the investigator's trace step). The rankings are combined with
  reciprocal rank fusion, which needs no tuning of scales between sources.

Scores: AC@k is the share of cases where the faulty service is in the top k;
Avg@5 is the mean of AC@1 to AC@5 (the paper's main number).

Download one suite and score it (needs ``pip install -e ".[bench]"``):

    python -m incident_agent.evals.rcaeval --download re1 --data data/rcaeval
    python -m incident_agent.evals.rcaeval --data data/rcaeval --json results.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:  # optional: only this benchmark needs them
    import numpy as np
    import pandas as pd
except ImportError:  # pragma: no cover
    np = pd = None  # type: ignore[assignment]

HF_REPO = "phamquiluan/RCAEval"
FAULTS = ("cpu", "mem", "disk", "socket", "delay", "loss", "f1", "f2", "f3", "f4", "f5")
NAME = re.compile(r"^(?P<suite>re\d)(?P<system>ob|ss|tt)_(?P<service>.+)_(?P<fault>[a-z]+\d*)_(?P<n>\d+)$")
PERSIST = 3  # samples a change must last (RCAEval metrics are sampled every second)
RRF_K = 60  # reciprocal rank fusion constant, the usual default
MAX_K = 5
# Columns that describe the whole system, not one service.
IGNORED_PREFIXES = ("time", "main_", "PassthroughCluster", "istio-proxy", "frontend-external")


@dataclass
class Case:
    name: str
    path: Path
    suite: str  # RE1-OB, RE2-TT, ...
    service: str  # the faulty service (ground truth)
    fault: str
    inject_time: int


@dataclass
class Result:
    case: Case
    ranks: dict[str, list[str]]  # ranker -> services, best first
    seconds: dict[str, float] = field(default_factory=dict)


# --- finding and reading cases -------------------------------------------------------


def _need_pandas() -> None:
    if pd is None:
        raise SystemExit('This benchmark needs pandas, numpy and pyarrow: pip install -e ".[bench]"')


def find_cases(root: Path) -> list[Case]:
    """Every case under ``root``, in either layout (Hugging Face Parquet or the original Zenodo files)."""
    index = _index(root)
    cases = []
    for marker in sorted(root.rglob("*")):
        if not marker.is_dir() or not any((marker / f).exists() for f in
                                          ("metrics.parquet", "metrics.json", "metrics.csv", "data.csv")):
            continue
        case = _case(marker, index)
        if case:
            cases.append(case)
    return cases


def _index(root: Path) -> dict[str, dict[str, Any]]:
    path = next(iter(sorted(root.rglob("cases.parquet"))), None)
    if path is None:
        return {}
    df = pd.read_parquet(path)
    return {str(r["case"]): r for r in df.to_dict("records")} if "case" in df else {}


def _case(path: Path, index: dict[str, dict[str, Any]]) -> Optional[Case]:
    inject = path / "inject_time.txt"
    row = index.get(path.name)
    m = NAME.match(path.name)
    if m:  # Hugging Face layout: re2ob_checkoutservice_cpu_1
        suite = f"{m['suite']}-{m['system']}".upper()
        service, fault = m["service"], m["fault"]
    elif path.parent.name.rsplit("_", 1)[-1] in FAULTS:  # original layout: RE2/RE2-OB/checkoutservice_cpu/1
        service, fault = path.parent.name.rsplit("_", 1)
        suite = path.parent.parent.name.upper()
    else:
        return None
    if row is not None:
        service = str(row.get("root_cause_service") or service)
        fault = str(row.get("fault") or fault)
        suite = str(row.get("dataset") or suite).upper()
    if inject.exists():
        t = int(float(inject.read_text().strip()))
    elif row is not None and any(k in row for k in ("inject_time", "injection_time")):
        t = int(row.get("inject_time") or row.get("injection_time"))
    else:
        return None
    return Case(name=path.name, path=path, suite=suite, service=service, fault=fault, inject_time=t)


def read_metrics(case: Case) -> "pd.DataFrame":
    p = case.path
    if (p / "metrics.parquet").exists():
        df = pd.read_parquet(p / "metrics.parquet")
    elif (p / "metrics.json").exists():
        raw = {k: v for k, v in json.loads((p / "metrics.json").read_text()).items() if v}
        frames = [pd.Series({int(t): v for t, v in pts}, name=k, dtype=float) for k, pts in raw.items()]
        df = pd.concat(frames, axis=1).sort_index().rename_axis("time").reset_index()
    else:
        df = pd.read_csv(p / ("metrics.csv" if (p / "metrics.csv").exists() else "data.csv"))
    return df.sort_values("time").reset_index(drop=True)


def _read_table(case: Case, kind: str, columns: list[str]) -> Optional["pd.DataFrame"]:
    for name in (f"{kind}.parquet", f"{kind}.csv"):
        f = case.path / name
        if f.exists():
            if name.endswith(".parquet"):
                df = pd.read_parquet(f)
                return df[[c for c in columns if c in df.columns]]
            return pd.read_csv(f, usecols=lambda c: c in columns, low_memory=False)
    return None


def service_of(column: str) -> str:
    """'checkoutservice_cpu' -> 'checkoutservice'; 'ts-auth-service_latency-90' -> 'ts-auth-service'."""
    return column.rsplit("_", 1)[0]


# --- rankers -------------------------------------------------------------------------


def _metric_scores(df: "pd.DataFrame", inject_time: int, persist: int) -> dict[str, float]:
    """Highest robust z-score each metric reached after the fault time.

    With persist > 1 the z-score must hold for that many samples in a row
    (the rolling minimum), so a single noisy sample cannot win.
    """
    cols = [c for c in df.columns if not c.startswith(IGNORED_PREFIXES)]
    data = df[cols].apply(pd.to_numeric, errors="coerce")
    data = data.loc[:, data.nunique(dropna=True) > 1]  # constant columns say nothing
    for c in data.columns:
        if c.endswith("_mem"):
            data[c] = data[c] / 1e6  # as BARO does: memory in MB
    before, after = data[df["time"] < inject_time], data[df["time"] >= inject_time]
    scores = {}
    for c in data.columns:
        a, b = before[c].dropna().to_numpy(), after[c].ffill().dropna().to_numpy()
        if len(a) < 2 or len(b) < persist:
            continue
        median = np.median(a)
        iqr = np.subtract(*np.percentile(a, [75, 25]))
        z = (b - median) / (iqr if iqr > 0 else 1.0)
        if persist > 1:
            z = pd.Series(z).rolling(persist).min().dropna().to_numpy()
        if len(z):
            scores[c] = float(np.max(z))
    return scores


def _services_by_best(scores: dict[str, float]) -> list[str]:
    best: dict[str, float] = {}
    for col, s in scores.items():
        svc = service_of(col)
        best[svc] = max(best.get(svc, float("-inf")), s)
    return sorted(best, key=lambda k: best[k], reverse=True)


def rank_baro(case: Case, metrics: "pd.DataFrame") -> list[str]:
    """BARO as published: rank metrics by their highest robust z-score; services in the order they first appear."""
    return _services_by_best(_metric_scores(metrics, case.inject_time, persist=1))


def _log_ranking(case: Case) -> list[str]:
    """Services whose error-looking log lines rose most after the fault time (relative to before)."""
    logs = _read_table(case, "logs", ["time", "timestamp", "container_name", "message", "level"])
    if logs is None or logs.empty or "container_name" not in logs:
        return []
    if "timestamp" in logs and pd.api.types.is_numeric_dtype(logs["timestamp"]):
        t = logs["timestamp"].astype("float64") / 1e9
    else:
        return []
    text = logs.get("level", pd.Series("", index=logs.index)).astype(str) + " " + \
        logs.get("message", pd.Series("", index=logs.index)).astype(str)
    bad = text.str.contains(r"error|exception|fail|refused|timeout", case=False, regex=True)
    span_before = max(1.0, case.inject_time - float(t.min()))
    span_after = max(1.0, float(t.max()) - case.inject_time)
    rates = {}
    for svc, group in logs[bad].groupby("container_name"):
        tt = t.loc[group.index]
        rb, ra = (tt < case.inject_time).sum() / span_before, (tt >= case.inject_time).sum() / span_after
        if ra > rb:
            rates[str(svc)] = (ra + 1e-3) / (rb + 1e-3)
    return sorted(rates, key=lambda k: rates[k], reverse=True)


def _trace_ranking(case: Case) -> list[str]:
    """Services where failing calls start (the deepest failing span), more after the fault time than before."""
    tr = _read_table(case, "traces", ["traceID", "spanID", "parentSpanID", "serviceName", "startTime",
                                      "startTimeMillis", "statusCode"])
    if tr is None or tr.empty or "statusCode" not in tr:
        return []
    code = pd.to_numeric(tr["statusCode"], errors="coerce").fillna(0)
    tr = tr.assign(error=(code >= 400) | ((code > 0) & (code < 100)))  # HTTP 4xx/5xx, or a gRPC error code
    failing = tr[tr["error"]]
    if failing.empty:
        return []
    failing_ids = set(failing["spanID"])
    # A failing span whose child also failed only passed the error on.
    passed_on = set(failing.loc[failing["parentSpanID"].isin(failing_ids), "parentSpanID"])
    origins = failing[~failing["spanID"].isin(passed_on)]
    if "startTimeMillis" in origins:
        at = pd.to_numeric(origins["startTimeMillis"], errors="coerce") / 1e3
    else:
        at = pd.to_numeric(origins["startTime"], errors="coerce") / 1e6
    svc = origins["serviceName"].astype(str).str.replace("service$", "", regex=False)
    after = svc[at >= case.inject_time].value_counts()
    before = svc[at < case.inject_time].value_counts()
    growth = {s: n - before.get(s, 0) for s, n in after.items() if n > before.get(s, 0)}
    return sorted(growth, key=lambda k: growth[k], reverse=True)


def _fuse(rankings: list[list[str]]) -> list[str]:
    score: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for i, svc in enumerate(ranking):
            score[svc] += 1.0 / (RRF_K + i + 1)
    return sorted(score, key=lambda k: score[k], reverse=True)


def _canonical(name: str, known: list[str]) -> Optional[str]:
    """Map a log or trace service name onto a metrics service name ('frontendservice' -> 'frontend')."""
    if name in known:
        return name
    for k in known:
        if name.startswith(k) or k.startswith(name):
            return k
    return None


def rank_ours(case: Case, metrics: "pd.DataFrame") -> list[str]:
    by_metrics = _services_by_best(_metric_scores(metrics, case.inject_time, persist=PERSIST))
    rankings = [by_metrics]
    for extra in (_log_ranking(case), _trace_ranking(case)):
        mapped = [m for m in (_canonical(s, by_metrics) for s in extra) if m]
        if mapped:
            rankings.append(list(dict.fromkeys(mapped)))
    return _fuse(rankings) if len(rankings) > 1 else by_metrics


RANKERS: dict[str, Callable[[Case, "pd.DataFrame"], list[str]]] = {"baro": rank_baro, "ours": rank_ours}


# --- scoring -------------------------------------------------------------------------


def _same(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or a.removesuffix("service") == b.removesuffix("service")


def hit_at(ranks: list[str], truth: str, k: int) -> bool:
    return any(_same(s, truth) for s in ranks[:k])


def score(results: list[Result]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rankers = sorted({r for res in results for r in res.ranks})
    groups: dict[str, list[Result]] = defaultdict(list)
    for res in results:
        groups[res.case.suite].append(res)
        groups[f"{res.case.suite} {res.case.fault}"].append(res)
        groups["all"].append(res)
    for name, group in sorted(groups.items()):
        row = {"cases": len(group)}
        for r in rankers:
            ac = [sum(hit_at(g.ranks[r], g.case.service, k) for g in group) / len(group) for k in range(1, MAX_K + 1)]
            row[r] = {"AC@1": round(ac[0], 3), "AC@3": round(ac[2], 3), "Avg@5": round(sum(ac) / MAX_K, 3),
                      "seconds_per_case": round(sum(g.seconds.get(r, 0) for g in group) / len(group), 3)}
        out[name] = row
    return out


def run(cases: list[Case], rankers: list[str], progress: bool = True) -> list[Result]:
    results = []
    for i, case in enumerate(cases, 1):
        try:
            metrics = read_metrics(case)
        except Exception as e:  # a broken download should not stop the whole run
            print(f"skipped {case.name}: {e}", file=sys.stderr)
            continue
        res = Result(case=case, ranks={})
        for r in rankers:
            t = time.perf_counter()
            res.ranks[r] = RANKERS[r](case, metrics)
            res.seconds[r] = time.perf_counter() - t
        results.append(res)
        if progress and (i % 25 == 0 or i == len(cases)):
            print(f"  {i}/{len(cases)} cases", file=sys.stderr)
    return results


def format_scores(s: dict[str, Any], rankers: list[str]) -> str:
    head = f"{'cases':<28}{'n':>5}" + "".join(f"{r + ' AC@1':>12}{r + ' Avg@5':>13}" for r in rankers)
    lines = [head]
    for name, row in s.items():
        if " " in name:
            continue  # per-fault rows go in the JSON
        lines.append(f"{name:<28}{row['cases']:>5}" + "".join(
            f"{row[r]['AC@1']:>12.2f}{row[r]['Avg@5']:>13.2f}" for r in rankers))
    return "\n".join(lines)


def download(suite: str, data: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit('Downloading needs huggingface_hub: pip install -e ".[bench]"') from None
    print(f"Downloading {suite.upper()} from Hugging Face ({HF_REPO}) to {data} ...", file=sys.stderr)
    snapshot_download(repo_id=HF_REPO, repo_type="dataset", allow_patterns=[f"{suite.lower()}*", "cases.parquet"],
                      local_dir=str(data))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("data/rcaeval"), help="folder with the cases")
    p.add_argument("--download", choices=["re1", "re2", "re3"], action="append",
                   help="download this suite first (repeat for several)")
    p.add_argument("--suite", help="only cases whose suite starts with this, e.g. RE2 or RE2-OB")
    p.add_argument("--rankers", default="baro,ours")
    p.add_argument("--limit", type=int, help="only the first N cases (for a quick check)")
    p.add_argument("--json", type=Path, help="also write all scores (with per-fault rows) to this file")
    args = p.parse_args(argv)
    _need_pandas()
    for suite in args.download or []:
        download(suite, args.data)
    cases = find_cases(args.data)
    if args.suite:
        cases = [c for c in cases if c.suite.startswith(args.suite.upper())]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"No cases found under {args.data}. Use --download re1 (or re2, re3) first.", file=sys.stderr)
        return 1
    rankers = [r.strip() for r in args.rankers.split(",") if r.strip()]
    print(f"Scoring {len(cases)} cases with {', '.join(rankers)}", file=sys.stderr)
    results = run(cases, rankers)
    s = score(results)
    print(format_scores(s, rankers))
    if args.json:
        args.json.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
