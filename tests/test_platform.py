"""Argo CD rollbacks and Kubernetes scaling: practice run, apply, check, undo, and settings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import auth
from fake_platform import ARGO_TOKEN, BAD, K8S_TOKEN, OLD, Platform, serve
from fakes import Clock, FakeProm
from incident_agent.domain.models import ActionSpec, ActionType, Environment, EvidenceKind, RiskLevel
from incident_agent.investigation.collectors import Collectors, EvidenceBook, _deploy_history
from incident_agent.remediation.argocd import ArgoCDClient, ArgoCDDeploys, ArgoCDExecutor
from incident_agent.remediation.executor import ExecutorError
from incident_agent.remediation.kubernetes import KubernetesExecutor, split_target
from incident_agent.remediation.runner import Runner, Timing


def action(type_: ActionType, service: str = "checkout", **params) -> ActionSpec:
    return ActionSpec(type=type_, service=service, environment=Environment.PRODUCTION, params=params,
                      risk=RiskLevel.MEDIUM, reversible=True, blast_radius_pct=25)


@pytest.fixture
def platform():
    p = Platform()
    server = serve(p)
    p.url = f"http://127.0.0.1:{server.server_port}"
    yield p
    server.shutdown()


def argo(platform: Platform, **kw) -> ArgoCDExecutor:
    http = httpx.Client(headers={"Authorization": f"Bearer {ARGO_TOKEN}"})
    return ArgoCDExecutor(ArgoCDClient(platform.url, http), sleep=lambda s: None, **kw)


def k8s(platform: Platform) -> KubernetesExecutor:
    http = httpx.Client(headers={"Authorization": f"Bearer {K8S_TOKEN}"})
    return KubernetesExecutor(platform.url, http, {"checkout": "shop/checkout"})


# --- Argo CD ----------------------------------------------------------------------------


def test_argocd_rollback_previews_applies_and_undoes(platform):
    ex = argo(platform)
    rollback = action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD[:7])  # a short revision is enough
    preview = ex.preview(rollback)
    assert preview.before == {"version": BAD} and preview.after == {"version": OLD}
    assert preview.changes == ["checkout: Argo CD application checkout bbbbbbb -> aaaaaaa"]
    assert platform.rollbacks == []  # the practice run changes nothing

    assert ex.apply(rollback)["version"] == OLD
    assert platform.rollbacks == [("checkout", 1)]
    assert platform.apps["checkout"]["status"]["sync"]["revision"] == OLD

    ex.apply(preview.undo)  # back to the bad revision: the newest history entry with it
    assert platform.apps["checkout"]["status"]["sync"]["revision"] == BAD
    assert platform.rollbacks[-1] == ("checkout", 2)


def test_argocd_refuses_while_auto_sync_would_undo_it(platform):
    with pytest.raises(ExecutorError, match="Auto-sync is on"):
        argo(platform).preview(action(ActionType.ROLLBACK_DEPLOYMENT, "payments", target_version=OLD))


def test_argocd_unknown_revision_lists_the_known_ones(platform):
    with pytest.raises(ExecutorError, match="recent revisions: aaaaaaa, bbbbbbb"):
        argo(platform).preview(action(ActionType.ROLLBACK_DEPLOYMENT, target_version="v9.9.9"))


def test_argocd_failed_sync_is_reported(platform):
    platform.fail_sync = True
    with pytest.raises(ExecutorError, match="sync of checkout failed"):
        argo(platform).apply(action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD))


def test_argocd_sync_that_never_finishes_times_out(platform):
    ticks = iter(range(0, 10_000, 10))
    platform.apps["checkout"]["status"]["operationState"] = {"phase": "Running"}

    def still_running(name, history_id):  # Argo accepts the rollback but the sync never completes
        platform.rollbacks.append((name, history_id))
        return 200, platform.apps[name]

    platform.rollback = still_running
    ex = argo(platform, timeout_seconds=60, clock=lambda: next(ticks))
    with pytest.raises(ExecutorError, match="did not finish syncing"):
        ex.apply(action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD))


def test_bad_app_names_never_reach_the_api(platform):
    ex = argo(platform, apps={"checkout": "../../settings"})
    with pytest.raises(ExecutorError, match="not a valid Argo CD application name"):
        ex.preview(action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD))


def test_rollback_that_makes_things_worse_is_undone_through_argocd(platform):
    clock = Clock()
    prom = FakeProm(new_error_from=clock.now() + timedelta(seconds=90))
    runner = Runner(argo(platform), prom, Timing(settle_seconds=60, window_seconds=120), now=clock.now,
                    sleep=clock.sleep)
    result = runner.run(action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD), "checkout")
    assert result.status == "rolled_back"
    assert [s.kind for s in result.steps] == ["preview", "applied", "checked", "undone"]
    assert platform.apps["checkout"]["status"]["sync"]["revision"] == BAD  # put back as it was


def test_argocd_history_is_deploy_evidence(platform):
    http = httpx.Client(headers={"Authorization": f"Bearer {ARGO_TOKEN}"})
    deploys = ArgoCDDeploys(ArgoCDClient(platform.url, http), {})  # empty: every application
    book = EvidenceBook()
    now = datetime.now(timezone.utc)
    _deploy_history(Collectors(deploys=deploys), book, now - timedelta(minutes=10), now)
    found = {e.service: e for e in book.items}
    assert set(found) == {"checkout", "payments"}
    e = found["checkout"]
    assert e.kind == EvidenceKind.DEPLOYMENT and e.source == "argocd"
    assert e.facts == {"change_type": "deploy", "from_version": OLD, "to_version": BAD}


def test_unreachable_deploy_history_is_a_note_not_a_crash():
    deploys = ArgoCDDeploys(ArgoCDClient("http://127.0.0.1:9", httpx.Client(timeout=1)), {"checkout": "checkout"})
    book = EvidenceBook()
    now = datetime.now(timezone.utc)
    _deploy_history(Collectors(deploys=deploys), book, now - timedelta(minutes=10), now)
    assert book.items == [] and "Deploy history unreachable" in book.notes[0]


# --- Kubernetes ---------------------------------------------------------------------------


def test_kubernetes_scale_previews_applies_and_undoes(platform):
    ex = k8s(platform)
    scale = action(ActionType.SCALE_SERVICE, replicas=5)
    preview = ex.preview(scale)
    assert preview.before == {"replicas": 2} and platform.scales[("shop", "checkout")] == 2
    assert ex.apply(scale) == {"replicas": 5}
    assert platform.scales[("shop", "checkout")] == 5
    ex.apply(preview.undo)
    assert platform.scales[("shop", "checkout")] == 2


@pytest.mark.parametrize("target", ["shop/../secrets", "Shop/checkout", "shop/", "a/b/c"])
def test_kubernetes_names_are_checked(target):
    with pytest.raises(ExecutorError):
        split_target(target)


def test_a_namespace_defaults_to_default():
    assert split_target("checkout") == ("default", "checkout")


# --- connected through Settings -------------------------------------------------------------


def test_each_change_goes_to_the_system_connected_for_it(client, company, platform):
    org = company("Acme")
    h = auth(org["admin"])
    r = client.put("/integrations/argocd", headers=h, json={
        "url": platform.url, "token": ARGO_TOKEN, "endpoints": {"checkout": "checkout", "payments": "payments"}})
    assert r.status_code == 200, r.text
    r = client.put("/integrations/kubernetes", headers=h, json={
        "url": platform.url, "token": K8S_TOKEN, "endpoints": {"checkout": "shop/checkout"}})
    assert r.status_code == 200, r.text

    tested = client.post("/integrations/argocd/test", headers=h).json()
    assert tested["last_test_ok"] is True
    assert "payments: auto-sync is on" in tested["last_test_message"]
    assert client.post("/integrations/kubernetes/test", headers=h).json()["last_test_ok"] is True

    org_id = client.get("/auth/me", headers=h).json()["org_id"]
    ex = client.app.state.connections.executor(org_id)
    assert ex.preview(action(ActionType.ROLLBACK_DEPLOYMENT, target_version=OLD)).after == {"version": OLD}
    assert ex.preview(action(ActionType.SCALE_SERVICE, replicas=3)).before == {"replicas": 2}
    with pytest.raises(ExecutorError, match="No connected system can do toggle feature flag"):
        ex.preview(action(ActionType.TOGGLE_FEATURE_FLAG, flag="x", value=True))


def test_wrong_tokens_are_explained(client, company, platform):
    org = company("Acme")
    h = auth(org["admin"])
    client.put("/integrations/argocd", headers=h, json={"url": platform.url, "token": "wrong"})
    out = client.post("/integrations/argocd/test", headers=h).json()
    assert out["last_test_ok"] is False and "refused the sign-in details (401)" in out["last_test_message"]


@pytest.mark.parametrize("kind,endpoints,error", [
    ("kubernetes", {}, "namespace/name"),
    ("kubernetes", {"checkout": "shop/../x"}, "not a Deployment"),
    ("argocd", {"checkout": "a/b"}, "not a valid Argo CD application name"),
])
def test_bad_settings_are_refused(client, company, platform, kind, endpoints, error):
    org = company("Acme")
    r = client.put(f"/integrations/{kind}", headers=auth(org["admin"]),
                   json={"url": platform.url, "token": "t", "endpoints": endpoints})
    assert r.status_code == 422 and error in r.json()["detail"]
