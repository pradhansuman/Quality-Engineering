#!/usr/bin/env python3
"""V10 autonomous QA planner using V9.2.10 as its evidence execution core."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin


VERSION = "10.1.0"
HERE = Path(__file__).resolve().parent
CORE_PATH = HERE / "qa9210.py"
if not CORE_PATH.is_file():
    CORE_PATH = HERE / "qa_agent_v9_2_10_WORKING_FINAL_WORKING.py"


def load_core():
    spec = importlib.util.spec_from_file_location("qa_v9210_core", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--requirements-only", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--openapi", type=Path)
    parser.add_argument("--product-brief", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", default=True)
    mode.add_argument("--headed", action="store_true")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--max-actions", type=int, default=80)
    parser.add_argument("--nav-timeout", type=int, default=15000)
    parser.add_argument("--dom-timeout", type=int, default=10000)
    parser.add_argument("--workflow-timeout", type=int, default=15000)
    parser.add_argument("--journey-timeout", type=int, default=15000)
    parser.add_argument("--max-recovery-attempts", type=int, default=2)
    parser.add_argument("--max-replays", type=int, default=8)
    parser.add_argument("--max-replay-actions", type=int, default=20)
    parser.add_argument("--max-audit-pages", type=int, default=2)
    parser.add_argument("--allow-form-submission", action="store_true")
    parser.add_argument("--mutation-testing", action="store_true")
    parser.add_argument("--max-mutations", type=int, default=20)
    parser.add_argument("--no-page-audits", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("qa_v10_report"))
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args(argv)


def bounded_text(path, limit=1_000_000):
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f"input exceeds {limit} bytes: {path}")
    return data.decode("utf-8")


def repository_model(root):
    if not root:
        return {"provided": False, "facts": [], "unknowns": ["Application repository was not provided"]}
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("--repo must be a directory")
    extensions, names, files, total = Counter(), Counter(), [], 0
    ignored = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    for path in root.rglob("*"):
        if any(part in ignored for part in path.parts) or not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        total += size
        if len(files) >= 3000 or total > 30 * 1024 * 1024:
            break
        relative = str(path.relative_to(root))
        files.append(relative)
        extensions[path.suffix.lower() or "<none>"] += 1
        names[path.name.lower()] += 1
    signals = {
        "ui": any(ext in extensions for ext in (".html", ".tsx", ".jsx", ".vue", ".svelte")),
        "api": any(name in names for name in ("openapi.json", "openapi.yaml", "swagger.json")) or
               any("route" in item.lower() or "controller" in item.lower() for item in files),
        "database": any("migration" in item.lower() or "schema" in item.lower() for item in files),
        "tests": any("test" in Path(item).name.lower() or "spec" in Path(item).name.lower() for item in files),
        "authentication": any(any(word in item.lower() for word in ("auth", "login", "session", "permission")) for item in files),
    }
    return {"provided": True, "root": str(root), "file_count": len(files), "bytes_scanned": total,
            "extension_counts": dict(extensions.most_common(20)), "signals": signals,
            "inventory_sha256": hashlib.sha256(json.dumps(sorted(files)).encode()).hexdigest(),
            "facts": [key for key, value in signals.items() if value],
            "unknowns": ["Runtime behavior and business intent cannot be proven from filenames alone"]}


def parse_openapi(path, target):
    if not path:
        return {"provided": False, "operations": [], "generated_tests": []}
    document = json.loads(bounded_text(path))
    operations, tests = [], []
    for route, methods in document.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            method = method.upper()
            if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            identifier = (operation or {}).get("operationId") or f"{method.lower()}:{route}"
            risk = "HIGH" if method in {"POST", "PUT", "PATCH", "DELETE"} else "MEDIUM"
            operations.append({"id": identifier, "method": method, "path": route, "risk": risk})
            responses = (operation or {}).get("responses", {})
            success = next((int(code) for code in responses if str(code).isdigit() and 200 <= int(code) < 300), None)
            if success and "{" not in route and method in {"GET", "HEAD"}:
                tests.append({"id": f"openapi:{identifier}", "url": urljoin(target, route), "method": method,
                              "expected_status": success, "provenance": "OPENAPI_DECLARATION"})
    return {"provided": True, "operations": operations[:500], "generated_tests": tests[:50],
            "truncated": len(operations) > 500 or len(tests) > 50}


def requirement_model(path):
    if not path:
        return {"provided": False, "declared": 0, "approved": False, "critical": 0}
    document = json.loads(bounded_text(path))
    requirements = document.get("requirements", [])
    return {"provided": True, "declared": len(requirements), "approved": document.get("scope_approved") is True,
            "critical": sum(item.get("critical") is True for item in requirements),
            "scope": document.get("scope"), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def strategy(product, requirements, openapi):
    tests = [
        {"family": "UI_DISCOVERY", "priority": 70, "why": "Map current visible behavior and workflows"},
        {"family": "NEGATIVE_NAVIGATION", "priority": 75, "why": "Detect broken same-origin routes and blocked journeys"},
        {"family": "ACCESSIBILITY_SMOKE", "priority": 55, "why": "Identify basic interaction barriers"},
        {"family": "RESPONSIVE_SMOKE", "priority": 50, "why": "Observe mobile, tablet and desktop behavior"},
    ]
    if openapi["operations"]:
        tests.append({"family": "API_CONTRACT", "priority": 80, "why": "Verify declared service behavior"})
    if product.get("signals", {}).get("authentication"):
        tests.append({"family": "AUTHORIZATION", "priority": 95, "why": "Permission failures can expose or corrupt data"})
    if product.get("signals", {}).get("database"):
        tests.append({"family": "DATA_CONSISTENCY", "priority": 90, "why": "UI/API persistence must reconcile"})
    if requirements["critical"]:
        tests.append({"family": "CRITICAL_JOURNEYS", "priority": 100, "why": "Explicit critical requirements take precedence"})
    return sorted(tests, key=lambda item: -item["priority"])


def flakiness(results):
    grouped = {}
    for result in results:
        grouped.setdefault(result.get("test_id", "UNKNOWN"), []).append(result)
    findings = []
    for test_id, runs in sorted(grouped.items()):
        statuses = {item.get("status", "UNKNOWN") for item in runs}
        signatures = {json.dumps({"status": item.get("status"), "classification": item.get("classification"),
                                  "assertions": item.get("assertions")}, sort_keys=True, default=str) for item in runs}
        if len(runs) > 1 and len(signatures) > 1:
            classification = "FLAKY_CANDIDATE"
        elif len(runs) > 1 and statuses <= {"PASS"}:
            classification = "REPEATABLE_PASS"
        elif len(runs) > 1:
            classification = "REPEATABLE_NONPASS"
        else:
            classification = "INSUFFICIENT_REPETITIONS"
        findings.append({"test_id": test_id, "runs": len(runs), "statuses": sorted(statuses),
                         "classification": classification,
                         "evidence_refs": [item.get("evidence_id") for item in runs if item.get("evidence_id")]})
    return {"method": "Compare normalized outcomes from repeated executions in this run",
            "findings": findings,
            "flaky_candidates": sum(item["classification"] == "FLAKY_CANDIDATE" for item in findings)}


def defects(root_causes, workflow_verdict):
    confirmed = {"CONFIRMED_REQUIREMENT_FAILURE", "CONFIRMED_REGRESSION"}
    grouped = {}
    for finding in root_causes.get("findings", []):
        classification = finding.get("classification", "UNKNOWN")
        key = (classification, finding.get("summary"), finding.get("layer", "unknown"))
        item = grouped.setdefault(key, {"type": "CONFIRMED_DEFECT" if classification in confirmed else "INVESTIGATION_CANDIDATE",
                                        "severity": finding.get("severity", "medium"), "classification": classification,
                                        "summary": finding.get("summary"), "probable_layer": finding.get("layer", "unknown"),
                                        "confidence": finding.get("confidence"), "evidence_refs": [], "occurrences": 0,
                                        "reproduction": "Replay the evidence-linked test or state path using the same run configuration.",
                                        "next_diagnostic": finding.get("next_diagnostic")})
        item["occurrences"] += 1
        item["evidence_refs"].extend(finding.get("evidence_refs", []))
        item["evidence_refs"] = sorted(set(item["evidence_refs"]))
    reports = []
    for index, item in enumerate(sorted(grouped.values(), key=lambda value: (
            value["type"] != "CONFIRMED_DEFECT", value["classification"], value["summary"] or "")), 1):
        reports.append({"id": f"V10-DEFECT-{index:04d}", **item})
    return {"confirmed_count": sum(item["type"] == "CONFIRMED_DEFECT" for item in reports),
            "candidate_count": sum(item["type"] == "INVESTIGATION_CANDIDATE" for item in reports),
            "reports": reports, "requirement_findings": workflow_verdict.get("findings", [])}


def candidate_requirements(behavior_plan, risk_model, critical_journeys):
    risk_by_url = {item["url"]: item for item in risk_model.get("risks", [])}
    journeys_by_url = {}
    for journey in critical_journeys.get("observed_multi_step_candidates", []):
        journeys_by_url.setdefault(journey["entry_url"], []).append(journey)
    candidates = []
    for plan in behavior_plan.get("plans", []):
        url = plan["url"]
        risk = risk_by_url.get(url, {})
        candidates.append({
            "id": "candidate:" + hashlib.sha256(url.encode()).hexdigest()[:12],
            "title": f"Validate {plan['inferred_behavior']} at {url}",
            "url": url, "priority": risk.get("priority", plan.get("priority", "MEDIUM")).upper(),
            "risk_score": risk.get("score"), "authority": "INFERRED_CURRENT_RUN_CANDIDATE",
            "approval_required": True, "input_action_ids": plan.get("input_actions", []),
            "completion_action_ids": plan.get("completion_candidates", []),
            "observed_journey_action_ids": [item["action_ids"] for item in journeys_by_url.get(url, [])],
            "suggested_test_families": ["positive", "negative", "boundary"] if plan.get("input_actions") else ["navigation", "error-path"],
            "missing_oracle": plan.get("required_oracle"),
        })
    return {"schema": "qa-v10-candidate-requirements-v1", "scope_approved": False,
            "authority": "Non-executable review draft from current-run observations",
            "candidates": sorted(candidates, key=lambda item: (
                {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(item["priority"], 3), item["url"])),
            "instructions": "Review business intent, add explicit expected outcomes and authorization, then convert approved candidates into the requirements contract."}


def quality_risk(truth, defect_report, flaky_report, requirements):
    coverage = truth["coverage"]
    points, contributors = 0, []
    def add(value, reason):
        nonlocal points
        points += value
        contributors.append({"points": value, "reason": reason})
    if not requirements["approved"]:
        add(25, "Business scope is not explicitly approved")
    if not coverage["reconciled"] or not coverage["complete_execution_evidence"]:
        add(40, "Execution evidence does not reconcile")
    if defect_report["confirmed_count"]:
        add(min(50, 25 * defect_report["confirmed_count"]), "Confirmed defects exist")
    if defect_report["candidate_count"]:
        add(min(20, 4 * defect_report["candidate_count"]), "Unresolved investigation candidates remain")
    if flaky_report["flaky_candidates"]:
        add(min(20, 10 * flaky_report["flaky_candidates"]), "Nondeterministic test outcomes were observed")
    unverified = coverage.get("unverified_state_action_pairs", 0)
    discovered = coverage.get("discovered_state_action_pairs", 0)
    if discovered and unverified:
        add(min(25, round(25 * unverified / discovered)), "Observed state/action frontier remains unverified")
    accepted_completion = truth.get("stop_reasons") in (["SCOPED_FRONTIER_EXHAUSTED"], ["DECLARED_WORKFLOWS_COMPLETE"])
    if truth.get("limit_reasons") or not accepted_completion:
        add(15, "Exploration ended with a budget or scope limit")
    score = min(100, points)
    band = "CRITICAL" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"
    return {"score": score, "band": band, "meaning": "Deterministic remaining-risk index, not defect probability",
            "confidence": "HIGH" if requirements["approved"] and truth["exploration_truth_gate"] else "LOW",
            "contributors": contributors}


def capability_matrix(agent, requirements, openapi, repository, truth):
    evidence = truth["coverage"]["reconciled"] and truth["coverage"]["complete_execution_evidence"]
    rows = [
        ("Product Understanding", "CONDITIONAL", "Observed product surfaces plus optional brief/repository signals"),
        ("Requirement Understanding", "IMPLEMENTED" if requirements["provided"] else "READY_NO_INPUT", "Explicit requirement contracts map to executable checks"),
        ("Risk Analysis", "IMPLEMENTED", "Current-run risk model and prioritization"),
        ("Critical Journey Detection", "IMPLEMENTED", "Declared journeys and observed multi-step candidates"),
        ("Test Strategy and Generation", "IMPLEMENTED", "Risk-ranked UI, API, negative, accessibility and responsive strategy"),
        ("UI Testing", "IMPLEMENTED", "Playwright state/action and workflow execution"),
        ("API Testing", "IMPLEMENTED" if openapi["provided"] or agent.api_results else "READY_NO_INPUT", "Explicit/OpenAPI safe API checks"),
        ("Data Validation", "CONDITIONAL", "Requires declared UI/API assertions or accessible backend data"),
        ("Negative Testing", "CONDITIONAL", "Safe inferred checks; write/submission tests require explicit opt-in"),
        ("Mutation Testing", "CONDITIONAL", "Available in V9 core when explicitly enabled and scoped"),
        ("Regression Intelligence", "CONDITIONAL", "Explicit baselines supported; change mapping requires repository references"),
        ("Failure Classification", "IMPLEMENTED", "Application, assertion, network, environment, framework and automation layers"),
        ("Root Cause Investigation", "CONDITIONAL", "Evidence-ranked probable layer; source causality requires code or telemetry"),
        ("Self-Healing", "CONDITIONAL", "Bounded safe locator/state replay recovery only"),
        ("Flaky-Test Detection", "IMPLEMENTED", "Repeated normalized outcome comparison"),
        ("Defect Generation", "IMPLEMENTED", "Evidence-linked confirmed defects and investigation candidates"),
        ("Truthful Coverage", "IMPLEMENTED" if evidence else "FAILED_GATE", "Canonical discovery/execution evidence reconciliation"),
        ("Quality Risk Score", "IMPLEMENTED", "Deterministic remaining-risk index with contributors"),
        ("Release Decision", "IMPLEMENTED", "Fail-closed RELEASE/BLOCK/INSUFFICIENT EVIDENCE"),
        ("V11 Multi-Agent QE Organization", "NOT_IMPLEMENTED", "Separate future distributed orchestration capability"),
        ("Continuous Production Quality Ownership", "NOT_IMPLEMENTED", "Requires persistent CI/CD, telemetry and production integrations"),
    ]
    return {"version": VERSION, "repository_provided": repository["provided"],
            "capabilities": [{"capability": name, "status": status, "basis": basis} for name, status, basis in rows]}


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


async def run(argv=None):
    options = cli(argv)
    core = load_core()
    target = core.plain_url(options.url)
    product = repository_model(options.repo)
    requirements = requirement_model(options.requirements)
    openapi = parse_openapi(options.openapi, target)
    brief = {"provided": False}
    if options.product_brief:
        text = bounded_text(options.product_brief, 250_000)
        brief = {"provided": True, "path": str(options.product_brief.resolve()),
                 "sha256": hashlib.sha256(text.encode()).hexdigest(), "characters": len(text)}
    temporary = None
    requirements_path = options.requirements
    if not requirements_path and openapi["generated_tests"]:
        temporary = tempfile.TemporaryDirectory(prefix="qa-v10-")
        requirements_path = Path(temporary.name) / "generated-requirements.json"
        requirements_path.write_text(json.dumps({"scope": "OpenAPI-derived contract", "scope_approved": False,
                                                 "api_tests": openapi["generated_tests"]}, indent=2))
    core_args = [target, "--headless" if not options.headed else "--headed", "--max-pages", str(options.max_pages),
                 "--max-seconds", str(options.max_seconds), "--max-actions", str(options.max_actions),
                 "--nav-timeout", str(options.nav_timeout), "--dom-timeout", str(options.dom_timeout),
                 "--workflow-timeout", str(options.workflow_timeout),
                 "--journey-timeout", str(options.journey_timeout),
                 "--max-recovery-attempts", str(options.max_recovery_attempts),
                 "--max-replays", str(options.max_replays),
                 "--max-replay-actions", str(options.max_replay_actions),
                 "--max-audit-pages", str(options.max_audit_pages),
                 "--output-dir", str(options.output_dir)]
    if requirements_path:
        core_args += ["--requirements", str(requirements_path)]
    if options.requirements_only:
        core_args.append("--requirements-only")
    if options.repo:
        core_args += ["--source-root", str(options.repo)]
    if options.log_file:
        core_args += ["--log-file", str(options.log_file)]
    if options.allow_form_submission:
        core_args.append("--allow-form-submission")
    if options.mutation_testing:
        core_args += ["--mutation-testing", "--max-mutations", str(options.max_mutations)]
    if options.no_page_audits:
        core_args.append("--no-page-audits")
    config = core.args(core_args)
    agent = core.Agent(config)
    try:
        code = await agent.run()
        truth = agent.truth()
        core_decision = truth["release_decision"]["decision"]
        decision = {"SAFE TO RELEASE": "RELEASE", "NOT SAFE TO RELEASE": "BLOCK"}.get(core_decision, "INSUFFICIENT EVIDENCE")
        root_causes = agent.root_cause_analysis()
        workflow_verdict = agent.workflow_verdict()
        flaky_report = flakiness(agent.workflow_results + agent.api_results)
        defect_report = defects(root_causes, workflow_verdict)
        risk_score = quality_risk(truth, defect_report, flaky_report, requirements)
        capabilities = capability_matrix(agent, requirements, openapi, product, truth)
        behaviors = agent.behavior_plan()
        risks = agent.risk_model()
        journeys = agent.critical_journeys()
        candidates = candidate_requirements(behaviors, risks, journeys)
        model = {"schema": "qa-v10-product-model-v2", "version": VERSION, "run_id": agent.run_id, "target": target,
                 "declared_requirements": requirements, "product_brief": brief, "repository": product, "openapi": openapi,
                 "observed_pages": sorted(agent.pages), "observed_behaviors": behaviors,
                 "observed_api_endpoints": list(agent.observed_api_endpoints.values()),
                 "inference_policy": "Declared, observed, and inferred facts remain separate; missing facts remain UNKNOWN"}
        plan = {"schema": "qa-v10-strategy-v2", "version": VERSION, "run_id": agent.run_id,
                "strategy": strategy(product, requirements, openapi),
                "critical_journeys": journeys, "risk_model": risks,
                "test_generation_policy": "Execute safe observations automatically; require explicit authority for writes and business assertions"}
        verdict = {"schema": "qa-v10-decision-v2", "version": VERSION, "run_id": agent.run_id, "decision": decision,
                   "core_decision": core_decision, "quality_risk": risk_score,
                   "reasons": truth["release_decision"]["reasons"],
                   "requirements_approved": requirements["approved"], "evidence_reconciled": truth["coverage"]["reconciled"],
                   "limitations": ["Undeclared business rules remain UNKNOWN",
                                   "Release applies only to declared approved scope with intact evidence"]}
        artifacts = {"product_model": model, "test_strategy": plan, "candidate_requirements": candidates, "flakiness": flaky_report,
                     "defects": defect_report, "quality_risk": risk_score, "capability_matrix": capabilities,
                     "release_decision": verdict}
        for name, payload in artifacts.items():
            write_json(agent.output_dir / f"qa_v10_{name}.json", payload)
        manifest = {"schema": "qa-v10-manifest-v1", "version": VERSION, "run_id": agent.run_id,
                    "artifacts": {f"qa_v10_{name}.json": hashlib.sha256((agent.output_dir / f"qa_v10_{name}.json").read_bytes()).hexdigest()
                                  for name in artifacts}}
        write_json(agent.output_dir / "qa_v10_manifest.json", manifest)
        executive = (f"# QA Director — V{VERSION}\n\nDecision: **{decision}**\n\n"
                     f"Target: {target}\n\nRun: `{agent.run_id}`\n\n"
                     f"Quality risk index: **{risk_score['score']}/100 ({risk_score['band']})**; confidence: {risk_score['confidence']}.\n\n"
                     f"Evidence reconciled: {truth['coverage']['reconciled']}. Confirmed defects: {defect_report['confirmed_count']}. "
                     f"Investigation candidates: {defect_report['candidate_count']}. Flaky candidates: {flaky_report['flaky_candidates']}.\n\n"
                     "## Decision reasons\n\n" + "\n".join(f"- {reason}" for reason in verdict["reasons"]) + "\n\n"
                     "## Truth boundary\n\nUndeclared requirements and unobserved behavior remain UNKNOWN. "
                     "V11 multi-agent operation and continuous production ownership are not claimed by this V10 run.\n")
        (agent.output_dir / "qa_v10_qa_director.md").write_text(executive, encoding="utf-8")
        print(f"V10 REPORTS | {agent.output_dir}", flush=True)
        print(f"V10 RISK | {risk_score['score']}/100 {risk_score['band']}", flush=True)
        print(f"V10 DECISION | {decision}", flush=True)
        return code
    finally:
        if temporary:
            temporary.cleanup()


def main(argv=None):
    try:
        return asyncio.run(run(argv))
    except (argparse.ArgumentTypeError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"V10 INPUT ERROR | {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
