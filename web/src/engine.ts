import type { Browser, Page } from '@cloudflare/playwright';
import { Buffer } from 'node:buffer';

export type Outcome = 'PASS' | 'FAIL' | 'UNKNOWN';
type Step = { operation: string; selector: string; value?: string };
type Assertion = { kind: string; selector?: string; expected?: string | number | boolean };
type Test = { id: string; url: string; steps: Step[]; assertions: Assertion[] };
export type Contract = { scope: string; scope_approved: boolean; requirements: { id: string; test_ids: string[] }[]; tests: Test[] };
type Action = { id: string; selector: string; label: string; kind: string; href: string; safe: boolean };
type Snapshot = { id: string; url: string; title: string; actions: Action[]; overflow_px: number; truncated: boolean };
type Execution = { pair: string; before: string; action: Action; after?: string; outcome: string; error?: string };
type Check = { test_id: string; attempt: number; kind: string; selector?: string; expected: unknown; observed: unknown; outcome: Outcome; state?: string };
const risky = /delete|remove|logout|log.out|sign.out|purchase|buy|checkout|subscribe|unsubscribe|pay|transfer|send|submit|confirm|reset|cancel|disconnect/i;

export function publicTarget(value: string): URL {
  if (value.includes('](')) throw new Error('Use a plain URL, not Markdown syntax');
  const target = new URL(value.trim());
  const host = target.hostname.toLowerCase().replace(/\.$/, '');
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password) throw new Error('A public HTTP or HTTPS URL is required');
  if (!host.includes('.') || host.includes(':') || host === 'localhost' || /\.(localhost|local|internal|test|invalid|example)$/.test(host) || /^\d+\.\d+\.\d+\.\d+$/.test(host)) throw new Error('Use a public domain name; local names and IP addresses are blocked');
  target.hash = '';
  return target;
}

export function parseContract(value: unknown, origin: string): Contract {
  if (!value || typeof value !== 'object') throw new Error('The contract must be a JSON object');
  const contract = value as Contract;
  if (typeof contract.scope !== 'string' || !contract.scope.trim() || !Array.isArray(contract.tests) || !contract.tests.length || contract.tests.length > 10 || !Array.isArray(contract.requirements) || !contract.requirements.length || contract.requirements.length > 100) throw new Error('Provide scope, 1–10 tests, and 1–100 requirements');
  const testIds = new Set<string>();
  for (const test of contract.tests) {
    if (!test || typeof test.id !== 'string' || !test.id || testIds.has(test.id)) throw new Error('Every test needs a unique id');
    testIds.add(test.id);
    if (typeof test.url !== 'string' || publicTarget(test.url).origin !== origin) throw new Error('Test URLs must use the application origin');
    if (!Array.isArray(test.steps) || test.steps.length > 15 || !Array.isArray(test.assertions) || !test.assertions.length || test.assertions.length > 20) throw new Error('Each test needs at most 15 steps and 1–20 assertions');
    for (const step of test.steps) {
      if (!step || !['click', 'double_click', 'right_click', 'fill', 'check', 'uncheck', 'select'].includes(step.operation) || typeof step.selector !== 'string' || !step.selector.trim()) throw new Error('Unsupported or incomplete workflow step');
      if (['fill', 'select'].includes(step.operation) && typeof step.value !== 'string') throw new Error('Fill and select steps require a string value');
    }
    for (const assertion of test.assertions) {
      if (!assertion || !['text_contains', 'text_equals', 'visible', 'value_equals', 'url_equals', 'http_status'].includes(assertion.kind)) throw new Error('Unsupported assertion kind');
      if (!['url_equals', 'http_status'].includes(assertion.kind) && (typeof assertion.selector !== 'string' || !assertion.selector.trim())) throw new Error('An assertion selector is required');
      if (assertion.kind === 'http_status' ? !Number.isInteger(assertion.expected) : assertion.kind === 'visible' ? assertion.expected !== undefined && typeof assertion.expected !== 'boolean' : typeof assertion.expected !== 'string') throw new Error('Assertion expected value has an invalid type');
    }
  }
  const requirementIds = new Set<string>();
  for (const requirement of contract.requirements) {
    if (!requirement || typeof requirement.id !== 'string' || !requirement.id || requirementIds.has(requirement.id) || !Array.isArray(requirement.test_ids) || !requirement.test_ids.length || requirement.test_ids.some(id => !testIds.has(id))) throw new Error('Every requirement needs a unique id and valid test_ids');
    requirementIds.add(requirement.id);
  }
  return contract;
}

async function digest(value: unknown): Promise<string> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(value)));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
}

async function capture(page: Page): Promise<Snapshot> {
  const observation = await page.evaluate(() => {
    const candidates = Array.from(document.querySelectorAll('a[href],button,[role="button"],[role="tab"],summary,input:not([type="hidden"]),select,textarea'));
    const elements = candidates.filter(element => { const rect = element.getBoundingClientRect(); return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== 'hidden'; });
    const actions = elements.slice(0, 120).map(element => {
      const segments: string[] = [];
      let current: Element | null = element;
      while (current && current !== document.documentElement) {
        const tag = current.tagName.toLowerCase();
        const siblings: Element[] = current.parentElement ? Array.from(current.parentElement.children).filter(sibling => sibling.tagName === current!.tagName) : [];
        segments.unshift(tag + ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')');
        current = current.parentElement;
      }
      const label = (element.getAttribute('aria-label') || element.textContent || element.getAttribute('title') || element.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 160);
      const href = element instanceof HTMLAnchorElement ? element.href : '';
      const kind = element.getAttribute('role') || element.tagName.toLowerCase();
      const disabled = element.matches(':disabled,[aria-disabled="true"]');
      return { selector: segments.join(' > '), label, href, kind, disabled, checked: element instanceof HTMLInputElement ? element.checked : undefined, value: element instanceof HTMLInputElement && element.type !== 'password' || element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement ? (element as HTMLInputElement).value : undefined, expanded: element.getAttribute('aria-expanded'), selected: element.getAttribute('aria-selected'), open: element.tagName === 'SUMMARY' ? element.parentElement?.hasAttribute('open') : undefined };
    });
    return { url: location.href, title: document.title, text: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 8000), actions, overflow_px: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth), truncated: elements.length > 120 };
  });
  const actions: Action[] = [];
  for (const action of observation.actions) {
    let safe = !action.disabled && !risky.test(action.label + ' ' + action.href) && ['a', 'tab', 'summary'].includes(action.kind);
    if (action.href) {
      try { safe = safe && publicTarget(action.href).origin === new URL(observation.url).origin && action.href !== observation.url; }
      catch { safe = false; }
    }
    actions.push({ ...action, id: await digest([action.kind, action.label, action.href, action.selector]), safe });
  }
  return { ...observation, actions, id: await digest([observation.url, observation.actions, observation.text]) };
}

export async function assess(browser: Browser, target: URL, contract: Contract | null, authorized: boolean, allowWrites: boolean, brief: string | null) {
  const started = Date.now(), deadline = started + 55_000;
  const states = new Map<string, Snapshot>(), discovered = new Map<string, { state: string; action: Action }>(), executions = new Map<string, Execution>();
  const checks: Check[] = [], tests: { id: string; attempt: number; outcome: Outcome; error?: string }[] = [];
  const executionAttempts: (Execution & { test_id: string; attempt: number })[] = [];
  const problems: { category: string; message: string; url?: string }[] = [];
  const screenshotEvidence: { test_id: string; attempt: number; data_url: string }[] = [];
  const pages = new Set<string>();
  let operations = 0, replays = 0, blockedRequests = 0, stop = 'SAFE_FRONTIER_COMPLETE';
  const contexts: Awaited<ReturnType<Browser['newContext']>>[] = [];
  const deadlineTimer = setTimeout(() => { stop = 'MAX_SECONDS'; void Promise.allSettled(contexts.map(context => context.close())); }, 55_000);
  const remaining = () => {
    if (Date.now() >= deadline) throw new Error('MAX_SECONDS');
    if (operations >= 40) throw new Error('MAX_ACTIONS');
    return Math.max(1, Math.min(4000, deadline - Date.now()));
  };
  const record = async (page: Page) => {
    remaining();
    const state = await capture(page);
    states.set(state.id, state);
    pages.add(state.url);
    for (const action of state.actions) discovered.set(state.id + ':' + action.id, { state: state.id, action });
    return state;
  };
  const prepare = async () => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block', acceptDownloads: false });
    contexts.push(context);
    await context.route('**/*', async route => {
      const request = route.request();
      try {
        const url = publicTarget(request.url());
        if (request.isNavigationRequest() && url.origin !== target.origin || !['GET', 'HEAD', 'OPTIONS'].includes(request.method()) && !(contract && authorized && allowWrites && url.origin === target.origin)) {
          blockedRequests++;
          return route.abort();
        }
        await route.continue();
      } catch { blockedRequests++; await route.abort(); }
    });
    const page = await context.newPage();
    page.setDefaultTimeout(4000);
    page.on('dialog', dialog => { void dialog.dismiss().catch(() => {}); });
    page.on('popup', popup => { void popup.close().catch(() => {}); });
    page.on('pageerror', error => { if (problems.length < 40) problems.push({ category: 'RUNTIME_OBSERVATION', message: String(error).slice(0, 500), url: page.url() }); });
    return page;
  };
  const navigate = async (page: Page, url: string) => {
    const timeout = Math.min(12_000, Math.max(1, deadline - Date.now()));
    remaining();
    const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
    await page.waitForTimeout(Math.min(400, Math.max(1, deadline - Date.now())));
    if (response && response.status() >= 400) problems.push({ category: 'HTTP_OBSERVATION', message: 'HTTP ' + response.status(), url: page.url() });
    return response?.status() ?? null;
  };
  try {
    if (contract) {
      for (const test of contract.tests) {
        for (let attempt = 1; attempt <= 2; attempt++) {
          remaining();
          const page = await prepare();
          let outcome: Outcome = 'UNKNOWN';
          try {
            const status = await navigate(page, test.url);
            await record(page);
            for (const step of test.steps) {
              if (!authorized) throw new Error('Testing authorization is required for workflow actions');
              if (!allowWrites) throw new Error('Enable approved workflow interactions to execute requirement steps');
              const timeout = remaining();
              const locator = page.locator(step.selector);
              if (await locator.count() !== 1) throw new Error('The workflow locator must match exactly one element: ' + step.selector);
              const before = await record(page);
              const action: Action = { id: await digest(['contract', test.id, step]), selector: step.selector, label: step.operation, kind: step.operation, href: '', safe: false };
              const pair = before.id + ':' + action.id;
              discovered.set(pair, { state: before.id, action });
              operations++;
              const execution: Execution & { test_id: string; attempt: number } = { pair, before: before.id, action, outcome: 'ERROR', test_id: test.id, attempt };
              executionAttempts.push(execution);
              executions.set(pair, execution);
              try {
                if (step.operation === 'fill') await locator.fill(step.value!, { timeout });
                else if (step.operation === 'select') await locator.selectOption(step.value!, { timeout });
                else if (step.operation === 'check') await locator.check({ timeout });
                else if (step.operation === 'uncheck') await locator.uncheck({ timeout });
                else if (step.operation === 'double_click') await locator.dblclick({ timeout });
                else await locator.click({ timeout, button: step.operation === 'right_click' ? 'right' : 'left' });
                const after = await record(page);
                execution.after = after.id;
                execution.outcome = before.id === after.id ? 'UNCHANGED' : 'CHANGED';
              } catch (error) { execution.error = String(error); throw error; }
            }
            for (const assertion of test.assertions) {
              const timeout = remaining();
              let observed: unknown;
              const expected = assertion.kind === 'visible' ? assertion.expected ?? true : assertion.expected;
              if (assertion.kind === 'http_status') observed = status;
              else if (assertion.kind === 'url_equals') observed = page.url();
              else {
                const locator = page.locator(assertion.selector!);
                if (assertion.kind === 'visible') {
                  if (expected === true) await locator.waitFor({ state: 'visible', timeout });
                  else await locator.waitFor({ state: 'hidden', timeout });
                  observed = await locator.isVisible();
                } else {
                  await locator.waitFor({ state: 'attached', timeout });
                  if (await locator.count() !== 1) throw new Error('Ambiguous assertion locator');
                  observed = assertion.kind === 'value_equals' ? await locator.inputValue({ timeout }) : (await locator.innerText({ timeout })).trim();
                }
              }
              const passed = assertion.kind === 'text_contains' ? String(observed).includes(String(expected)) : observed === expected;
              checks.push({ test_id: test.id, attempt, kind: assertion.kind, selector: assertion.selector, expected, observed, outcome: passed ? 'PASS' : 'FAIL', state: (await record(page)).id });
            }
            outcome = checks.some(check => check.test_id === test.id && check.attempt === attempt && check.outcome === 'FAIL') ? 'FAIL' : 'PASS';
            tests.push({ id: test.id, attempt, outcome });
          } catch (error) {
            tests.push({ id: test.id, attempt, outcome: 'UNKNOWN', error: String(error) });
          } finally {
            if (outcome !== 'PASS' && Date.now() < deadline && screenshotEvidence.length < 2) {
              try { screenshotEvidence.push({ test_id: test.id, attempt, data_url: 'data:image/jpeg;base64,' + Buffer.from(await page.screenshot({ type: 'jpeg', quality: 45, timeout: Math.min(2000, Math.max(1, deadline - Date.now())) })).toString('base64') }); } catch {}
            }
            await page.context().close().catch(() => {});
          }
        }
      }
      stop = 'REQUIREMENT_TESTS_COMPLETE';
    } else {
      const queue: { path: Action[]; expected?: string }[] = [{ path: [] }];
      const expanded = new Set<string>();
      const page = await prepare();
      await navigate(page, target.href);
      let current = await record(page);
      while (queue.length) {
        remaining();
        if (pages.size >= 5 || states.size >= 20) { stop = pages.size >= 5 ? 'MAX_PAGES' : 'MAX_STATES'; break; }
        const item = queue.shift()!;
        if (item.expected && current.id !== item.expected) {
          if (++replays > 6) { stop = 'MAX_REPLAYS'; break; }
          try {
            await navigate(page, target.href);
            for (const action of item.path) {
              const replayState = await record(page);
              if (!replayState.actions.some(candidate => candidate.id === action.id && candidate.safe)) throw new Error('Replay action identity changed');
              operations++;
              await page.locator(action.selector).click({ timeout: remaining() });
            }
            current = await record(page);
            if (current.id !== item.expected) { problems.push({ category: 'REPLAY_FAILURE', message: 'State changed during replay; path skipped' }); continue; }
          } catch (error) { problems.push({ category: 'REPLAY_FAILURE', message: String(error) }); continue; }
        }
        if (expanded.has(current.id)) continue;
        expanded.add(current.id);
        const source = current;
        for (const action of source.actions.filter(action => action.safe)) {
          remaining();
          if (pages.size >= 5 || states.size >= 20) break;
          const pair = source.id + ':' + action.id;
          if (executions.has(pair)) continue;
          if (current.id !== source.id) { queue.push({ path: item.path, expected: source.id }); expanded.delete(source.id); break; }
          if (item.path.length >= 3) continue;
          const before = await record(page);
          if (before.id !== source.id) { problems.push({ category: 'STATE_DRIFT', message: 'DOM changed before action; execution skipped' }); break; }
          const execution: Execution = { pair, before: before.id, action, outcome: 'ERROR' };
          executions.set(pair, execution);
          operations++;
          try {
            await page.locator(action.selector).click({ timeout: remaining() });
            await page.waitForTimeout(Math.min(250, Math.max(1, deadline - Date.now())));
            current = await record(page);
            execution.after = current.id;
            execution.outcome = current.id === before.id ? 'UNCHANGED' : 'CHANGED';
            if (current.id !== source.id) queue.unshift({ path: [...item.path, action], expected: current.id });
          } catch (error) {
            execution.error = String(error);
            problems.push({ category: 'AUTOMATION_FAILURE', message: String(error), url: page.url() });
            current = await record(page);
          }
        }
      }
    }
  } catch (error) {
    stop = /MAX_SECONDS|MAX_ACTIONS/.test(String(error)) ? String(error).replace('Error: ', '') : 'EXECUTION_ERROR';
    if (stop === 'EXECUTION_ERROR') problems.push({ category: 'ENVIRONMENT_OR_AUTOMATION', message: String(error) });
  } finally { clearTimeout(deadlineTimer); await Promise.allSettled(contexts.map(context => context.close())); }
  for (const test of contract?.tests ?? []) {
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (!tests.some(result => result.id === test.id && result.attempt === attempt)) tests.push({ id: test.id, attempt, outcome: 'UNKNOWN', error: 'Not executed: ' + stop });
    }
  }
  const requirementResults = contract?.requirements.map(requirement => ({ id: requirement.id, test_ids: requirement.test_ids, outcome: requirement.test_ids.every(id => tests.filter(test => test.id === id && test.outcome === 'PASS').length === 2) ? 'PASS' : requirement.test_ids.some(id => tests.filter(test => test.id === id && test.outcome === 'FAIL').length === 2) ? 'FAIL' : 'UNKNOWN' })) ?? [];
  const allPassed = tests.length === (contract?.tests.length ?? 0) * 2 && tests.length > 0 && tests.every(test => test.outcome === 'PASS');
  const confirmedFailure = checks.some(first => first.attempt === 1 && first.outcome === 'FAIL' && checks.some(second => second.attempt === 2 && second.outcome === 'FAIL' && first.test_id === second.test_id && first.kind === second.kind && first.selector === second.selector && first.expected === second.expected && first.observed === second.observed));
  const decision = confirmedFailure && !blockedRequests ? 'BLOCK' : contract?.scope_approved === true && authorized && allPassed && requirementResults.every(result => result.outcome === 'PASS') && !problems.length && !blockedRequests ? 'RELEASE' : 'INSUFFICIENT EVIDENCE';
  const frontier = [...discovered.entries()].filter(([pair]) => !executions.has(pair)).map(([pair, item]) => ({ pair, state: item.state, label: item.action.label, reason: !item.action.safe ? 'REQUIRES_EXPLICIT_TEST' : 'NOT_EXECUTED_WITHIN_RUN' }));
  if (stop === 'SAFE_FRONTIER_COMPLETE' && frontier.some(item => item.reason === 'NOT_EXECUTED_WITHIN_RUN')) stop = 'SAFE_FRONTIER_PARTIAL';
  const metrics = { duration_ms: Date.now() - started, pages: pages.size, states: states.size, discovered_actions: new Set([...discovered.values()].map(item => item.action.id)).size, executed_actions: new Set([...executions.values()].map(item => item.action.id)).size, discovered_pairs: discovered.size, executed_pairs: executions.size, unexplored_pairs: frontier.length, pair_execution_percent: discovered.size ? Math.round(executions.size / discovered.size * 100) : null, operations, replays, assertions: checks.length, requirements: requirementResults.length, requirements_passed: requirementResults.filter(result => result.outcome === 'PASS').length, reconciled: executions.size + frontier.length === discovered.size && [...executions.keys()].every(pair => discovered.has(pair)), stop_reason: stop, execution_mode: 'HEADLESS_CLOUDFLARE_BROWSER_RUN' };
  const unknowns = ['Undeclared business rules', 'Authentication and permissions outside the supplied workflow', 'API/backend consistency, security, performance, mutation and historical regression testing', ...(!contract ? ['No executable requirement contract supplied'] : contract.scope_approved !== true ? ['Requirement scope is not approved'] : []), ...(!authorized ? ['Testing authority not confirmed'] : []), ...(brief ? ['Product brief retained as context; natural-language requirements are not automatically verified'] : []), ...([...states.values()].some(state => state.truncated) ? ['DOM discovery truncated to 120 controls per state'] : [])];
  const report = { run_id: crypto.randomUUID(), target: target.href, decision, scope: contract?.scope ?? 'Bounded discovery of safe links, tabs and disclosures', summary: `${pages.size} page(s), ${executions.size} unique state/action pair(s), ${checks.length} assertion(s). ${frontier.length} discovered pair(s) untested.`, metrics: { ...metrics, blocked_requests: blockedRequests }, requirements: requirementResults, tests, checks, states: [...states.values()], executions: [...executions.values()], execution_attempts: executionAttempts, frontier, problems, screenshots: screenshotEvidence, unknowns: [...unknowns, ...(blockedRequests ? ['Network safety rules blocked requests; resulting behavior may be incomplete'] : [])], brief_supplied: Boolean(brief) };
  const printable = { ...report, screenshots: screenshotEvidence.map(item => ({ ...item, data_url: 'Included in downloadable JSON evidence' })) };
  const markdown = '# QA Report\n\nDecision: **' + decision + '**\n\nScope: ' + report.scope.replace(/[\r\n]/g, ' ') + '\n\n' + report.summary + '\n\nThis decision applies only to the stated scope. Action execution is not proof of business correctness.\n\n## Evidence\n\n```json\n' + JSON.stringify(printable, null, 2).replace(/`/g, '\\u0060') + '\n```\n';
  return { ...report, markdown };
}
