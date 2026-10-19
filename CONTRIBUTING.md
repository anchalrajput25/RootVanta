# Contributing to Rootvanta

Thanks for your interest. Rootvanta is actively in development, so issues and pull requests are very welcome.

## Set up

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cd web && npm install
```

## Before you open a pull request

1. Run the tests: `pytest` (and with `TEST_DATABASE_URL` set for the PostgreSQL and row-level security tests).
2. For dashboard changes: `npx tsc --noEmit && npm run build` in `web/`.
3. If you change how causes are found, run `python -m incident_agent.evals.practice --cases 1000 --seed 2026` and include the before and after numbers.

## Rules the project keeps

- Never report a recovery without evidence. Missing data is UNVERIFIED, not success.
- Every new table belongs to a company (`org_id`) and is covered by row-level security.
- New fix types must be reversible and go through the safety policy.
- Benchmark tuning uses a held-out split; publish the weak spots too.

Describe what changed and why in the pull request.
