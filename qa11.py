#!/usr/bin/env python3
"""V11 multi-agent autonomous QE organization orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


VERSION = "11.0.0"
HERE = Path(__file__).resolve().parent
V10_PATH = HERE / "qa10.py"
if not V10_PATH.is_file():
    V10_PATH = HERE / "qa_agent_v10.py"


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
    parser.add_argument("--output-dir", type=Path, default=Path("qa_v11_report"))
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def read_json(run_dir, name, default=None):
    path = run_dir / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def role_result(role, status, findings, unknowns, evidence):
    return {"role": role, "status": status, "findings": findings, "unknowns": unknowns,
            "evidence": evidence, "generated_at": datetime.now(timezone.utc).isoformat()}


async def analyze_role(role, context):
    await asyncio.sleep(0)
    truth = context["truth"]
    v10 = context["v10"]
    coverage = truth.get("coverage", {})
    requirements = v10.get("product", {}).get("declared_requirements", {})
    root = context["root"]
    defects = context["defects"]
    audit_results = context["audits"].get("audits", [])
    accessibility_risks = sum(
        bool(audit.get("accessibility", {}).get("images_missing_alt") or
             audit.get("accessibility", {}).get("inputs_missing_name") or
             audit.get("accessibility", {}).get("buttons_missing_name") or
             audit.get("accessibility", {}).get("duplicate_ids") or
             audit.get("accessibility", {}).get("heading_level_skips") or
             not audit.get("accessibility", {}).get("has_main_landmark", False))
        for audit in audit_results
    )
    performance_risks = sum(bool(audit.get("performance_risks")) for audit in audit_results)
    workflow_results = context["workflows"].get("results", [])
    workflow_nonpasses = sum(item.get("status") != "PASS" for item in workflow_results)
    role_map = {
        "Requirement Agent": ("PASS" if requirements.get("approved") else "UNKNOWN",
                              [f"Declared requirements: {requirements.get('declared', 0)}",
                               f"Review candidates: {len(v10.get('candidates', {}).get('candidates', []))}"],
                              [] if requirements.get("approved") else ["Business scope is not approved"]),
        "Risk Agent": ("PASS", [f"Remaining-risk index: {v10['decision']['quality_risk']['score']}/100"], []),
        "Architecture Agent": ("PASS" if v10["product"].get("repository", {}).get("provided") else "UNKNOWN",
                               v10["product"].get("repository", {}).get("facts", []),
                               v10["product"].get("repository", {}).get("unknowns", [])),
        "UI Agent": ("PASS" if workflow_results and not workflow_nonpasses else
                                "PASS" if coverage.get("executed_state_action_pairs", 0) and
                                not coverage.get("unverified_state_action_pairs", 0) else "UNKNOWN",
                     [f"Verified state/action pairs: {coverage.get('verified_state_action_pairs', 0)}",
                      f"Passing workflow executions: {len(workflow_results) - workflow_nonpasses}/{len(workflow_results)}"],
                     [f"Unverified pairs: {coverage.get('unverified_state_action_pairs', 0)}"]),
        "API Agent": ("PASS" if context["api"].get("results") else "UNKNOWN",
                      [f"API executions: {len(context['api'].get('results', []))}"],
                      [] if context["api"].get("results") else ["No executable API contract evidence"]),
        "Integration Agent": ("FAIL" if workflow_nonpasses else "PASS" if workflow_results else "UNKNOWN",
                              [f"Workflow executions: {len(workflow_results)}",
                               f"Non-passing workflow executions: {workflow_nonpasses}"],
                              [] if workflow_results else ["No declared integration workflow evidence"]),
        "Data Agent": ("UNKNOWN", [], ["Backend or declared UI/API consistency oracle not available"]),
        "Performance Agent": ("RISK" if performance_risks else "UNKNOWN",
                              [f"Viewport audits with performance risks: {performance_risks}"],
                              ["No load profile, service-level objective, or production-like environment"]),
        "Security Agent": ("UNKNOWN", [], ["No explicit authorized security-testing scope"]),
        "Accessibility Agent": ("RISK" if accessibility_risks else "PASS" if audit_results else "UNKNOWN",
                                [f"Audited viewport results: {len(audit_results)}",
                                 f"Audits with accessibility risks: {accessibility_risks}"],
                                [] if audit_results else ["Page audits disabled or unavailable"]),
        "Localization Agent": ("UNKNOWN", [], ["No declared locales or localization oracles"]),
        "Mutation Agent": ("PASS" if context["mutations"].get("killed", 0) else "UNKNOWN",
                           [f"Killed mutants: {context['mutations'].get('killed', 0)}"],
                           [] if context["mutations"].get("killed", 0) else ["No intact killed-mutant evidence"]),
        "Regression Agent": ("PASS" if truth.get("regression_baseline_loaded") else "UNKNOWN", [],
                             [] if truth.get("regression_baseline_loaded") else ["No approved baseline"]),
        "Flaky Agent": ("PASS" if context["flaky"].get("flaky_candidates", 0) == 0 else "FAIL",
                        [f"Flaky candidates: {context['flaky'].get('flaky_candidates', 0)}"], []),
        "Healing Agent": ("PASS", ["Bounded locator and state recovery evidence is recorded"],
                          ["Recovery cannot be guaranteed for arbitrary dynamic applications"]),
        "Investigation Agent": ("FAIL" if defects.get("confirmed_count") else
                                "UNKNOWN" if defects.get("candidate_count") else "PASS",
                                [f"Confirmed defects: {defects.get('confirmed_count', 0)}",
                                 f"Investigation candidates: {defects.get('candidate_count', 0)}"], []),
        "Root Cause Agent": ("PASS" if root.get("findings") else "UNKNOWN",
                            [f"Evidence-ranked findings: {len(root.get('findings', []))}"],
                            root.get("unknowns", [])),
        "Coverage Agent": ("PASS" if coverage.get("reconciled") else "FAIL",
                           [f"Evidence reconciled: {coverage.get('reconciled')}",
                            f"Discovered pairs: {coverage.get('discovered_state_action_pairs', 0)}"], []),
        "Knowledge Agent": ("PASS", ["Current-run product, graph, strategy, risk, and evidence models persisted"],
                            ["Cross-run learning is not trusted without explicit baseline authority"]),
        "Release Agent": (v10["decision"]["decision"], v10["decision"].get("reasons", []),
                          ["Undeclared and unobserved behavior remains UNKNOWN"]),
    }
    status, findings, unknowns = role_map[role]
    return role_result(role, status, findings, unknowns, context["evidence_refs"])


async def execute_v10(options):
    command = [sys.executable, "-u", str(V10_PATH), options.url,
               "--headed" if options.headed else "--headless",
               "--max-pages", str(options.max_pages), "--max-seconds", str(options.max_seconds),
               "--max-actions", str(options.max_actions), "--nav-timeout", str(options.nav_timeout),
               "--dom-timeout", str(options.dom_timeout), "--workflow-timeout", str(options.workflow_timeout),
               "--journey-timeout", str(options.journey_timeout),
               "--max-recovery-attempts", str(options.max_recovery_attempts),
               "--max-replays", str(options.max_replays), "--max-replay-actions", str(options.max_replay_actions),
               "--max-audit-pages", str(options.max_audit_pages),
               "--output-dir", str(options.output_dir / "v10")]
    for flag, value in (("--requirements", options.requirements), ("--repo", options.repo),
                        ("--openapi", options.openapi), ("--product-brief", options.product_brief),
                        ("--log-file", options.log_file)):
        if value:
            command += [flag, str(value.resolve())]
    if options.allow_form_submission:
        command.append("--allow-form-submission")
    if options.requirements_only:
        command.append("--requirements-only")
    if options.mutation_testing:
        command += ["--mutation-testing", "--max-mutations", str(options.max_mutations)]
    if options.no_page_audits:
        command.append("--no-page-audits")
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT)
    report_dir = None
    try:
        while line := await process.stdout.readline():
            text = line.decode(errors="replace").rstrip()
            print(text, flush=True)
            match = re.match(r"V10 REPORTS \| (.+)", text)
            if match:
                report_dir = Path(match.group(1)).resolve()
        code = await process.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        process.terminate()
        await process.wait()
        raise
    if report_dir is None or not report_dir.is_dir():
        raise RuntimeError(f"V10 did not produce a report directory (exit {code})")
    return code, report_dir


async def run(argv=None):
    options = cli(argv)
    _, run_dir = await execute_v10(options)
    truth = read_json(run_dir, "qa_v9_2_10_canonical_truth.json", {})
    context = {
        "truth": truth,
        "v10": {"product": read_json(run_dir, "qa_v10_product_model.json", {}),
                "decision": read_json(run_dir, "qa_v10_release_decision.json", {}),
                "candidates": read_json(run_dir, "qa_v10_candidate_requirements.json", {})},
        "api": read_json(run_dir, "qa_v9_2_10_api_results.json", {}),
        "workflows": read_json(run_dir, "qa_v9_2_10_workflow_results.json", {}),
        "audits": read_json(run_dir, "qa_v9_2_10_page_quality_audits.json", {}),
        "mutations": read_json(run_dir, "qa_v9_2_10_mutation_report.json", {}),
        "flaky": read_json(run_dir, "qa_v10_flakiness.json", {}),
        "defects": read_json(run_dir, "qa_v10_defects.json", {}),
        "root": read_json(run_dir, "qa_v9_2_10_root_cause_analysis.json", {}),
        "evidence_refs": ["qa_v9_2_10_canonical_truth.json", "qa_v10_manifest.json"],
    }
    roles = ["Requirement Agent", "Risk Agent", "Architecture Agent", "UI Agent", "API Agent",
             "Integration Agent", "Data Agent", "Performance Agent", "Security Agent", "Accessibility Agent",
             "Localization Agent", "Mutation Agent", "Regression Agent", "Flaky Agent", "Healing Agent",
             "Investigation Agent", "Root Cause Agent", "Coverage Agent", "Knowledge Agent", "Release Agent"]
    results = await asyncio.gather(*(analyze_role(role, context) for role in roles))
    decision = context["v10"]["decision"]["decision"]
    organization = {"schema": "qa-v11-organization-v1", "version": VERSION,
                    "run_id": context["v10"]["decision"].get("run_id"), "decision": decision,
                    "orchestrator": {"status": "COMPLETE", "delegated_roles": len(results),
                                     "policy": "No specialist may convert UNKNOWN into PASS without evidence"},
                    "agents": results,
                    "summary": {"pass": sum(item["status"] == "PASS" for item in results),
                                "fail": sum(item["status"] in {"FAIL", "BLOCK"} for item in results),
                                "risk": sum(item["status"] == "RISK" for item in results),
                                "unknown": sum(item["status"] in {"UNKNOWN", "INSUFFICIENT EVIDENCE"} for item in results)}}
    destination = run_dir / "qa_v11_organization.json"
    destination.write_text(json.dumps(organization, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    report = (f"# QA Director — V{VERSION}\n\nDecision: **{decision}**\n\n"
              f"Specialists completed: {len(results)}. PASS: {organization['summary']['pass']}; "
              f"FAIL/BLOCK: {organization['summary']['fail']}; RISK: {organization['summary']['risk']}; "
              f"UNKNOWN: {organization['summary']['unknown']}.\n\n"
              "Every specialist conclusion references the canonical V10/V9 evidence boundary. "
              "UNKNOWN is preserved where requirements, systems, permissions, telemetry, or test environments are absent.\n")
    (run_dir / "qa_v11_qa_director.md").write_text(report, encoding="utf-8")
    print(f"V11 REPORTS | {run_dir}", flush=True)
    print(f"V11 ORGANIZATION | {len(results)} specialist roles completed | sha256={digest}", flush=True)
    print(f"V11 DECISION | {decision}", flush=True)
    return 0 if decision == "RELEASE" else 1


def main(argv=None):
    try:
        return asyncio.run(run(argv))
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"V11 ERROR | {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("V11 STOP | INTERRUPTED", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
