# Autonomous Quality Engineering

V11 coordinates 20 specialized quality roles over the V10 planning layer and V9.2.10 canonical evidence engine. It explores web applications, executes approved workflows, preserves unknowns, and returns `RELEASE`, `BLOCK`, or `INSUFFICIENT EVIDENCE` for the tested scope.

## Repository files

- `qa11.py` — main V11 command and QE orchestrator.
- `qa10.py` — product understanding, strategy, risk, defects, and requirements candidates.
- `qa9210.py` — browser exploration and canonical evidence engine.
- `api/server.py` — containerized execution API and requirements-document ingestion.
- `web/` — Cloudflare Worker interface.
- `requirements.example.json` — approved-scope example.

## Local CLI

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 -u qa11.py "https://example.com/" --headless --max-pages 10 --max-seconds 300
```

## Web application architecture

Cloudflare Workers hosts the public interface. `/api/assess` runs browser exploration and explicit JSON workflow tests directly with Cloudflare Browser Run. The public form does not require an access key or a separate server.

Cloud mode explores safe same-origin links, tabs and disclosures with current-run state/action identities. It reports transitions, skipped controls, remaining frontier, failures, and reconciled coverage. Runs are bounded to 55 seconds of engine execution, 40 operations including replay, 5 exploration pages, 20 exploration states, depth 3 and 6 replays. Browser launch adds time. These are bounded observations, not exhaustive application coverage.

JSON contracts use the format in `requirements.example.json`. Supported steps: `click`, `double_click`, `right_click`, `fill`, `check`, `uncheck`, `select`. Supported assertions: `text_contains`, `text_equals`, `visible`, `value_equals`, `url_equals`, `http_status` (initial navigation response). Each test runs twice with isolated browser storage. Missing/ambiguous locators produce UNKNOWN; a repeated identical assertion mismatch produces BLOCK. RELEASE requires explicit scope approval, confirmed testing permission, complete successful repeated tests and no observed execution problems. It applies only to the stated requirement scope. Workflow interactions require the separate form opt-in.

Download the Markdown report and JSON evidence before leaving the page. Evidence includes states, actions, assertions, repeated execution attempts and up to two failure screenshots; it is not stored on the server. URL-only exploration cannot produce RELEASE. TXT/Markdown briefs are context only and are not automatically converted into verified assertions. Cloud mode does not execute API/backend consistency, mutation, historical regression, security, or performance testing.

When `QA_API_BASE_URL` points to a deployed Python runner, the interface automatically uses `/api/jobs` and polls for its full report. Optional `QA_API_TOKEN` is a server-to-server secret and never appears in the form. Full runner availability and deployment are separate from the native Worker.

## Cloudflare Git deployment

In Cloudflare Workers & Pages, import this GitHub repository and use:

- Root directory: `web`
- Build command: `npm run check`
- Deploy command: `npx wrangler deploy`

No access key, secret, or environment variable is required for the built-in Cloudflare assessment.

Cloud runs are headless. The headed option appears only when a Python runner is configured; that runner must have a desktop display.

Native cloud mode does not depend on a Mac or tunnel. If using the optional local Python runner, keep its computer, backend process and tunnel running.

Start the runner locally:

```bash
docker build -t autonomous-qe .
docker run --rm -p 8000:8000 -e QA_API_TOKEN=replace-me autonomous-qe
```

Deploy the Cloudflare interface manually if desired:

```bash
cd web
npm install
npm run deploy
```

Set `QA_API_BASE_URL` only when using the optional Python runner. No runner variable is needed for native mode.

## Requirement documents

Native cloud mode accepts JSON, TXT and Markdown up to 1 MB. The full Python runner also accepts PDF, DOC and DOCX. Unsupported uploads and malformed contracts are rejected explicitly. An approved JSON contract containing `scope`, `scope_approved: true`, requirements, tests, and assertions enables scoped release gating; uploading prose alone does not authorize release.

## Truth boundary

A URL-only run cannot prove undeclared business requirements. Missing requirements, authorization, baselines, APIs, backend access, security scope, performance infrastructure, or production telemetry remain `UNKNOWN`. A `RELEASE` decision applies only to the explicitly approved and successfully verified scope.

## Public deployment safety

Read `SECURITY.md` before internet deployment. Add authentication, rate limiting, quotas, isolated browser networking, persistent job storage, and retention controls. Never operate this service as an unrestricted public URL scanner.
