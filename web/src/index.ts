interface Env {
  QA_API_BASE_URL?: string;
  QA_API_TOKEN?: string;
  QA_UI_ACCESS_KEY?: string;
}

const page = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonomous Quality Engineering</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui;background:#07111f;color:#e8f0ff}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 10% 0,#17335c 0,transparent 35%),#07111f}.shell{max-width:920px;margin:auto;padding:64px 24px}.eyebrow{color:#69e6c1;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{font-size:clamp(2.5rem,7vw,5rem);line-height:.95;margin:.35em 0}p{color:#aabbd5;line-height:1.7}.card{margin-top:36px;padding:28px;border:1px solid #294464;border-radius:22px;background:#0b192bbf;box-shadow:0 30px 90px #0008}label{display:block;font-weight:750;margin:18px 0 8px}input[type=url],input[type=file]{width:100%;padding:14px;border-radius:12px;border:1px solid #35516f;background:#071321;color:#fff}button{margin-top:24px;padding:14px 22px;border:0;border-radius:12px;background:#69e6c1;color:#05140f;font-weight:850;cursor:pointer}button:disabled{opacity:.5}.options{display:flex;gap:24px;flex-wrap:wrap;margin-top:18px}.options label{margin:0;font-weight:600}.status{display:none;margin-top:24px;padding:18px;border-radius:14px;background:#071321;white-space:pre-wrap}.decision{font-size:1.5rem;font-weight:900}.RELEASE{color:#69e6c1}.BLOCK{color:#ff718b}.INSUFFICIENT{color:#ffd166}small{color:#7890ad}</style></head>
<body><main class="shell"><div class="eyebrow">V11 Quality Organization</div><h1>Test what matters.<br>Prove what happened.</h1><p>Enter a public application URL. Optionally attach approved requirements or a product brief. The system preserves unknowns and returns RELEASE, BLOCK, or INSUFFICIENT EVIDENCE.</p>
<section class="card"><form id="form"><label for="access">Access key</label><input id="access" type="password" autocomplete="current-password" required>
<label for="url">Application URL</label><input id="url" name="target_url" type="url" placeholder="https://example.com/" required>
<label for="requirements">Requirements or product brief</label><input id="requirements" name="requirements" type="file" accept=".txt,.md,.json,.pdf,.doc,.docx"><small>TXT, Markdown, JSON, PDF, DOC, or DOCX. Approved JSON contracts can drive a scoped release gate.</small>
<div class="options"><label><input name="allow_form_submission" type="checkbox" value="true"> Allow safe form submission</label></div>
<button id="run" type="submit">Start quality assessment</button></form><div id="status" class="status"></div></section></main>
<script>
const form=document.querySelector('#form'),statusBox=document.querySelector('#status'),button=document.querySelector('#run'),access=document.querySelector('#access');
const show=t=>{statusBox.style.display='block';statusBox.innerHTML=t};
const headers=()=>({'x-qa-access-key':access.value});
async function downloadReport(id){const response=await fetch('/api/jobs/'+id+'/report',{headers:headers()});if(!response.ok)throw new Error('Unable to download report');const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='qa-v11-'+id+'.md';a.click();URL.revokeObjectURL(url)}
form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;show('Submitting assessment…');try{const body=new FormData(form);if(!body.get('requirements')?.size)body.delete('requirements');const response=await fetch('/api/jobs',{method:'POST',body,headers:headers()});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Unable to start');const id=data.job_id;for(;;){await new Promise(r=>setTimeout(r,2500));const check=await fetch('/api/jobs/'+id,{headers:headers()}),job=await check.json();show('<strong>Job '+id+'</strong>\n\n'+(job.recent_log||job.status));if(job.status==='COMPLETE'){const cls=job.decision==='INSUFFICIENT EVIDENCE'?'INSUFFICIENT':job.decision;show('<div class="decision '+cls+'">'+job.decision+'</div><p>'+JSON.stringify(job.summary,null,2)+'</p><button type="button" id="download">Download QA Director report</button>');document.querySelector('#download').onclick=()=>downloadReport(id);break}if(job.status==='FAILED')throw new Error(job.error||'Assessment failed')}}}catch(error){show('<span style="color:#ff718b">'+error.message+'</span>')}finally{button.disabled=false}});
</script></body></html>`;

async function proxy(request: Request, env: Env): Promise<Response> {
  if (!env.QA_API_BASE_URL) return Response.json({ detail: "QA_API_BASE_URL is not configured" }, { status: 503 });
  if (!env.QA_UI_ACCESS_KEY || request.headers.get("x-qa-access-key") !== env.QA_UI_ACCESS_KEY) {
    return Response.json({ detail: "Invalid access key" }, { status: 401 });
  }
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, env.QA_API_BASE_URL);
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("x-qa-access-key");
  if (env.QA_API_TOKEN) headers.set("authorization", `Bearer ${env.QA_API_TOKEN}`);
  return fetch(target, { method: request.method, headers, body: request.method === "GET" ? undefined : request.body, redirect: "manual" });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return proxy(request, env);
    return new Response(page, { headers: { "content-type": "text/html;charset=UTF-8", "content-security-policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'", "x-content-type-options": "nosniff" } });
  },
};
