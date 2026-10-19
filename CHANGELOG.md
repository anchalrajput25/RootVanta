# Changelog

Rootvanta is actively in development. Each milestone below is finished and tested.
Details of every part are in [docs/MILESTONES.md](docs/MILESTONES.md).

## Milestone 11 (in progress)

- Better use of logs and traces on RCAEval, measured on a held-out split
- A small fine-tuned model for ranking causes

## Milestone 10: learning from what it sees

- Alert grouping: alerts from one outage become one incident, using the dependency map (30-minute window, worst severity wins)
- Service map tab, learned from traces, with "Update from traces"
- Incident memory: "Seen before" notes and a warning when the same fix already failed
- Each service's own normal: robust per-service baseline (median and MAD)
- New API: `/incidents/{id}/alerts`, `/incidents/{id}/similar`, `/service-map`, `/service-map/refresh`

## Milestone 9: real change systems and company sign-in

- Argo CD rollback executor, with deploy history as evidence
- Kubernetes scale executor (token or in-cluster service account)
- Routing: each fix goes to the system the company connected
- Company sign-in (OpenID Connect with PKCE): Google Workspace, Microsoft Entra ID, Okta
- Helm chart and Argo CD manifest
- Chat messages link straight to the incident

## Milestone 8: measuring results

- Practice benchmark of 1,000 incidents: 88.9% right cause, 1.8% confidently wrong
- RCAEval runner (735 real cases): 0.49 AC@1 vs 0.50 for BARO
- Investigation time and AI cost tracking
- Load test across many companies: no data leaked in 625 checks

## Milestone 7: self-service setup

- Each company connects its own monitoring, with encrypted tokens and a connection test
- Settings pages for monitoring, alerts, team and notifications
- "Get started" checklist, one-command install, CI

## Milestone 6: web dashboard

- Next.js incident workspace: investigation, evidence, timeline, fix, approval and verdict

## Milestone 5: safe fixing with auto-undo

- Database job queue and workers; a fix is never applied twice
- Practice run, gradual rollout (10% first), verification after each stage, automatic undo
- Alertmanager intake and Slack-style notifications

## Milestone 4: investigator

- Evidence from Prometheus, Loki and Jaeger with reproducible queries
- Ranked causes with confidence and cited evidence; "not enough evidence" below 60%
- Optional Claude re-ranking with schema-checked, evidence-cited answers
- LangGraph pipeline with prompt-injection screening

## Milestone 3: practice environment

- Three-service practice shop with Prometheus, Loki, Jaeger and Grafana
- Failure injector and live fix checker

## Milestone 2: multi-company API

- FastAPI, roles, signed tokens, PostgreSQL row-level security, audit log

## Milestone 1: deterministic core

- Domain models, deny-by-default safety policy, recovery verification, scenarios and the false-success scorecard
