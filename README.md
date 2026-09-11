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

Cloudflare Workers hosts the public interface. `/api/assess` runs a bounded audit directly with Cloudflare Browser Run, so the public form does not need an access key or a separate server.

Free-plan mode performs a bounded headless page audit and returns a downloadable QA report. It deliberately returns `INSUFFICIENT EVIDENCE` unless a directly observed server failure justifies `BLOCK`; it does not claim the full V11 Python scope. Other `/api/*` routes can optionally proxy to the full Python runner when `QA_API_BASE_URL` is configured.

## Cloudflare Git deployment

In Cloudflare Workers & Pages, import this GitHub repository and use:

- Root directory: `web`
- Build command: `npm run check`
- Deploy command: `npx wrangler deploy`

No access key, secret, or environment variable is required for the built-in Cloudflare assessment.

The web form defaults to headless execution and provides a headed option when the runner is attached to a desktop computer. Cloud-hosted browser sessions do not expose a visible headed window.

The Worker remains deployed, but the free Python runner operates through your Mac. Keep the Mac, backend process, and tunnel process running. Re-run the command after a restart because Quick Tunnel URLs are temporary.

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

Update `web/wrangler.jsonc` with the HTTPS address of the runner API before deployment.

## Requirement documents

The web interface accepts TXT, Markdown, JSON, PDF, DOC, and DOCX files. An approved JSON contract containing `scope`, `scope_approved: true`, requirements, tests, and assertions activates requirements-only release gating. Other documents are treated as product briefs: they guide planning but cannot authorize release automatically.

## Truth boundary

A URL-only run cannot prove undeclared business requirements. Missing requirements, authorization, baselines, APIs, backend access, security scope, performance infrastructure, or production telemetry remain `UNKNOWN`. A `RELEASE` decision applies only to the explicitly approved and successfully verified scope.

## Public deployment safety

Read `SECURITY.md` before internet deployment. Add authentication, rate limiting, quotas, isolated browser networking, persistent job storage, and retention controls. Never operate this service as an unrestricted public URL scanner.
