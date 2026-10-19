# Rootvanta: milestone details

An AI incident investigation and remediation agent for SRE and on-call teams. It finds out why production broke, proposes a safe fix, and **proves whether the fix actually worked**: it checks real metrics against explicit targets, rolls back when the fix fails, and never reports "recovered" without evidence.

Designed from day one for many users and many companies (multi-tenant): every record belongs to an organization and team, and roles decide who can request and approve actions.

## Status

Milestones 1 to 10 are done. RCAEval results are in `evals/RESULTS.md`.

**Milestone 1, deterministic core**

| Part | Where | What it does |
|---|---|---|
| Domain models | `src/incident_agent/domain/` | Organizations, teams, users with roles, incidents, evidence, hypotheses, typed recovery actions, approvals and audit events |
| Safety policy | `src/incident_agent/safety/` | Deny-by-default rules in code. Checks the tenant and team match and the requester's role, then the action allowlist, reversibility, blast radius and scale limits. Approval must come from a different user in the same organization who holds the approver role |
| Recovery verification | `src/incident_agent/verification/` | Decides VERIFIED, PARTIALLY VERIFIED, FAILED or UNVERIFIED from telemetry and regression tests only. Missing data is never a success |
| Practice incidents | `evals/scenarios/` | 3 incidents and 11 cases with known ground truth: bad deploy, dependency outage, CPU saturation |
| Scorecard | `src/incident_agent/evals/runner.py` | Measures the false-success rate and compares it with a naive "the action ran, so it's fixed" baseline |

**Milestone 2, multi-company web API**

| Part | Where | What it does |
|---|---|---|
| Web API | `src/incident_agent/api/` | FastAPI under `/api/v1`: sign up a company, log in, manage teams and users, open incidents, propose fixes, approve them, record verification results, read the audit log |
| Login and roles | `api/security.py`, `api/deps.py` | Passwords hashed with scrypt, signed access tokens, roles admin / approver / responder / viewer. A deactivated user loses access at once |
| Company separation | `src/incident_agent/db/rls.py` | PostgreSQL row-level security: the database itself hides other companies' rows, even if a query forgets to filter. The app connects as a non-superuser so this can't be bypassed |
| Audit log | `api/audit.py` | Every sign-up, login, incident, plan, approval and verification is recorded in the same transaction as the change |

**Milestone 3, practice environment** (`demo/`)

| Part | Where | What it does |
|---|---|---|
| Sample shop | `demo/shop/` | Three services from one image: checkout, payments, search, plus a traffic generator. Each exports metrics, JSON logs and traces |
| Monitoring | `demo/docker-compose.yml` | Prometheus, Loki (logs collected by Alloy), Jaeger and Grafana with a ready "Shop overview" dashboard |
| Failure injector | `demo/inject.py` | Breaks the shop on purpose (bad deploy, payment outage, traffic spike) and applies the fixes (rollback, backup provider flag, scale) |
| Live fix checker | `src/incident_agent/telemetry/` | Reads real metrics from Prometheus after a fix and runs the same verifier as the scorecard |

**Milestone 4, investigator** (`src/incident_agent/investigation/`)

| Part | Where | What it does |
|---|---|---|
| Evidence collection | `collectors.py`, `telemetry/loki.py`, `telemetry/jaeger.py` | Finds which services the alerting one calls (traces), then reads error rate, latency and CPU for all of them, groups error logs by type, lists recent deploys, flag changes and scaling, and finds where failing requests start. Every item gets an id (E1, E2…) and the query that reproduces it |
| Reasoning | `rules.py` | Builds the timeline and ranks causes (bad deploy, failing dependency, overload) with a confidence score and the evidence for and against each. Below 60% it says "not enough evidence" |
| Safe fix | `rules.py`, `demo/runbook.yaml` | Picks the fix in code from the allowed list: roll back to the previous version, turn on a fallback the team wrote in the runbook, or add replicas. Runs it through the safety policy. It never changes anything by itself |
| Optional AI step | `llm.py` | With `--ai`, Claude re-ranks the causes and writes the summary. Log text is sent as data, cleaned and shortened; answers must match a JSON schema and cite real evidence ids, or they are dropped. If the API fails, the rule engine's answer is used |
| Pipeline | `graph.py` | LangGraph: collect, screen for instructions hidden in logs, timeline, reason, propose, finish |
| Report | `report.py`, `__main__.py` | Markdown for people, JSON for tools |

**Milestone 5, safe fixing with auto-undo** (`src/incident_agent/remediation/`, `src/incident_agent/workflow/`)

| Part | Where | What it does |
|---|---|---|
| Practice run | `remediation/executor.py` | Reads the live system and shows exactly what a fix will change and how it will be undone, without changing anything |
| Run the fix | `remediation/executor.py` | Applies rollbacks, feature flags and scaling to the practice shop, then reads the state back to confirm |
| Gradual rollout | `remediation/runner.py` | A flag goes to 10% of requests first. That stage must cause no new errors and no higher error rate before it goes to everyone |
| Check and auto-undo | `remediation/runner.py` | After each stage it waits, then runs the verifier on real metrics. FAILED means the change is undone automatically. Not enough data means it is not widened |
| Background workers | `workflow/queue.py`, `workflow/worker.py` | Investigations and fixes run as jobs in the database. Start as many workers as you like; each job is claimed by exactly one, and a fix is never applied twice, even if a worker dies |
| Agent account | `workflow/plans.py` | The investigator's proposed fix is requested by the organization's agent account, so a person must approve it |
| Alerts open incidents | `api/routes_workflow.py`, `demo/alertmanager/` | Alertmanager posts firing alerts; each opens an incident and starts an investigation. Repeat alerts for the same service join the open incident |
| Notifications | `workflow/notify.py` | Slack-style webhooks: a fix waiting for approval, verified, undone, or needing a person |

**Milestone 6, web dashboard** (`web/`): a Next.js app with the incident list, the investigation (cause, confidence, evidence, timeline), the fix with practice run, approve and run buttons, each stage's checks, and a scorecard of fixes proven and undone. It refreshes every 4 seconds.

**Milestone 7, any company can set it up itself**

| Part | Where | What it does |
|---|---|---|
| Own monitoring per company | `src/incident_agent/integrations/` | Each company saves its own Prometheus, Loki, Jaeger and change-system addresses in Settings. Workers use only that company's connections; a company that has not connected Prometheus gets a clear message, never another company's data |
| Encrypted tokens | `integrations/crypto.py` | Monitoring tokens are encrypted in the database with `INCIDENT_AGENT_SECRET_KEY` and never sent back to the browser or written to the audit log |
| Connection test | `integrations/probe.py` | "Save and test" makes one small read and explains the result: connected, wrong token, cannot reach it, or not the right kind of server |
| Safe addresses | `integrations/store.py` | Cloud metadata addresses are always refused. With `INCIDENT_AGENT_BLOCK_PRIVATE_URLS=1`, a server shared by many companies also refuses private network addresses |
| Settings pages | `web/components/settings/` | Monitoring, Alerts (create a token, copy the Alertmanager snippet, revoke), Team (add people with a temporary password, change roles, remove and restore access), Notifications (add a webhook, send a test) |
| Getting started | `web/components/GettingStarted.tsx` | A checklist for a new company's admin until the basics are connected |
| One-command install | `install.sh`, `install.ps1`, `docker-compose.yml` | Creates `.env` with new random secrets, then starts PostgreSQL, the API, workers and the dashboard |
| Automatic checks | `infra/github-ci.yml` (put it at `.github/workflows/ci.yml` in your GitHub repository) | On every push: all tests on SQLite and PostgreSQL, the dashboard's type check and build, and both Docker images |

**Milestone 8, measuring results** (full numbers in [`evals/RESULTS.md`](../evals/RESULTS.md))

| Part | Where | What it does |
|---|---|---|
| Practice benchmark | `src/incident_agent/evals/practice.py` | 1,000 generated incidents with known causes and distractions. Measures right cause, "not enough evidence", confidently wrong, right fix, and time. Now: 89% right and 1.8% confidently wrong, up from 53% and 17% |
| Public benchmark | `src/incident_agent/evals/rcaeval.py`, `run_benchmarks.bat` | Downloads RCAEval (735 real failure cases) and scores our ranking against the published BARO method (AC@1, Avg@5). All 735 cases: 0.49 vs 0.50 first place, level but not better |
| Speed and AI cost | `investigation/llm.py`, `investigation_metrics` table | Every investigation records its time and, with the AI step, its tokens and cost. The dashboard shows typical time and this month's AI cost |
| Load test | `src/incident_agent/loadtest.py`, `evals/fake_monitoring.py` | Many companies and people at once against a running install, with real investigations. Reports response times, errors, alert-to-answer time, and checks that no company sees another's data |

Run them:

```bash
python -m incident_agent.evals.practice --cases 1000 --seed 2026
pip install -e ".[bench]" && python -m incident_agent.evals.rcaeval --download re1 --data data/rcaeval
python -m incident_agent.loadtest --api http://localhost:8000 --companies 20 --people 10 --seconds 60
```

Current scorecard:

```
false-success rate  agent: 0% (0)  baseline: 100% (3)
unsafe actions executed: 0
expectation mismatches: 0
```

**Milestone 9, real change systems and company sign-in**

| Part | Where | What it does |
|---|---|---|
| Argo CD rollback | `remediation/argocd.py` | Rolls an application back to an earlier revision from its history, waits until the sync finishes, and undoes it the same way if the check fails. Refuses while auto-sync is on (Argo CD would put it straight back). Argo CD deploy history also becomes evidence in investigations |
| Kubernetes scaling | `remediation/kubernetes.py` | Changes a Deployment's replica count through the `scale` API, and puts it back on undo. Works with a token or, inside the cluster, with its own service account |
| One fix, the right system | `remediation/routing.py` | Rollbacks go to Argo CD, scaling to Kubernetes, the rest to the custom change system, whichever the company connected |
| Company sign-in (SSO) | `auth/oidc.py`, `api/routes_sso.py`, Settings > Sign-in | Google Workspace, Microsoft Entra ID, Okta or any OpenID Connect provider. PKCE, nonce and signature checks; first sign-in makes the account with a chosen role; "require it" leaves passwords only for admins |
| Helm chart | `infra/helm/incident-agent/` | API, workers, dashboard, optional PostgreSQL, Ingress, autoscaling, disruption budgets, and scale-only permissions in the namespaces you list |
| Argo CD install | `infra/argocd/incident-agent.yaml` | Deploys the chart with Argo CD |
| Chat links | `workflow/notify.py` | Every Slack-style message links straight to the incident |

**Milestone 10, learning from what it sees**

| Part | Where | What it does |
|---|---|---|
| One outage, one incident | `api/routes_workflow.py` (`_group_into`) | An alert joins an open incident when it is the same service, or a service next to it on the dependency map whose incident opened in the last 30 minutes. The incident shows every alert and why each one is there, and the worst alert's severity wins |
| Dependency map | `knowledge/service_map.py`, Service map tab | Who calls whom, learned from traces. Every investigation adds what it sees, and "Update from traces" reads the last hour for every service. Also used when the incident window itself has no traces |
| Incident memory | `knowledge/memory.py`, "Seen before" | When a cause was seen before, the report and the chat message say what was done then and whether it worked, and warn when the same fix already failed once. Memory only informs people: it never changes a cause's confidence or the fix |
| Its own normal | `investigation/collectors.py` (`Normal`) | A rise is also abnormal when it is far outside that service's own usual spread, so a steady service is not missed just because it stays under the fixed limits, and a noisy one does not cry wolf |

## Install it for your company

Needs [Docker](https://docs.docker.com/get-docker/). One command creates `.env` with new random secrets and starts PostgreSQL, the API, two workers and the dashboard:

```bash
./install.sh          # Windows PowerShell: .\install.ps1
```

Then open http://localhost:3001, create your company, and follow the "Get started" checklist:

1. **Settings > Monitoring**: enter your Prometheus address (and Loki and Jaeger if you have them), then "Save and test". Monitoring on the same computer is reached at `http://host.docker.internal:<port>`.
2. **Settings > Alerts**: create a token and paste the snippet into `alertmanager.yml` (or a Grafana webhook contact point). Each alert needs a `service` label.
3. **Settings > Team**: add the people who approve and run fixes.
4. **Settings > Notifications**: add a Slack-style webhook.

Keep `.env` private and back it up: without `INCIDENT_AGENT_SECRET_KEY` the saved tokens cannot be read. Set `INCIDENT_AGENT_BLOCK_PRIVATE_URLS=1` in it when several companies share one server, and `WORKERS=4` for more incidents at once.

5. **Settings > Monitoring**: connect Argo CD (an API token for an account allowed to get and sync the applications) and Kubernetes (the API address and a token allowed to patch `deployments/scale`), and list which application or `namespace/deployment` each service is. A custom admin API like the practice shop's (`/admin/state`, `X-Admin-Token`) also works.
6. **Settings > Sign-in** (optional): company sign-in. Register the redirect address shown there with your provider. Set `INCIDENT_AGENT_PUBLIC_URL` in `.env` to the address people open, so the redirect and chat links are right.

### On Kubernetes

Build and push the two images (`docker build -t <registry>/incident-agent-api .` and `docker build -t <registry>/incident-agent-web web`), then:

```bash
helm install incident-agent infra/helm/incident-agent -n incident-agent --create-namespace \
  --set image.api=<registry>/incident-agent-api --set image.web=<registry>/incident-agent-web \
  --set publicUrl=https://incidents.example.com --set ingress.host=incidents.example.com \
  --set scaling.namespaces='{shop,payments}'
```

Or edit `infra/argocd/incident-agent.yaml` and `kubectl apply` it to let Argo CD deploy it. With `scaling.namespaces` set, connect Kubernetes in Settings with the address `https://kubernetes.default.svc` and no token: it uses its own service account, which may only scale Deployments in those namespaces. This is for an install used by one company: any admin who connects that address gets those permissions, so a shared install sets `blockPrivateUrls=true`, which refuses it, and each company connects its own cluster with its own token. For production use your own PostgreSQL (`postgres.enabled=false`, `externalDatabase.url`) with a role that is not a superuser, so row-level security applies.

## Develop and test

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

pytest                                  # all tests on SQLite (more run when PostgreSQL is available, see below)
python -m incident_agent.evals.runner   # practice-incident scorecard
```

### Start the API

Quick local run with a SQLite file (no Docker needed):

```bash
uvicorn incident_agent.api.app:create_app --factory --reload
```

Then open http://localhost:8000/docs to try every endpoint in the browser.

A company that has not connected its own monitoring in Settings gets a clear message. For a single-company install configured with environment variables instead, set `INCIDENT_AGENT_SHARED_CONNECTIONS=1` (see `.env.example`).

### Run the tests on PostgreSQL too

This also runs the API tests on PostgreSQL and the tests that prove the database blocks cross-company access by itself. It needs a PostgreSQL database owned by a role that is **not** a superuser (`infra/postgres/init.sql` creates one; `infra/github-ci.yml` shows the steps):

```bash
# macOS/Linux
TEST_DATABASE_URL=postgresql+psycopg://incident_app:incident_app@localhost:5432/incident_test pytest
# Windows PowerShell
$env:TEST_DATABASE_URL="postgresql+psycopg://incident_app:incident_app@localhost:5432/incident_test"; pytest
```

### Practice incidents (needs Docker Desktop)

```bash
cd demo
docker compose up --build -d     # first time takes a few minutes
```

Open Grafana at http://localhost:3000 (dashboard "Shop overview"), Jaeger at http://localhost:16686, Prometheus at http://localhost:9090.

Play an incident and check the fix. Run these from the `demo` folder:

```bash
python inject.py bad-deploy          # about 30% of orders start failing
python inject.py rollback            # the fix
python -m incident_agent.telemetry.check bad_deploy_5xx --window 120 --wait
# about 3 minutes later: status VERIFIED

python inject.py bad-deploy
python inject.py rollback --to v2.3.0   # rolls back too far and breaks coupons
python -m incident_agent.telemetry.check bad_deploy_5xx --window 120 --wait
# status FAILED: a new error appeared, recommendation: roll back this fix
```

The other incidents are `payments-outage` (fixed by `backup-provider`) and `traffic-spike` (fixed by `scale --replicas 8`). `python inject.py reset` puts everything back. `docker compose down` stops it all.

### Let the investigator find the cause

Break something, wait about 3 minutes, then ask the investigator:

```bash
python inject.py payments-outage
python -m incident_agent.investigation checkout --minutes 5
# Cause found: payments is failing, and checkout fails when it calls payments
# Proposed fix: switch checkout to the backup payment provider, starting with 10% of traffic
```

Add `--json report.json` to save the full result. Add `--ai` to also let Claude rank the causes (needs `pip install -e ".[ai]"` and `ANTHROPIC_API_KEY`).

### The whole loop: alert, investigation, approval, fix, proof

Use three terminals, all in the `incident-agent` folder (the practice shop must be running):

```bash
# terminal 1: the API
uvicorn incident_agent.api.app:app

# terminal 2: one-time setup, then a worker with short practice-shop checks
python -m incident_agent.workflow.demo setup
python -m incident_agent.workflow --demo

# terminal 3: wait about 6 minutes after starting the shop, then break it and follow along
cd demo
python inject.py payments-outage
cd ..
python -m incident_agent.workflow.demo watch
```

About a minute later Alertmanager opens an incident, the worker investigates, and `watch` shows the report and the practice run and asks you to approve. After you say `y`, the fix goes to 10% of requests, is checked, then goes to everyone and is checked again, and you see the verdict. If a check fails, the fix is undone by itself.

### The web dashboard

The dashboard is a Next.js app in `web/`. It needs [Node.js](https://nodejs.org) 20 or newer (the LTS installer is fine). With the API running:

```bash
cd web
npm install      # first time only
npm run dev      # then open http://localhost:3001
```

Log in as `lead@demo-shop.test` (can approve) or `oncall@demo-shop.test`, password `demo-password-123`, after running `python -m incident_agent.workflow.demo setup` once. The page refreshes every 4 seconds and shows:

- the incident list, and counts of open incidents, fixes proven, and fixes undone automatically
- the investigation: cause, confidence, evidence and timeline
- the proposed fix with a practice run, Approve / Reject, and Run fix
- each stage of the fix and the checks on real metrics after it

The browser only talks to the dashboard; it passes `/api/v1` requests on to the API at `INCIDENT_AGENT_API_URL` (default `http://127.0.0.1:8000`). `run_demo.bat` starts it for you when Node.js is installed.

## Roadmap

1. ~~Deterministic core: models, safety policy, verification, scenarios, scorecard~~
2. ~~API and multi-tenant storage: FastAPI, PostgreSQL row-level security per organization, login and roles, audit log~~ 
3. ~~Demo environment: sample shop with Prometheus, Loki, Jaeger and Grafana in Docker Compose; failure injector; live fix checker~~
4. ~~Investigation agent: collects evidence, ranks cited causes, says "not enough evidence", timeline, report, safe fix proposal~~
5. ~~Execution: background workers, practice run, approval, executor, gradual rollout, verification with automatic undo, alert intake, notifications~~
6. ~~Dashboard: Next.js incident workspace with timeline, evidence, approval and verdict~~
7. ~~Self-service setup: each company's own monitoring with encrypted tokens and a connection test, settings pages, one-command install, CI~~
8. ~~Evaluation at scale: practice benchmark, RCAEval runner, speed and AI cost, load test~~ 
9. ~~Change-system connectors (Kubernetes, Argo CD), SSO, Helm chart, chat links~~
10. ~~Alert grouping, dependency map, incident memory, each service's own normal~~
11. Next: a small fine-tuned ranking model, and better use of logs and traces on RCAEval (see `evals/RESULTS.md`)
