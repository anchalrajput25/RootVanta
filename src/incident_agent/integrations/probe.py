"""'Test connection': one small read from each system, explained in plain words."""

from __future__ import annotations

import httpx

from incident_agent.integrations.store import Integration

NAMES = {"prometheus": "Prometheus", "loki": "Loki", "jaeger": "Jaeger", "service_admin": "service admin API",
         "argocd": "Argo CD", "kubernetes": "Kubernetes API"}


class Refused(Exception):
    pass


def _get(client: httpx.Client, url: str, **params: str) -> httpx.Response:
    r = client.get(url, params=params or None)
    if r.status_code in (401, 403):
        raise Refused(f"the server refused the sign-in details ({r.status_code})")
    r.raise_for_status()
    return r


def _explain(e: Exception, kind: str) -> str:
    if isinstance(e, Refused):
        return f"Reached it, but {e}."
    if isinstance(e, httpx.ConnectError):
        return "Could not reach it. Check the address, and that this server can reach it."
    if isinstance(e, httpx.TimeoutException):
        return "It did not answer within 10 seconds."
    return f"It answered, but not like a {NAMES[kind]}: {str(e)[:200]}"


def test(i: Integration) -> tuple[bool, str]:
    """Returns (worked, message for a person)."""
    with i.http() as client:
        if i.kind == "service_admin":
            return _test_services(client, i)
        if i.kind in ("argocd", "kubernetes"):
            return _test_targets(client, i)
        try:
            return True, _probe(client, i)
        except (Refused, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
            return False, _explain(e, i.kind)


def _probe(client: httpx.Client, i: Integration) -> str:
    base = i.url.rstrip("/")
    if i.kind == "prometheus":
        data = _get(client, f"{base}/api/v1/query", query="up").json()
        if data.get("status") != "success":
            raise ValueError(data.get("error", "the query failed"))
        n = len(data["data"]["result"])
        return f"Connected. {n} monitored target{'' if n == 1 else 's'} found."
    if i.kind == "loki":
        labels = _get(client, f"{base}/loki/api/v1/labels").json().get("data") or []
        return f"Connected. {len(labels)} log label{'' if len(labels) == 1 else 's'} found."
    services = _get(client, f"{base}/api/services").json().get("data") or []
    return f"Connected. Traces found for {len(services)} service{'' if len(services) == 1 else 's'}."


def _test_services(client: httpx.Client, i: Integration) -> tuple[bool, str]:
    """Every service the agent may change must answer."""
    problems = []
    for service, url in sorted(i.endpoints.items()):
        try:
            _get(client, f"{url.rstrip('/')}/admin/state").json()
        except (Refused, httpx.HTTPError, ValueError) as e:
            problems.append(f"{service}: {_explain(e, i.kind)}")
    if problems:
        return False, " ".join(problems)[:500]
    return True, f"Connected to {', '.join(sorted(i.endpoints))}."


def _test_targets(client: httpx.Client, i: Integration) -> tuple[bool, str]:
    """Argo CD: every listed application must be readable. Kubernetes: every listed Deployment's scale."""
    base = i.url.rstrip("/")
    try:
        if i.kind == "argocd" and not i.endpoints:
            items = _get(client, f"{base}/api/v1/applications", fields="items.metadata.name").json().get("items") or []
            return True, f"Connected. {len(items)} application{'' if len(items) == 1 else 's'} visible."
    except (Refused, httpx.HTTPError, ValueError) as e:
        return False, _explain(e, i.kind)
    problems, notes = [], []
    for service, target in sorted(i.endpoints.items()):
        try:
            if i.kind == "argocd":
                app = _get(client, f"{base}/api/v1/applications/{target or service}").json()
                if ((app.get("spec") or {}).get("syncPolicy") or {}).get("automated"):
                    notes.append(f"{service}: auto-sync is on, so rollbacks will be refused")
            else:
                namespace, _, name = target.partition("/")
                _get(client, f"{base}/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale").json()
        except (Refused, httpx.HTTPError, ValueError) as e:
            problems.append(f"{service}: {_explain(e, i.kind)}")
    if problems:
        return False, " ".join(problems)[:500]
    message = f"Connected to {', '.join(sorted(i.endpoints))}."
    return True, (message + " Note: " + "; ".join(notes) + ".")[:500] if notes else message
