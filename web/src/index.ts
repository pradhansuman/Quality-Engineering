import { launch, type BrowserWorker } from "@cloudflare/playwright";

interface Env {
  BROWSER: BrowserWorker;
  QA_API_BASE_URL?: string;
}

const page = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonomous Quality Engineering</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui;background:#07111f;color:#e8f0ff}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 10% 0,#17335c 0,transparent 35%),#07111f}.shell{max-width:920px;margin:auto;padding:64px 24px}.eyebrow{color:#69e6c1;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{font-size:clamp(2.5rem,7vw,5rem);line-height:.95;margin:.35em 0}p{color:#aabbd5;line-height:1.7}.card{margin-top:36px;padding:28px;border:1px solid #294464;border-radius:22px;background:#0b192bbf;box-shadow:0 30px 90px #0008}label{display:block;font-weight:750;margin:18px 0 8px}input[type=url],input[type=file]{width:100%;padding:14px;border-radius:12px;border:1px solid #35516f;background:#071321;color:#fff}button{margin-top:24px;padding:14px 22px;border:0;border-radius:12px;background:#69e6c1;color:#05140f;font-weight:850;cursor:pointer}button:disabled{opacity:.5}.options{display:flex;gap:24px;flex-wrap:wrap;margin-top:18px}.options label{margin:0;font-weight:600}.status{display:none;margin-top:24px;padding:18px;border-radius:14px;background:#071321;white-space:pre-wrap}.decision{font-size:1.5rem;font-weight:900}.RELEASE{color:#69e6c1}.BLOCK{color:#ff718b}.INSUFFICIENT{color:#ffd166}small{color:#7890ad}</style></head>
<body><main class="shell"><div class="eyebrow">V11 Quality Organization</div><h1>Test what matters.<br>Prove what happened.</h1><p>Enter a public application URL. Optionally attach approved requirements or a product brief. The system preserves unknowns and returns RELEASE, BLOCK, or INSUFFICIENT EVIDENCE.</p>
<section class="card"><form id="form"><label for="url">Application URL</label><input id="url" name="target_url" type="url" placeholder="https://example.com/" required>
<label for="requirements">Requirements or product brief</label><input id="requirements" name="requirements" type="file" accept=".txt,.md,.json,.pdf,.doc,.docx"><small>TXT, Markdown, JSON, PDF, DOC, or DOCX. Approved JSON contracts can drive a scoped release gate.</small>
<div class="options"><label><input name="headed" type="checkbox" value="true"> Show browser on runner computer</label><label><input name="allow_form_submission" type="checkbox" value="true"> Allow safe form submission</label></div>
<small>Headless is the default. Headed mode displays Chromium only when the runner is operating on a computer with a desktop; cloud browser services remain headless.</small>
<button id="run" type="submit">Start quality assessment</button></form><div id="status" class="status"></div></section></main>
<script>
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),button=document.querySelector('#run');
const show=t=>{statusBox.style.display='block';statusBox.replaceChildren(document.createTextNode(t))};
function downloadReport(report){const blob=new Blob([report.markdown],{type:'text/markdown'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='qa-report-'+report.run_id+'.md';a.click();URL.revokeObjectURL(url)}
function showReport(report){statusBox.style.display='block';const decision=document.createElement('div'),summary=document.createElement('p'),metrics=document.createElement('pre'),download=document.createElement('button');decision.className='decision '+(report.decision==='INSUFFICIENT EVIDENCE'?'INSUFFICIENT':report.decision);decision.textContent=report.decision;summary.textContent=report.summary;metrics.textContent=JSON.stringify(report.metrics,null,2);download.type='button';download.textContent='Download QA report';download.onclick=()=>downloadReport(report);statusBox.replaceChildren(decision,summary,metrics,download)}
form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;show('Cloudflare browser assessment is running…');try{const body=new FormData(form);if(!body.get('requirements')?.size)body.delete('requirements');const response=await fetch('/api/assess',{method:'POST',body});const report=await response.json();if(!response.ok)throw new Error(report.detail||'Assessment failed');showReport(report)}catch(error){show(error instanceof Error?error.message:'Assessment failed')}finally{button.disabled=false}});
</script></body></html>`;

function publicTarget(value: string): URL {
  if (value.includes("[") || value.includes("](")) throw new Error("Use a plain URL, not Markdown syntax");
  const target = new URL(value);
  if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) throw new Error("A public HTTP or HTTPS URL is required");
  const host = target.hostname.toLowerCase();
  if (host === "localhost" || host.endsWith(".local") || /^(127\.|10\.|192\.168\.|169\.254\.|0\.)/.test(host)) throw new Error("Private and local targets are blocked");
  return target;
}

function escapeMarkdown(value: unknown): string {
  return String(value ?? "").replace(/[\r\n]+/g, " ").replace(/\|/g, "\\|");
}

async function nativeAssessment(request: Request, env: Env): Promise<Response> {
  const form = await request.formData();
  let target: URL;
  try { target = publicTarget(String(form.get("target_url") || "")); }
  catch (error) { return Response.json({ detail: error instanceof Error ? error.message : "Invalid URL" }, { status: 400 }); }
  const upload = form.get("requirements");
  const requirement = upload instanceof File ? { name: upload.name, type: upload.type, bytes: upload.size,
    text: /\.(txt|md|json)$/i.test(upload.name) && upload.size <= 1_000_000 ? (await upload.text()).slice(0, 100_000) : null } : null;
  const runId = crypto.randomUUID();
  const started = Date.now();
  let browser: Awaited<ReturnType<typeof launch>> | undefined;
  try {
    browser = await launch(env.BROWSER);
    const page = await browser.newPage();
    const runtimeErrors: string[] = [];
    page.on("pageerror", error => runtimeErrors.push(String(error).slice(0, 500)));
    const response = await page.goto(target.toString(), { waitUntil: "domcontentloaded", timeout: 20_000 });
    await page.waitForTimeout(800);
    const observation = await page.evaluate(() => {
      const visible = (element: Element) => { const box = element.getBoundingClientRect(), style = getComputedStyle(element); return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none"; };
      const named = (element: Element) => Boolean((element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent || "").trim());
      const ids = Array.from(document.querySelectorAll("[id]")).map(element => element.id);
      return { title: document.title, text_length: (document.body?.innerText || "").trim().length,
        headings: Array.from(document.querySelectorAll("h1,h2,h3")).filter(visible).slice(0, 30).map(element => (element.textContent || "").trim()),
        links: Array.from(document.querySelectorAll("a[href]")).filter(visible).length,
        buttons: Array.from(document.querySelectorAll("button,[role=button]")).filter(visible).length,
        inputs: Array.from(document.querySelectorAll("input:not([type=hidden]),textarea,select")).filter(visible).length,
        forms: document.forms.length,
        images_missing_alt: Array.from(document.images).filter(image => !image.hasAttribute("alt")).length,
        unnamed_buttons: Array.from(document.querySelectorAll("button,[role=button]")).filter(element => visible(element) && !named(element)).length,
        unnamed_inputs: Array.from(document.querySelectorAll("input:not([type=hidden]),textarea,select")).filter(element => visible(element) && !(element as HTMLInputElement).labels?.length && !element.getAttribute("aria-label") && !element.getAttribute("title")).length,
        duplicate_ids: Array.from(new Set(ids.filter((id, index) => ids.indexOf(id) !== index))),
        has_main_landmark: Boolean(document.querySelector("main,[role=main]")),
        horizontal_overflow_px: Math.max(0, document.documentElement.scrollWidth - innerWidth) };
    });
    const findings = [
      ...(response && response.status() >= 400 ? [`Target returned HTTP ${response.status()}`] : []),
      ...runtimeErrors.map(error => `Runtime error: ${error}`),
      ...(observation.unnamed_buttons ? [`${observation.unnamed_buttons} visible buttons lack accessible names`] : []),
      ...(observation.unnamed_inputs ? [`${observation.unnamed_inputs} visible inputs lack accessible names`] : []),
      ...(observation.duplicate_ids.length ? [`Duplicate IDs: ${observation.duplicate_ids.join(", ")}`] : []),
      ...(observation.horizontal_overflow_px > 5 ? [`Horizontal overflow: ${observation.horizontal_overflow_px}px`] : []),
    ];
    const decision = response && response.status() >= 500 ? "BLOCK" : "INSUFFICIENT EVIDENCE";
    const metrics = { http_status: response?.status() ?? null, duration_ms: Date.now() - started, ...observation,
      requirement_file: requirement ? { name: requirement.name, type: requirement.type, bytes: requirement.bytes, extracted: requirement.text !== null } : null,
      execution_mode: "HEADLESS_CLOUDFLARE_BROWSER_RUN", findings: findings.length };
    const markdown = `# Cloudflare QA Report\n\nDecision: **${decision}**\n\nRun: \`${runId}\`\n\nTarget: ${target}\n\n## Observed scope\n\n- HTTP status: ${metrics.http_status}\n- Title: ${escapeMarkdown(observation.title)}\n- Links: ${observation.links}\n- Buttons: ${observation.buttons}\n- Inputs: ${observation.inputs}\n- Forms: ${observation.forms}\n- Requirement document: ${requirement ? escapeMarkdown(requirement.name) : "not supplied"}\n\n## Findings\n\n${findings.length ? findings.map(item => `- ${escapeMarkdown(item)}`).join("\n") : "- No direct runtime or basic accessibility defect was observed."}\n\n## Truth boundary\n\nThis Cloudflare Free assessment is a bounded headless page audit. Undeclared business rules, multi-step workflows, APIs, authentication, backend data, security, performance under load, and unobserved states remain UNKNOWN.\n`;
    return Response.json({ run_id: runId, decision, summary: findings.length ? `${findings.length} risk finding(s) require review.` : "The observed page loaded without a directly proven defect; broader behavior remains unknown.", metrics, findings, markdown });
  } catch (error) {
    return Response.json({ detail: error instanceof Error ? error.message : "Browser assessment failed" }, { status: 500 });
  } finally {
    if (browser) {
      try { await browser.close(); }
      catch {}
    }
  }
}

async function proxy(request: Request, env: Env): Promise<Response> {
  if (!env.QA_API_BASE_URL) return Response.json({ detail: "QA_API_BASE_URL is not configured" }, { status: 503 });
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, env.QA_API_BASE_URL);
  const headers = new Headers(request.headers);
  headers.delete("host");
  return fetch(target, { method: request.method, headers, body: request.method === "GET" ? undefined : request.body, redirect: "manual" });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/api/health" && request.method === "GET") return Response.json({ ok: true, mode: "cloudflare-browser-run" });
    if (url.pathname === "/api/assess" && request.method === "POST") return nativeAssessment(request, env);
    if (url.pathname.startsWith("/api/")) return proxy(request, env);
    return new Response(page, { headers: { "content-type": "text/html;charset=UTF-8", "content-security-policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'", "x-content-type-options": "nosniff" } });
  },
};
