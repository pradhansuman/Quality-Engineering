# V11.0 Multi-Agent Autonomous QE Organization

V11 coordinates 20 specialist QA roles over the V10.1 planning layer and V9.2.10 canonical evidence engine.

## Run

```sh
cd /Users/skp/web_agent
python3 -u qa11.py "https://your-application.example/" \
  --headless \
  --max-pages 10 \
  --max-seconds 300 \
  --max-actions 80
```

For explicitly authorized form workflows and mutation checks:

```sh
python3 -u qa11.py "https://your-application.example/" \
  --headless \
  --requirements /absolute/path/requirements.json \
  --requirements-only \
  --allow-form-submission \
  --mutation-testing
```

## Organization

The QE Orchestrator coordinates requirement, risk, architecture, UI, API, integration, data, performance, security, accessibility, localization, mutation, regression, flaky, healing, investigation, root-cause, coverage, knowledge, and release roles.

Every role consumes the same canonical run evidence. Missing requirements, authorization, telemetry, baselines, environments, or proof remain `UNKNOWN`. The release role can only return `RELEASE`, `BLOCK`, or `INSUFFICIENT EVIDENCE` from the fail-closed evidence gate.

V11 defaults to a 15-second navigation timeout and 10-second DOM-readiness timeout. Use `--nav-timeout`, `--dom-timeout`, `--workflow-timeout`, `--journey-timeout`, `--max-recovery-attempts`, `--max-replays`, and `--max-replay-actions` for slower environments. Use `--max-audit-pages` to bound responsive audits. Page audits persist severity-ranked `HIGH`, `MEDIUM`, and `LOW` findings instead of flattening every observation into one undifferentiated risk.

## Outputs

Each run includes all V9 and V10 artifacts plus:

- `qa_v11_organization.json`
- `qa_v11_qa_director.md`

V11 does not claim continuous production ownership without persistent CI/CD, telemetry, authorized security, load infrastructure, and production integrations.
