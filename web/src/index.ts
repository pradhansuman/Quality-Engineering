import { launch, type BrowserWorker } from '@cloudflare/playwright';
import { assess, parseContract, publicTarget, type Contract } from './engine';

interface Env { BROWSER: BrowserWorker; QA_API_BASE_URL?: string; QA_API_TOKEN?: string }

const page = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quality Engineering</title><style>
*{box-sizing:border-box}body{font-family:system-ui;margin:0;background:#07111f;color:#e8f0ff}main{max-width:920px;margin:auto;padding:48px 24px}h1{font-size:3rem}p,small{color:#aabbd5;line-height:1.6}form,#status{background:#112039;border:1px solid #294464;border-radius:16px;padding:24px;margin:24px 0}label{display:block;margin:16px 0}input[type=url],input[type=file]{width:100%;padding:12px;background:#07111f;color:white;border:1px solid #567;border-radius:8px}button{padding:12px 18px;margin:10px 10px 0 0;background:#69e6c1;border:0;border-radius:8px;font-weight:700;cursor:pointer}button:disabled{opacity:.5}pre{white-space:pre-wrap;overflow-wrap:anywhere}#decision{font-size:1.7rem;font-weight:800;color:#ffd166}li{margin:8px 0}
</style></head><body><main><h1>Test. Explore. Show the evidence.</h1>
<p id="mode">Cloud assessment explores safe links, tabs and disclosures. Upload a JSON test contract to check explicit workflows. Every result reports tested scope and remaining unknowns.</p>
<form id="form"><label for="url">Application URL</label><input id="url" name="target_url" type="url" required placeholder="https://example.com/">
<label for="requirements">Requirements (optional)</label><input id="requirements" name="requirements" type="file" accept=".json,.txt,.md">
<small id="formats">JSON contracts execute supported tests. TXT and Markdown are context only, not verified requirements. <a href="/api/example-contract" style="color:#69e6c1">Download a DemoQA contract</a>.</small>
<label><input type="checkbox" name="authorized" value="true"> I have permission to test this application</label>
<label><input type="checkbox" name="allow_form_submission" value="true"> Allow the uploaded contract's workflow interactions and same-origin writes</label>
<label id="headed-option" hidden><input type="checkbox" name="headed" value="true"> Show browser on the runner computer</label>
<small>Cloud runs are headless and bounded to about one minute, 40 operations and 5 exploration pages. A release decision applies only to an approved, verified requirement scope.</small>
<button id="run">Start assessment</button></form><section id="status" hidden aria-live="polite"></section></main>
<script>
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),button=document.querySelector('#run');
let mode='cloud';
function show(text){statusBox.hidden=false;statusBox.textContent=text}
function download(data,type,name){const url=URL.createObjectURL(new Blob([data],{type})),link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function reportView(report){statusBox.replaceChildren();const heading=document.createElement('div');heading.id='decision';heading.textContent=report.decision;statusBox.append(heading);
for(const text of [report.summary,'Decision scope: '+report.scope]){const paragraph=document.createElement('p');paragraph.textContent=text;statusBox.append(paragraph)}
const details=document.createElement('pre');details.textContent=JSON.stringify(report.metrics,null,2);statusBox.append(details);
const list=document.createElement('ul');for(const item of [...report.problems.map(problem=>problem.category+': '+problem.message),...report.unknowns.map(item=>'UNKNOWN: '+item)]){const row=document.createElement('li');row.textContent=item;list.append(row)}statusBox.append(list);
for(const [label,type,name,data] of [['Download QA report','text/markdown','qa-report.md',report.markdown],['Download evidence','application/json','qa-evidence.json',JSON.stringify(report,null,2)]]){const control=document.createElement('button');control.type='button';control.textContent=label;control.onclick=()=>download(data,type,name);statusBox.append(control)}}
async function read(response){const text=await response.text();let result;try{result=JSON.parse(text)}catch{throw new Error('Service returned HTTP '+response.status+'. Try again or check the service limits.')}if(!response.ok)throw new Error(result.detail||'Service error '+response.status);return result}
const ready=fetch('/api/health').then(read).then(health=>{if(health.mode==='python-runner'){mode='runner';document.querySelector('#requirements').accept='.json,.txt,.md,.pdf,.doc,.docx';document.querySelector('#formats').textContent='Full runner supports JSON, TXT, Markdown, PDF, DOC and DOCX.';document.querySelector('#headed-option').hidden=false;document.querySelector('#mode').textContent='Full Python QA engine: browser exploration, supplied workflows and detailed QA report.'}}).catch(()=>{});
form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;show('Starting browser assessment…');const started=Date.now();let timer;try{await ready;timer=setInterval(()=>show('Assessment running: '+Math.floor((Date.now()-started)/1000)+' seconds. Gathering evidence…'),1000);const body=new FormData(form);if(!body.get('requirements')?.size)body.delete('requirements');
if(mode==='runner'){const job=await read(await fetch('/api/jobs',{method:'POST',body}));clearInterval(timer);for(let poll=0;poll<180;poll++){const state=await read(await fetch('/api/jobs/'+job.job_id));show(state.status+'\\n'+(state.recent_log||''));if(state.status==='FAILED')throw new Error(state.error||'Runner failed');if(state.status==='COMPLETE'){show((state.decision||'INSUFFICIENT EVIDENCE')+'\\n'+(state.summary||''));const link=document.createElement('a');link.href='/api/jobs/'+job.job_id+'/report';link.textContent='Download QA report';statusBox.append(link);return}await new Promise(resolve=>setTimeout(resolve,2000))}throw new Error('Polling stopped after six minutes. Job ID: '+job.job_id)}
const report=await read(await fetch('/api/assess',{method:'POST',body}));clearInterval(timer);reportView(report)
}catch(error){show(error.message||'Assessment failed')}finally{clearInterval(timer);button.disabled=false}});
</script></body></html>`;

async function nativeAssessment(request: Request, env: Env): Promise<Response> {
  let browser: Awaited<ReturnType<typeof launch>> | undefined;
  try {
    const reader = request.body?.getReader();
    if (!reader) throw new Error('An assessment form is required');
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > 1_100_000) {
        await reader.cancel();
        return Response.json({ detail: 'Upload must be smaller than 1 MB' }, { status: 413 });
      }
      chunks.push(chunk.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const form = await new Response(bytes, { headers: { 'content-type': request.headers.get('content-type') || '' } }).formData();
    const target = publicTarget(String(form.get('target_url') || ''));
    if (form.get('headed') === 'true') throw new Error('Cloud assessment supports headless mode only');
    const upload = form.get('requirements');
    let contract: Contract | null = null, brief: string | null = null;
    if (upload instanceof File && upload.size) {
      if (!/\.(json|txt|md)$/i.test(upload.name)) throw new Error('Cloud mode accepts JSON, TXT or Markdown. PDF/DOC files require the full Python runner.');
      const text = await upload.text();
      if (/\.json$/i.test(upload.name)) contract = parseContract(JSON.parse(text), target.origin);
      else brief = text.slice(0, 100_000);
    }
    const authorized = form.get('authorized') === 'true';
    if (form.get('allow_form_submission') === 'true' && !authorized) throw new Error('Confirm testing permission before enabling workflow interactions');
    browser = await launch(env.BROWSER);
    return Response.json(await assess(browser, target, contract, authorized, form.get('allow_form_submission') === 'true', brief));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/429|rate limit|quota|time limit exceeded/i.test(message)) return Response.json({ detail: /time limit exceeded|quota/i.test(message) ? 'Cloudflare browser allowance is exhausted. Retry after your allowance resets.' : 'Cloudflare is limiting new browser sessions. Wait one minute and try again.', decision: 'INSUFFICIENT EVIDENCE', category: 'PROVIDER_LIMIT' }, { status: 429, headers: { 'retry-after': '60' } });
    return Response.json({ detail: message, decision: 'INSUFFICIENT EVIDENCE' }, { status: browser ? 503 : /browser|quota|429|limit exceeded/i.test(message) ? 503 : 400 });
  } finally { if (browser) await browser.close().catch(() => {}); }
}

async function proxy(request: Request, env: Env): Promise<Response> {
  if (!env.QA_API_BASE_URL) return Response.json({ detail: 'Full Python runner is not configured' }, { status: 503 });
  const incoming = new URL(request.url), headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('authorization');
  if (env.QA_API_TOKEN) headers.set('authorization', 'Bearer ' + env.QA_API_TOKEN);
  return fetch(new URL(incoming.pathname + incoming.search, env.QA_API_BASE_URL), { method: request.method, headers, body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body, redirect: 'manual' });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/api/health') return Response.json({ ok: true, version: 'cloud-workflows-1', mode: env.QA_API_BASE_URL && !env.QA_API_BASE_URL.includes('.example.com') ? 'python-runner' : 'cloudflare-browser-run' });
    if (url.pathname === '/api/example-contract') return Response.json({ scope: 'DemoQA title and HTTP response only', scope_approved: false, requirements: [{ id: 'REQ-HOME', test_ids: ['home'] }], tests: [{ id: 'home', url: 'https://demoqa.com/', steps: [], assertions: [{ kind: 'http_status', expected: 200 }, { kind: 'visible', selector: 'body', expected: true }] }] }, { headers: { 'content-disposition': 'attachment; filename="requirements.json"' } });
    if (url.pathname === '/api/assess' && request.method === 'POST') return nativeAssessment(request, env);
    if (url.pathname.startsWith('/api/')) {
      try { return await proxy(request, env); }
      catch { return Response.json({ detail: 'The Python runner is unavailable' }, { status: 503 }); }
    }
    return new Response(page, { headers: { 'content-type': 'text/html;charset=UTF-8', 'content-security-policy': "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'", 'x-content-type-options': 'nosniff', 'cache-control': 'no-store' } });
  },
};
