#!/usr/bin/env python3
"""V9.2.10: current-run semantic QA and bounded live DOM exploration.

Consolidates the user's V9.2.10 agent into one discovery/execution ledger.
Requires Python 3.10+ and Playwright with Chromium installed.
Run --help for budgets. No previous report, browser profile, or state is read.
Exit codes: 0 = all scoped truth gates pass; 1 = fail-closed/incomplete;
2 = invalid CLI/setup/report failure; 130 = interrupted.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import signal
import shutil
import struct
import sys
import time
import tempfile
import uuid
import zlib
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


VERSION = "9.2.10"
PREFIX = "qa_v9_2_10"
REPORT_DIR = Path(f"{PREFIX}_report")


def now():
    return datetime.now(timezone.utc).isoformat()


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def canon(raw, base=None, drop_query=False):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or value.lower() in {"none", "null", "undefined"}:
        return None
    if re_markdown(value) or any(character.isspace() for character in value):
        return None
    if any(ord(character) < 32 for character in value) or any(character in value for character in "\\<>`"):
        return None
    if value.startswith(("<", "`")) or value.startswith("#") and not value.startswith(("#/", "#!")):
        return None
    try:
        parsed = urlsplit(urljoin(base, value) if base else value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.encode("idna").decode().lower()
        host = f"[{host}]" if ":" in host else host
        port = parsed.port
        if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        path = parsed.path or "/"
        query = "" if drop_query else parsed.query
        fragment = parsed.fragment if parsed.fragment.startswith(("/", "!")) else ""
        return urlunsplit((parsed.scheme.lower(), host, path, query, fragment))
    except (ValueError, UnicodeError):
        return None


def re_markdown(value):
    return "](" in value or value.startswith("[")


def surface(url):
    return canon(url)


def same_origin(first, second):
    left, right = urlsplit(first), urlsplit(second)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def plain_url(value):
    if re_markdown(value.strip()):
        raise argparse.ArgumentTypeError("Use a plain URL, e.g. https://demoqa.com/; Markdown links are rejected.")
    canonical = canon(value)
    if not canonical:
        raise argparse.ArgumentTypeError("Expected an absolute http:// or https:// URL without credentials or whitespace.")
    return canonical


def positive_int(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def positive_float(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a finite positive number")


def png_pixels(path, check=None):
    if check:
        check()
    if Path(path).stat().st_size > 32 * 1024 * 1024:
        raise ValueError("screenshot exceeds the 32 MiB decode budget")
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("screenshot is not PNG")
    offset, width, height, depth, color, interlace, compressed = 8, None, None, None, None, None, bytearray()
    ended = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError("truncated PNG chunk")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        if offset + length + 12 > len(data):
            raise ValueError("truncated PNG payload")
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        checksum = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != checksum:
            raise ValueError("PNG checksum mismatch")
        offset += length + 12
        if kind == b"IHDR":
            if width is not None or length != 13:
                raise ValueError("invalid PNG header")
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            ended = length == 0 and offset == len(data)
            break
    channels = {2: 3, 6: 4}.get(color)
    if not ended or not width or not height or depth != 8 or channels is None or interlace != 0:
        raise ValueError("unsupported PNG format")
    if width * height > 4_194_304:
        raise ValueError("screenshot exceeds the 4 megapixel decode budget")
    stride = width * channels
    expected_bytes = height * (stride + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected_bytes + 1)
    if len(raw) != expected_bytes or not decoder.eof or decoder.unused_data:
        raise ValueError("PNG decompressed size mismatch")
    previous, pixels, cursor = bytearray(stride), bytearray(), 0
    for row_index in range(height):
        if check and row_index % 32 == 0:
            check()
        filter_type, cursor = raw[cursor], cursor + 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 255
            elif filter_type == 2:
                row[index] = (row[index] + above) & 255
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 255
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = abs(estimate - left), abs(estimate - above), abs(estimate - upper_left)
                row[index] = (row[index] + (left if distances[0] <= distances[1] and distances[0] <= distances[2]
                                            else above if distances[1] <= distances[2] else upper_left)) & 255
            elif filter_type != 0:
                raise ValueError("unsupported PNG filter")
        pixels.extend(row)
        previous = row
    return width, height, channels, bytes(pixels)


def png_difference(first, second, check=None):
    left, right = png_pixels(first, check), png_pixels(second, check)
    if left[:3] != right[:3]:
        return 1.0
    channels, changed = left[2], 0
    for offset in range(0, len(left[3]), channels):
        if check and offset % (channels * 4096) == 0:
            check()
        if max(abs(left[3][offset + channel] - right[3][offset + channel]) for channel in range(3)) > 24:
            changed += 1
    return changed / (left[0] * left[1])


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action):
        if action.dest == "headless":
            return action.help
        return super()._get_help_string(action)


def args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=HelpFormatter)
    parser.add_argument("url", type=plain_url)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", dest="headless", action="store_true", default=argparse.SUPPRESS,
                      help="run without a browser window (the default mode)")
    mode.add_argument("--headed", dest="headless", action="store_false", default=argparse.SUPPRESS,
                      help="show the Chromium window")
    parser.set_defaults(headless=True)
    parser.add_argument("--max-pages", type=positive_int, default=5, help="distinct page surfaces, including failed navigation attempts")
    parser.add_argument("--max-seconds", type=positive_float, help="whole exploration deadline; default min(300, max(30, pages*30)); cleanup adds at most 5 seconds")
    parser.add_argument("--max-actions", "--unknown-budget", dest="max_actions", type=positive_int, help="unique exploration attempts; default min(120, pages*8)")
    parser.add_argument("--max-states", type=positive_int, help="unique observed states; default min(60, pages*6)")
    parser.add_argument("--max-depth", type=positive_int, default=3, help="actions in a path from a page entry")
    parser.add_argument("--max-actions-per-state", type=positive_int, default=10)
    parser.add_argument("--max-actions-per-page", type=positive_int, default=4, help="share action time across page surfaces instead of exhausting one page")
    parser.add_argument("--max-replays", type=positive_int, default=12, help="maximum state restoration attempts")
    parser.add_argument("--max-replay-actions", type=positive_int, default=30)
    parser.add_argument("--nav-timeout", type=positive_int, default=10000, help="navigation timeout in milliseconds")
    parser.add_argument("--launch-timeout", type=positive_int, default=30000, help="Chromium startup timeout in milliseconds")
    parser.add_argument("--dom-timeout", type=positive_int, default=3000, help="DOM readiness timeout in milliseconds")
    parser.add_argument("--action-timeout", type=positive_int, default=2000, help="one control operation timeout in milliseconds")
    parser.add_argument("--journey-timeout", type=positive_int, default=10000, help="restoration deadline in milliseconds")
    parser.add_argument("--settle-ms", type=positive_int, default=200, help="short post-action DOM settling period; never networkidle")
    parser.add_argument("--max-dom-elements", type=positive_int, default=600)
    parser.add_argument("--max-links", type=positive_int, default=1000)
    parser.add_argument("--explore-frames", action="store_true", help="include uniquely identified same-origin iframe controls")
    parser.add_argument("--max-frames", type=positive_int, default=5)
    parser.add_argument("--requirements", type=Path, help="JSON workflow assertions; never a saved exploration state")
    parser.add_argument("--requirements-only", action="store_true", help="test only the explicitly declared workflow scope")
    parser.add_argument("--workflow-timeout", type=positive_int, default=10000, help="workflow step and screenshot timeout in milliseconds; global deadline still applies")
    parser.add_argument("--no-auto-workflows", dest="auto_workflows", action="store_false", help="disable generated, non-submitting control workflows")
    parser.add_argument("--max-auto-workflows", type=positive_int, default=3)
    baseline_mode = parser.add_mutually_exclusive_group()
    baseline_mode.add_argument("--baseline", type=Path, help="explicit regression oracle JSON; never used for discovery or replay")
    baseline_mode.add_argument("--write-baseline", type=Path, help="write a candidate regression oracle after the run")
    parser.add_argument("--baseline-approved", action="store_true", help="authorize the supplied/written baseline as a release oracle")
    parser.add_argument("--visual-threshold", type=positive_float, default=0.05, help="maximum changed-pixel ratio against an approved visual baseline")
    parser.add_argument("--max-audit-pages", type=positive_int, default=2)
    parser.add_argument("--no-page-audits", dest="page_audits", action="store_false", help="disable desktop/mobile smoke, visual and accessibility audits")
    parser.add_argument("--max-recovery-attempts", type=positive_int, default=2)
    parser.add_argument("--allow-form-submission", action="store_true", help="explicitly authorize form submission; destructive labels remain blocked")
    parser.add_argument("--allow-api-writes", action="store_true", help="authorize explicitly declared POST/PUT/PATCH/DELETE API tests")
    parser.add_argument("--max-api-requests", type=positive_int, default=20, help="total API test executions, including repeats")
    parser.add_argument("--max-api-response-bytes", type=positive_int, default=1048576, help="maximum API body retained for assertion evaluation")
    parser.add_argument("--mutation-testing", action="store_true", help="inject reversible DOM assertion mutations after passing UI workflows")
    parser.add_argument("--max-mutations", type=positive_int, default=5, help="maximum DOM assertion mutants per run")
    parser.add_argument("--allow-origin", action="append", type=plain_url, default=[], help="additional explicit workflow/SSO origin; discovery remains on the target origin")
    parser.add_argument("--source-root", type=Path, help="application source for explicit source references and isolated mutation tests")
    parser.add_argument("--source-mutation-plan", type=Path, help="JSON command/edits plan; executes the declared test command in temporary copies")
    parser.set_defaults(auto_workflows=True)
    parser.set_defaults(page_audits=True)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR, help="a fresh run subdirectory is always created here")
    parser.add_argument("--log-file", type=Path, help="also copy progress to this file")
    parser.add_argument("--log-stream", choices=("stdout", "stderr"), default="stdout", help="flushed stdout works with tee")
    parser.add_argument("--version", action="version", version=VERSION)
    config = parser.parse_args(argv)
    config.allowed_origins = [config.url, *config.allow_origin]
    config.source_plan = None
    if config.source_root:
        config.source_root = config.source_root.resolve()
        if not config.source_root.is_dir():
            parser.error("--source-root must be a directory")
    if config.source_mutation_plan:
        if not config.source_root:
            parser.error("--source-mutation-plan requires --source-root")
        try:
            plan = json.loads(config.source_mutation_plan.read_text())
            if not isinstance(plan.get("command"), list) or not plan["command"] or not all(isinstance(part, str) and part for part in plan["command"]):
                raise ValueError("command must be a nonempty argv list")
            if not isinstance(plan.get("mutants"), list) or not 1 <= len(plan["mutants"]) <= 10:
                raise ValueError("provide 1 to 10 explicit mutants")
            for mutant in plan["mutants"]:
                if not all(isinstance(mutant.get(key), str) and mutant[key] for key in ("path", "find", "replace")):
                    raise ValueError("each mutant needs path, find, replace")
                candidate = (config.source_root / mutant["path"]).resolve()
                if not candidate.is_relative_to(config.source_root) or not candidate.is_file():
                    raise ValueError("mutant path must be a file inside source-root")
            config.source_plan = plan
        except (OSError, ValueError, AttributeError, TypeError) as error:
            parser.error(f"Invalid source mutation plan: {error}")
    if config.baseline_approved and not (config.baseline or config.write_baseline):
        parser.error("--baseline-approved requires --baseline or --write-baseline")
    if config.visual_threshold > 1:
        parser.error("--visual-threshold must be at most 1")
    if not config.page_audits and (config.baseline or config.write_baseline):
        parser.error("baseline comparison/creation requires page audits")
    config.baseline_data = None
    if config.baseline:
        try:
            baseline = json.loads(config.baseline.read_text(encoding="utf-8"))
            if baseline.get("schema") != "qa-v9.2.10-regression-baseline-v1" or baseline.get("target") != config.url:
                raise ValueError("schema or target mismatch")
            if not isinstance(baseline.get("audits"), list) or not 1 <= len(baseline["audits"]) <= 150:
                raise ValueError("audits must contain 1 to 150 entries")
            baseline_keys = set()
            for audit in baseline["audits"]:
                destination = canon(audit.get("url"))
                key = (destination, audit.get("viewport"))
                if not destination or not same_origin(destination, config.url) or key in baseline_keys:
                    raise ValueError("baseline URLs must be same-origin and URL/viewport pairs unique")
                dimensions = {"mobile": (375, 812), "tablet": (768, 1024), "desktop": (1440, 900)}
                if dimensions.get(audit.get("viewport")) != (audit.get("width"), audit.get("height")):
                    raise ValueError("baseline viewport dimensions are unsupported")
                filename = audit.get("screenshot")
                if not isinstance(filename, str) or Path(filename).name != filename or filename in {"", ".", ".."}:
                    raise ValueError("baseline screenshot must be a local filename")
                for field_name in ("screenshot_sha256", "structure_signature"):
                    if not isinstance(audit.get(field_name), str) or not re.fullmatch(r"[0-9a-f]{64}", audit[field_name]):
                        raise ValueError("baseline signatures must be SHA-256 hashes")
                baseline_keys.add(key)
            config.baseline_data = baseline
        except (OSError, ValueError, TypeError, AttributeError) as error:
            parser.error(f"Invalid baseline: {error}")
    if config.requirements_only and not config.requirements:
        parser.error("--requirements-only requires --requirements")
    config.contract = None
    if config.requirements:
        try:
            contract = json.loads(config.requirements.read_text(encoding="utf-8"))
            if not isinstance(contract, dict) or not isinstance(contract.get("scope"), str) or not contract["scope"].strip():
                raise ValueError("a nonempty scope is required")
            requirements = contract.get("requirements", [])
            tests, api_tests = contract.setdefault("tests", []), contract.setdefault("api_tests", [])
            if not isinstance(requirements, list) or len(requirements) > 50 or not isinstance(tests, list) or not isinstance(api_tests, list):
                raise ValueError("requirements, tests and api_tests must be bounded lists")
            requirement_ids = set()
            for requirement in requirements:
                identifier = requirement.get("id")
                if not isinstance(identifier, str) or not identifier or identifier in requirement_ids or not isinstance(requirement.get("title"), str):
                    raise ValueError("requirements need unique ids and titles")
                if type(requirement.get("critical", False)) is not bool:
                    raise ValueError("requirement critical must be boolean")
                requirement_ids.add(identifier)
                linked = requirement.setdefault("test_ids", [])
                if not isinstance(linked, list) or not all(isinstance(item, str) for item in linked):
                    raise ValueError("requirement test_ids must be a list of strings")
                cases = requirement.get("acceptance_cases", [])
                if not isinstance(cases, list) or len(cases) > 20 or not all(isinstance(case, dict) for case in cases):
                    raise ValueError("acceptance_cases must contain at most 20 structured cases")
                for case_index, case in enumerate([requirement, *cases]):
                    for family, collection in (("ui", tests), ("api", api_tests)):
                        if family in case:
                            if not isinstance(case[family], dict):
                                raise ValueError("requirement ui/api acceptance checks must be objects")
                            test_id = f"requirement:{identifier}:{family}" + (f":case:{case_index}" if case_index else "")
                            collection.append({**case[family], "id": test_id})
                            linked.append(test_id)
            if len(tests) > 50 or len(api_tests) > 50 or not (tests or api_tests or requirements):
                raise ValueError("provide requirements or up to 50 UI and 50 API tests")
            identifiers = set()
            authentication_tests = []
            for test in tests:
                if test.get("authentication"):
                    auth = test["authentication"]
                    authentication_tests.append({**auth, "id": f"authentication:{test['id']}"})
            linked_api_tests = []
            for test in tests:
                if not isinstance(test.get("api_checks", []), list) or len(test.get("api_checks", [])) > 10:
                    raise ValueError("provide at most 10 linked API checks")
                for index, check in enumerate(test.get("api_checks", [])):
                    if check.get("method", "GET") != "GET":
                        raise ValueError("linked API postconditions use GET only")
                    linked_api_tests.append({**check, "id": f"linked:{test['id']}:{index}"})
            for test in tests + authentication_tests:
                if not isinstance(test.get("id"), str) or not test["id"] or test["id"] in identifiers:
                    raise ValueError("workflow ids must be nonempty and unique")
                identifiers.add(test["id"])
                destination = canon(test.get("url"), config.url)
                if not destination or not any(same_origin(destination, origin) for origin in config.allowed_origins):
                    raise ValueError("workflow URL is outside the explicit origins")
                test["url"] = destination
                steps, assertions = test.get("steps", []), test.get("assertions", [])
                if not isinstance(steps, list) or len(steps) > 20 or not isinstance(assertions, list) or not 1 <= len(assertions) <= 20:
                    raise ValueError("each workflow needs 1 to 20 assertions and at most 20 steps")
                for step in steps:
                    if step.get("operation") == "return_to_opener":
                        continue
                    if step.get("operation") not in {"fill", "check", "select", "click", "double_click", "right_click", "click_popup"} or not isinstance(step.get("selector"), str):
                        raise ValueError("unsupported workflow operation or missing selector")
                    if step["operation"] in {"fill", "select"} and not (isinstance(step.get("value"), str) or isinstance(step.get("value_env"), str)):
                        raise ValueError("fill/select steps require a string value")
                    if "fallback_selector" in step and not all(isinstance(step.get(key), str) and step[key].strip()
                                                                for key in ("fallback_selector", "target_tag", "target_name")):
                        raise ValueError("safe fallback needs a selector, target_tag and target_name")
                for assertion in assertions:
                    kind, expected = assertion.get("kind"), assertion.get("expected")
                    if not isinstance(assertion.get("selector"), str) or kind not in {"text_contains", "value", "checked", "visible", "count"}:
                        raise ValueError("unsupported assertion or missing selector")
                    valid = (kind in {"text_contains", "value"} and isinstance(expected, str) or
                             kind in {"checked", "visible"} and type(expected) is bool or
                             kind == "count" and type(expected) is int and expected >= 0)
                    if not valid or kind == "text_contains" and not expected:
                        raise ValueError("assertion expected value has the wrong type")
            for test in api_tests + linked_api_tests:
                identifier = test.get("id")
                if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                    raise ValueError("UI/API test ids must be nonempty and unique")
                identifiers.add(identifier)
                destination = canon(test.get("url"), config.url)
                if not destination or not same_origin(destination, config.url):
                    raise ValueError("API URLs must be same-origin")
                test["url"] = destination
                if test.setdefault("method", "GET") not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
                    raise ValueError("unsupported API method")
                if type(test.get("expected_status")) is not int or not 100 <= test["expected_status"] <= 599:
                    raise ValueError("API expected_status must be an HTTP status code")
                headers = test.get("headers_env", {})
                if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) and key and value
                                                           for key, value in headers.items()):
                    raise ValueError("API headers_env maps header names to environment variable names")
                assertions = test.get("assertions", [])
                if not isinstance(assertions, list) or len(assertions) > 20:
                    raise ValueError("API assertions must contain at most 20 checks")
                for assertion in assertions:
                    if assertion.get("kind") != "json_equals" or "expected" not in assertion or not isinstance(assertion.get("path"), list):
                        raise ValueError("API assertions require json_equals, a JSON key/index path and expected")
                    if not all(type(part) in (str, int) for part in assertion["path"]):
                        raise ValueError("API JSON paths contain only keys or indices")
            if any(identifier not in identifiers for requirement in requirements for identifier in requirement["test_ids"]):
                raise ValueError("requirement refers to an undefined test id")
            critical_tests = {identifier for requirement in requirements if requirement.get("critical") for identifier in requirement["test_ids"]}
            tests.sort(key=lambda test: test["id"] not in critical_tests)
            api_tests.sort(key=lambda test: test["id"] not in critical_tests)
            config.contract = contract
        except (OSError, ValueError, TypeError, AttributeError) as error:
            parser.error(f"Invalid requirements: {error}")
    config.max_seconds = config.max_seconds or min(300, max(30, config.max_pages * 30))
    config.max_actions = config.max_actions or min(120, config.max_pages * 8)
    config.max_states = config.max_states or min(60, config.max_pages * 6)
    return config


DOM_SNAPSHOT = r"""limits => {
    const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
    const roots = [document];
    let scanned = 0, rootScanTruncated = false;
    for (let index = 0; index < roots.length; index++) {
        for (const element of roots[index].querySelectorAll('*')) {
            if (++scanned > 10000) { rootScanTruncated = true; break; }
            if (element.shadowRoot) roots.push(element.shadowRoot);
        }
        if (rootScanTruncated) break;
    }
    const queryAll = selector => roots.flatMap(root => Array.from(root.querySelectorAll(selector)));
    const visible = element => {
        const style = getComputedStyle(element), rect = element.getBoundingClientRect();
        return !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const selectorFor = element => {
        if (element.tagName === 'BUTTON') {
            const text = clean(element.innerText || element.textContent);
            if (text && queryAll('button').filter(candidate =>
                clean(candidate.innerText || candidate.textContent) === text).length === 1) return '';
        }
        for (const attribute of ['id', 'data-testid', 'data-test', 'name', 'aria-label', 'title', 'placeholder']) {
            const value = element.getAttribute(attribute);
            if (value) {
                const selector = element.tagName.toLowerCase() + '[' + attribute + '=' + JSON.stringify(value) + ']';
                if (queryAll(selector).length === 1) return selector;
            }
        }
        if (element.tagName === 'SELECT' && queryAll('select').length === 1) return 'select';
        return '';
    };
    const candidates = queryAll(
        'a[href],button,input:not([type=hidden]),textarea,select,summary,label[for],' +
        '[role=button],[role=checkbox],[role=radio],[role=tab],[role=slider],[role=combobox],' +
        '[role=textbox],[role=menuitem],[role=option],[contenteditable=true],[onclick],' +
        '[tabindex]:not([tabindex="-1"]),.card,.header-wrapper'
    );
    const controls = candidates.slice(0, limits.elements).map(element => {
        const tag = element.tagName.toLowerCase();
        const labelElements = Array.from(element.labels || []);
        const labelledby = clean(element.getAttribute('aria-labelledby')).split(' ').filter(Boolean)
            .map(identifier => element.getRootNode().getElementById(identifier)?.textContent || '').join(' ');
        const text = clean(element.innerText || element.textContent).slice(0, 200);
        const label = clean(element.getAttribute('aria-label') || labelledby ||
            labelElements.map(label => label.textContent).join(' ') ||
            element.getAttribute('placeholder') || element.getAttribute('title') || text ||
            element.getAttribute('name') || element.id || tag).slice(0, 200);
        const labelsVisible = labelElements.some(visible);
        const context = element.closest('form,fieldset,[role=dialog],[role=tabpanel],section');
        const form = element.form || element.closest('form');
        const sensitive = element.type === 'password' || /^(current-password|new-password|one-time-code|cc-)/.test(element.autocomplete || '');
        const options = tag === 'select' ? Array.from(element.options).map(option => ({
            value: option.value, disabled: option.disabled, selected: option.selected
        })) : [];
        return {
            tag, type: (element.type || element.getAttribute('type') || '').toLowerCase(), sensitive,
            role: element.getAttribute('role') || '', label, text,
            selector: selectorFor(element), id: element.id || '', name: element.getAttribute('name') || '',
            shape: tag + Array.from(element.classList).map(name => '.' + CSS.escape(name)).join(''),
            href: element.getAttribute('href') || element.closest('a[href]')?.getAttribute('href') || null,
            navigation_control: !!element.closest('nav,aside,.element-group'),
            in_form: !!form, form_method: (form?.method || '').toLowerCase(), form_action: form?.action || null,
            html_for: element.getAttribute('for') || '',
            visible: visible(element) || labelsVisible,
            disabled: element.matches(':disabled') || element.getAttribute('aria-disabled') === 'true',
            readonly: !!element.readOnly, editable: element.isContentEditable,
            context: context ? (context.id || context.getAttribute('aria-label') || '') : '',
            value: sensitive || element.value == null ? null : String(element.value),
            checked: element.checked ?? element.getAttribute('aria-checked'),
            selected: element.getAttribute('aria-selected'), expanded: element.getAttribute('aria-expanded'),
            pressed: element.getAttribute('aria-pressed'), aria_value: element.getAttribute('aria-valuenow'),
            invalid: element.getAttribute('aria-invalid'), options,
            minimum: element.getAttribute('min'), maximum: element.getAttribute('max'),
            step: element.getAttribute('step'), maxlength: element.getAttribute('maxlength'),
            files: Array.from(element.files || []).map(file => ({name:file.name,size:file.size})),
            pointer: getComputedStyle(element).cursor === 'pointer'
        };
    });
    const anchors = queryAll('a[href]');
    const body = clean([document.body?.innerText, ...roots.slice(1).map(root => root.textContent)].join(' '));
    return {
        url: location.href, title: document.title, text: body.slice(0, 24000), controls,
        links: anchors.slice(0, limits.links).map(element => element.getAttribute('href')),
        truncated: rootScanTruncated || candidates.length > limits.elements || anchors.length > limits.links || body.length > 24000,
        settling: document.getAnimations().some(animation => animation.playState === 'running' &&
            Number.isFinite(animation.effect?.getComputedTiming().endTime)),
        frame_count: queryAll('iframe,frame').length, open_shadow_roots: roots.length - 1,
        ready: !!document.body && (body.length > 0 || controls.length > 0)
    };
}"""


@dataclass
class Action:
    action_id: str
    identity: dict
    semantic: str
    operation: str
    label: str
    selector: str
    destination: str | None
    control: dict
    blocked: str | None = None


@dataclass
class StateModel:
    state_id: str
    url: str
    title: str
    fingerprint: str
    source: str
    timestamp: str
    actions: list[Action] = field(default_factory=list)
    projection: dict = field(default_factory=dict)


class RunStopped(Exception):
    pass


class Agent:
    def __init__(self, config):
        self.config = config
        self.target = config.url
        self.run_id = uuid.uuid4().hex
        self.t0 = time.monotonic()
        self.deadline = self.t0 + config.max_seconds
        self.output_dir = config.output_dir / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.log_handle = None
        if config.log_file:
            config.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_handle = config.log_file.open("a", encoding="utf-8", buffering=1)
        self.evidence_file = (self.output_dir / f"{PREFIX}_evidence.jsonl").open("x", encoding="utf-8", buffering=1)
        self.evidence = []
        self.errors = []
        self.states = {}
        self.actions = {}
        self.pairs = {}
        self.transitions = []
        self.paths = {}
        self.expanded = set()
        self.navigation = {}
        self.pages = {}
        self.page_attempts = set()
        self.queue = deque([self.target])
        self.queued = {surface(self.target)}
        self.in_progress = set()
        self.terminal_controls = set()
        self.verified_radio_controls = set()
        self.stop_reasons = []
        self.limit_reasons = set()
        self.initial_state_id = None
        self.metrics = Counter()
        self.page_action_counts = Counter()
        self.phase = "setup"
        self.interrupted = asyncio.Event()
        self.operations = set()
        self.event_tasks = set()
        self.browser = None
        self.context = None
        self.playwright = None
        self.playwright_manager = None
        self.driver_start_task = None
        self.active_dialogs = []
        self.current_http_errors = []
        self.navigation_blocks = []
        self.root_discovered = False
        self.persistence_ok = True
        self.stream_open = True
        self.workflow_results = []
        self.api_results = []
        self.observed_api_endpoints = {}
        self.mutation_results = []
        self.healing_events = []
        self.source_results = []
        self.workflow_routing = False
        self.expected_popup = False
        self.secret_values = set()
        self.page_audit_results = []
        self.audit_plan = []
        self.audit_scope_truncated = False
        self.regression_results = []
        self.recovery_events = []

    def log(self, message):
        line = f"[{time.monotonic() - self.t0:7.1f}s] {message}"
        if self.stream_open:
            try:
                print(line, file=getattr(sys, self.config.log_stream), flush=True)
            except BrokenPipeError:
                self.stream_open = False
        if self.log_handle:
            self.log_handle.write(line + "\n")

    def ev(self, kind, **details):
        record = {"evidence_id": f"{self.run_id}:{len(self.evidence) + 1}", "run_id": self.run_id,
                  "version": VERSION, "current_run": True, "timestamp": now(), "type": kind, "details": details}
        self.evidence_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.evidence_file.flush()
        self.evidence.append(json.loads(json.dumps(record, ensure_ascii=False)))
        return record["evidence_id"]

    def err(self, phase, error, **details):
        entry = {"phase": phase, "error": str(error)[:10000], **details}
        self.errors.append(entry)
        self.ev("ERROR", **entry)
        self.log(f"{phase.upper()} ERROR | {str(error).splitlines()[0][:220]}")

    def stop(self, reason):
        if reason not in self.stop_reasons:
            self.stop_reasons.append(reason)
            self.log(f"STOP | {reason}")

    def check(self):
        if self.interrupted.is_set():
            self.stop("INTERRUPTED")
            raise RunStopped("INTERRUPTED")
        if time.monotonic() >= self.deadline:
            self.stop("MAX_SECONDS")
            raise RunStopped("MAX_SECONDS")

    def check_exploration(self):
        self.check()
        reserve = min(120, self.config.max_seconds * 0.6)
        if (self.config.auto_workflows or self.config.contract) and self.deadline - time.monotonic() <= reserve:
            self.stop("VERIFICATION_TIME_RESERVED")
            raise RunStopped("VERIFICATION_TIME_RESERVED")

    async def call(self, awaitable):
        try:
            self.check()
        except RunStopped:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            elif isinstance(awaitable, asyncio.Future):
                awaitable.cancel()
            raise
        operation = asyncio.ensure_future(awaitable)
        self.operations.add(operation)
        signal_wait = asyncio.create_task(self.interrupted.wait())
        try:
            self.check()
            done, _ = await asyncio.wait({operation, signal_wait}, timeout=max(0, self.deadline - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            self.check()
            if operation not in done:
                self.stop("MAX_SECONDS")
                raise RunStopped("MAX_SECONDS")
            return operation.result()
        finally:
            signal_wait.cancel()
            await asyncio.gather(signal_wait, return_exceptions=True)
            if operation.done():
                if not operation.cancelled():
                    operation.exception()
                self.operations.discard(operation)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            self.log(f"PROGRESS | {self.phase} | pages={len(self.pages)} states={len(self.states)} "
                     f"actions={self.metrics['execution_attempts']}/{self.config.max_actions} "
                     f"remaining={max(0, self.deadline - time.monotonic()):.0f}s")

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.event_tasks.add(task)
        def finished(completed):
            self.event_tasks.discard(completed)
            if not completed.cancelled() and completed.exception():
                self.err("browser_event", completed.exception())
        task.add_done_callback(finished)

    def on_dialog(self, dialog):
        self.active_dialogs.append({"type": dialog.type, "message": dialog.message[:1000]})
        self.task(dialog.dismiss())

    def on_response(self, response):
        if response.request.resource_type in {"xhr", "fetch"} and same_origin(response.url, self.target):
            key = (response.request.method, response.url)
            if key not in self.observed_api_endpoints and len(self.observed_api_endpoints) < 200:
                item = {"url": response.url, "method": response.request.method, "status": response.status}
                item["evidence_id"] = self.ev("API_SURFACE", **item)
                self.observed_api_endpoints[key] = item
        if response.status >= 400 and response.request.resource_type == "document":
            self.current_http_errors.append({"url": response.url, "status": response.status})

    async def new_page(self):
        page = await self.call(self.context.new_page())
        page.set_default_timeout(self.config.action_timeout)
        page.set_default_navigation_timeout(self.config.nav_timeout)
        page.on("dialog", self.on_dialog)
        page.on("popup", lambda popup: self.task(popup.close()))
        page.on("response", self.on_response)
        return page

    async def navigate(self, page, url, replay=False):
        self.phase = "replay navigation" if replay else "discovery navigation"
        last_error = None
        for attempt in range(self.config.max_recovery_attempts + 1):
            self.log(f"NAV | {url}" + (f" | recovery={attempt}" if attempt else ""))
            self.metrics["navigation_attempts"] += 1
            try:
                response = await self.call(page.goto(url, wait_until="commit", timeout=self.config.nav_timeout))
                final = canon(page.url)
                if not response or response.status >= 400 or not final or not same_origin(final, self.target):
                    raise RuntimeError(f"navigation unverified: status={response.status if response else None}, final={page.url}")
                final_surface = surface(final)
                if final_surface not in self.page_attempts:
                    if len(self.page_attempts) >= self.config.max_pages:
                        self.limit_reasons.add("MAX_PAGES")
                        raise RuntimeError("redirect exceeds the page budget")
                    self.page_attempts.add(final_surface)
                await self.call(page.wait_for_function("() => !!document.body && (document.body.innerText.trim().length > 0 || document.querySelector('input,button,a[href]') || Array.from(document.querySelectorAll('*')).some(element => element.shadowRoot))",
                                                       timeout=self.config.dom_timeout))
                await self.call(asyncio.sleep(self.config.settle_ms / 1000))
                self.ev("NAVIGATION", requested_url=url, final_url=final, status=response.status, replay=replay,
                        recovery_attempt=attempt, outcome="VERIFIED")
                if attempt:
                    event = {"operation": "navigation", "url": url, "attempts": attempt,
                             "outcome": "RECOVERED", "last_error": str(last_error)[:1000]}
                    self.recovery_events.append(event)
                    self.ev("RECOVERY", **event)
                return response.status
            except RunStopped:
                raise
            except Exception as error:
                last_error = error
                if attempt < self.config.max_recovery_attempts:
                    self.metrics["recovery_attempts"] += 1
                    await self.call(asyncio.sleep(min(0.25 * (attempt + 1), 0.75)))
                    continue
        event = {"operation": "navigation", "url": url, "attempts": self.config.max_recovery_attempts,
                 "outcome": "FAILED", "last_error": str(last_error)[:1000]}
        self.recovery_events.append(event)
        self.ev("RECOVERY", **event)
        self.err("navigation", last_error, url=url, replay=replay)
        self.ev("NAVIGATION", requested_url=url, replay=replay, outcome="FAILED")
        return None

    def queue_links(self, raw, url):
        newly_queued = []
        for href in raw:
            destination = canon(href, url)
            if not destination or not same_origin(destination, self.target):
                self.metrics["rejected_links"] += 1
                continue
            key = surface(destination)
            if key == surface(url) or key == surface(self.target):
                self.metrics["self_or_root_navigation_skips"] += 1
                continue
            item = self.navigation.setdefault(key, {"destination": destination, "sources": [], "status": "PENDING"})
            if url not in item["sources"]:
                item["sources"].append(url)
            if key in self.pages:
                item["status"] = "VERIFIED"
            if key not in self.queued and key not in self.page_attempts:
                newly_queued.append(destination)
                self.queued.add(key)
        self.queue.extendleft(reversed(newly_queued))

    def model_actions(self, controls, url):
        actions = {}
        for control in controls:
            tag, kind, role = control["tag"], control["type"], control["role"]
            if control["href"] and canon(control["href"], url):
                continue
            if tag == "a" and control["href"] in {"#", ""} and not role:
                continue
            if tag == "a" and control["href"] and not control["href"].startswith("#"):
                continue
            if tag == "label" and control["html_for"]:
                continue
            if not control["visible"] and kind != "file":
                continue
            if tag in {"div", "span"} and not role and not control["pointer"]:
                continue
            if kind == "file":
                semantic, operation = "FILE_UPLOAD", "upload"
            elif kind == "checkbox" or role == "checkbox":
                semantic, operation = "CHECKBOX", "check"
            elif kind == "radio" or role == "radio":
                semantic, operation = "RADIO", "check"
            elif kind == "range" or role == "slider":
                semantic, operation = "SLIDER", "adjust"
            elif tag == "select":
                semantic, operation = "SELECT", "select"
            elif role == "combobox":
                semantic, operation = "COMBOBOX", "fill" if tag in {"input", "textarea"} else "click"
            elif role == "tab":
                semantic, operation = "TAB", "click"
            elif tag == "input" and kind in {"button", "submit", "reset", "image"}:
                semantic, operation = "BUTTON", "click"
            elif tag == "textarea":
                semantic, operation = "TEXTAREA", "fill"
            elif control["editable"]:
                semantic, operation = "TEXTAREA", "fill"
            elif tag == "input" or role == "textbox":
                semantic, operation = ("NUMBER_INPUT" if kind == "number" else "TEXT_INPUT"), "fill"
            else:
                semantic, operation = "BUTTON", "click"
            if semantic == "BUTTON" and re.search(r"\bdouble[ -]click\b", control["label"], re.I):
                operation = "double_click"
            elif semantic == "BUTTON" and re.search(r"\bright[ -]click\b", control["label"], re.I):
                operation = "right_click"
            locator_identity = ({"selector": control["selector"]} if control["selector"] else
                                {"role": role, "label": control["label"], "context": control["context"]})
            identity = {"surface": surface(url), "semantic": semantic, "operation": operation,
                        "tag": tag, "input_type": kind, "target": locator_identity}
            if control.get("frame_path"):
                identity["frame_path"] = control["frame_path"]
            action_id = sha(identity)
            blocked = "DISABLED" if control["disabled"] else "READONLY" if control["readonly"] else self.action_block_reason(control, operation)
            action = Action(action_id, identity, semantic, operation, control["label"], control["selector"], None, control, blocked)
            if action_id in actions:
                actions[action_id].blocked = "AMBIGUOUS_IDENTITY"
            else:
                actions[action_id] = action
        return sorted(actions.values(), key=lambda action: (action.operation == "click", action.semantic, action.action_id))

    def action_block_reason(self, control, operation):
        if control.get("sensitive") or control.get("type") == "password":
            return "SENSITIVE_CONTROL_REQUIRES_EXPLICIT_WORKFLOW"
        if operation not in {"click", "double_click", "right_click"}:
            return None
        label = control.get("label", "")
        if re.search(r"\b(delete|remove|purchase|buy|pay|publish|transfer|send)\b", label, re.I):
            return "POTENTIALLY_IRREVERSIBLE_ACTION"
        in_form = control.get("in_form", False)
        native_submit = control.get("type") in {"submit", "image"} and (in_form or control.get("tag") == "input")
        if not self.config.allow_form_submission and (native_submit or in_form and re.search(
                r"\b(submit|save|register|sign[ -]?in|log[ -]?in|create|update|continue|next)\b", label, re.I)):
            return "FORM_SUBMISSION_REQUIRES_OPT_IN"
        return None

    async def snapshot(self, page):
        limits = {"elements": self.config.max_dom_elements, "links": self.config.max_links}
        raw = await self.call(page.evaluate(DOM_SNAPSHOT, limits))
        if not self.config.explore_frames:
            return raw
        raw["unexplored_frames"] = 0
        raw["frame_observations"] = []
        pending = deque((frame, []) for frame in page.main_frame.child_frames)
        examined = 0
        while pending:
            frame, parent_path = pending.popleft()
            examined += 1
            if examined > self.config.max_frames:
                raw["unexplored_frames"] += len(pending) + 1
                break
            destination = canon(frame.url)
            if not destination or not same_origin(destination, self.target):
                raw["unexplored_frames"] += 1
                raw["frame_observations"].append({"url": frame.url, "status": "OUT_OF_SCOPE"})
                continue
            try:
                element = await self.call(frame.frame_element())
                try:
                    selector = await self.call(element.evaluate(r'''element => {
                        for (const attribute of ['id', 'name', 'src']) {
                            const value = element.getAttribute(attribute);
                            if (!value) continue;
                            const selector = element.tagName.toLowerCase() + '[' + attribute + '=' + JSON.stringify(value) + ']';
                            if (element.ownerDocument.querySelectorAll(selector).length === 1) return selector;
                        }
                        return null;
                    }'''))
                finally:
                    await self.call(element.dispose())
                if not selector:
                    raise RuntimeError("iframe has no unique stable selector")
                path = parent_path + [{"selector": selector, "url": destination}]
                child = await self.call(frame.evaluate(DOM_SNAPSHOT, limits))
                if not child["ready"] or canon(child["url"]) != destination:
                    raise RuntimeError("iframe changed destination or is not ready")
                raw["frame_observations"].append({"path": path, "text": child["text"], "status": "OBSERVED"})
                for control in child["controls"]:
                    control["frame_path"] = path
                    raw["controls"].append(control)
                raw["settling"] = raw.get("settling", False) or child.get("settling", False)
                raw["truncated"] = raw["truncated"] or child["truncated"]
                if child["links"]:
                    raw["unexplored_frames"] += 1
                pending.extend((nested, path) for nested in frame.child_frames)
            except RunStopped:
                raise
            except Exception as error:
                raw["unexplored_frames"] += 1
                raw["frame_observations"].append({"url": frame.url, "status": "UNAVAILABLE", "error": str(error)[:300]})
        return raw

    async def capture_state(self, page, source="runtime", register=True):
        self.phase = "live DOM discovery"
        raw = await self.snapshot(page)
        if raw.get("settling") or source in {"page_entry", "after_action", "after_replay_step", "after_replay"}:
            settle_deadline = time.monotonic() + self.config.dom_timeout / 1000
            while True:
                previous = sha(raw)
                await self.call(asyncio.sleep(min(0.1, self.config.settle_ms / 1000)))
                raw = await self.snapshot(page)
                if not raw.get("settling") and sha(raw) == previous:
                    break
                if time.monotonic() >= settle_deadline:
                    raise RuntimeError("DOM_STABILITY_TIMEOUT: no settled state was observed")
        url = canon(raw["url"])
        if not raw["ready"] or not url or not same_origin(url, self.target):
            raise RuntimeError(f"no usable in-scope state: {raw['url']}")
        actions = self.model_actions(raw["controls"], url)
        controls = [{"identity": action.identity, "value": action.control["value"],
                     "checked": action.control["checked"], "selected": action.control["selected"],
                     "expanded": action.control["expanded"], "disabled": action.control["disabled"],
                     "pressed": action.control.get("pressed"), "aria_value": action.control.get("aria_value"),
                     "invalid": action.control["invalid"], "files": action.control["files"],
                     "options": action.control["options"], "blocked": action.blocked}
                    for action in actions]
        projection = {"url": url, "title": raw["title"], "text": raw["text"], "controls": controls}
        if raw.get("frame_observations"):
            projection["frames"] = raw["frame_observations"]
        fingerprint = sha(projection)
        state = StateModel(fingerprint, url, raw["title"], fingerprint, source, now(), actions, projection)
        self.metrics["state_captures"] += 1
        if not register:
            return state
        if surface(url) not in self.page_attempts:
            if len(self.page_attempts) >= self.config.max_pages:
                self.stop("MAX_PAGES")
                raise RunStopped("MAX_PAGES")
            self.page_attempts.add(surface(url))
        if state.state_id not in self.states:
            if len(self.states) >= self.config.max_states:
                self.stop("MAX_STATES")
                raise RunStopped("MAX_STATES")
            self.states[state.state_id] = state
            self.ev("STATE_DISCOVERY", state_id=state.state_id, url=url, projection=projection,
                    truncated=raw["truncated"], frame_count=raw["frame_count"],
                    open_shadow_roots=raw.get("open_shadow_roots", 0))
        if raw["truncated"]:
            self.limit_reasons.add("DOM_SNAPSHOT_TRUNCATED")
        if raw.get("unexplored_frames", raw["frame_count"]):
            self.limit_reasons.add("FRAMES_NOT_EXPLORED")
        self.queue_links(raw["links"], url)
        for action in actions:
            self.actions.setdefault(action.action_id, action)
            pair_key = (state.state_id, action.action_id)
            if pair_key not in self.pairs:
                evidence_id = self.ev("STATE_ACTION_DISCOVERY", state_id=state.state_id, action_id=action.action_id,
                                      identity=action.identity, label=action.label, blocked=action.blocked)
                self.pairs[pair_key] = {"state_id": state.state_id, "action_id": action.action_id,
                                        "status": "BLOCKED" if action.blocked else "PENDING", "reason": action.blocked,
                                        "attempted": False, "discovery_evidence_id": evidence_id, "execution_evidence_id": None}
        self.pages.setdefault(surface(url), {"url": url, "title": raw["title"], "state_ids": []})
        if state.state_id not in self.pages[surface(url)]["state_ids"]:
            self.pages[surface(url)]["state_ids"].append(state.state_id)
        return state

    async def resolve(self, page, action):
        page = self.control_scope(page, action)
        if action.selector:
            locator = page.locator(action.selector)
        elif action.control["role"]:
            locator = page.get_by_role(action.control["role"], name=action.label, exact=True)
        elif action.control["text"]:
            text_pattern = r"^\s*" + r"\s+".join(re.escape(word) for word in action.control["text"].split()) + r"\s*$"
            locator = page.locator(action.control.get("shape") or action.control["tag"]).filter(has_text=re.compile(text_pattern))
        else:
            locator = page.get_by_label(action.label, exact=True)
        count = await self.call(locator.count())
        if count != 1:
            raise RuntimeError(f"exact discovered control must resolve once, found {count}: {action.label}")
        if not await self.call(locator.is_enabled(timeout=self.config.dom_timeout)):
            raise RuntimeError(f"control is disabled: {action.label}")
        return locator

    def control_scope(self, page, action):
        for frame in action.control.get("frame_path", []):
            page = page.frame_locator(frame["selector"])
        return page

    def probe(self, action):
        control = action.control
        kind = control["type"]
        value = {"email": "qa-v9-2-10@example.com", "url": "https://example.com/", "tel": "5550100123",
                 "date": "2026-09-10", "datetime-local": "2026-09-10T12:00", "month": "2026-09",
                 "week": "2026-W37", "time": "12:00", "color": "#336699"}.get(kind, "QA_V9_2_10_PROBE")
        if action.semantic == "COMBOBOX":
            value = "A"
        if "datepicker" in control["id"].lower() and kind not in {"date", "datetime-local"}:
            value = "09/10/2026"
        if kind == "number":
            lower = float(control["minimum"]) if control["minimum"] else 0
            upper = float(control["maximum"]) if control["maximum"] else max(42, lower)
            value = str(min(max(42, lower), upper))
        if control["maxlength"] is not None and int(control["maxlength"]) >= 0:
            value = value[:int(control["maxlength"])]
        return value

    async def execute_control(self, page, locator, action):
        control = action.control
        if action.operation == "fill":
            expected = self.probe(action)
            await self.call(locator.fill(expected))
            observed = await self.call(locator.inner_text() if control["editable"] else locator.input_value())
            return {"verified": observed == expected, "expected": expected, "observed": observed}
        if action.operation == "upload":
            filename = f"{PREFIX}_probe.txt"
            await self.call(locator.set_input_files({"name": filename, "mimeType": "text/plain", "buffer": b"V9.2.10 current-run QA probe\n"}))
            files = await self.call(locator.evaluate("element => Array.from(element.files || []).map(file => ({name:file.name,size:file.size}))"))
            return {"verified": len(files) == 1 and files[0]["name"] == filename, "files": files}
        if action.operation == "check":
            if control["tag"] == "input":
                if await self.call(locator.is_visible()):
                    await self.call(locator.check())
                elif control["id"]:
                    label = self.control_scope(page, action).locator("label[for=" + json.dumps(control["id"]) + "]")
                    if await self.call(label.count()) != 1:
                        raise RuntimeError("hidden checkbox has no unique visible label")
                    if not await self.call(locator.is_checked()):
                        await self.call(label.click())
                checked = await self.call(locator.is_checked())
            else:
                if control["checked"] != "true":
                    await self.call(locator.click())
                checked = await self.call(locator.get_attribute("aria-checked")) == "true"
            return {"verified": checked, "checked": checked}
        if action.operation == "select":
            options = [option for option in control["options"] if not option["disabled"]]
            choice = next((option for option in options if not option["selected"]), next(iter(options), None))
            if choice is None:
                raise RuntimeError("select has no enabled options")
            await self.call(locator.select_option(choice["value"]))
            observed = await self.call(locator.input_value())
            return {"verified": observed == choice["value"], "expected": choice["value"], "observed": observed}
        if action.operation == "adjust":
            before = control["value"] if control["tag"] == "input" else await self.call(locator.get_attribute("aria-valuenow"))
            maximum = control["maximum"] or await self.call(locator.get_attribute("aria-valuemax"))
            key = "ArrowLeft" if maximum is not None and before is not None and float(before) >= float(maximum) else "ArrowRight"
            await self.call(locator.press(key))
            observed = await self.call(locator.input_value() if control["tag"] == "input" else locator.get_attribute("aria-valuenow"))
            return {"verified": observed is not None and observed != before, "before": before, "observed": observed}
        if action.operation == "double_click":
            await self.call(locator.dblclick())
        elif action.operation == "right_click":
            await self.call(locator.click(button="right"))
        else:
            await self.call(locator.click())
        if action.semantic == "TAB":
            selected = await self.call(locator.get_attribute("aria-selected"))
            return {"verified": selected == "true", "aria_selected": selected}
        return {"verified": None}

    def control_key(self, action):
        fields = ("value", "checked", "selected", "expanded", "disabled", "invalid", "options")
        return action.action_id, sha({name: action.control[name] for name in fields})

    def block_pair(self, key, reason):
        if key in self.pairs and self.pairs[key]["status"] == "PENDING":
            self.pairs[key].update(status="BLOCKED", reason=reason)
            self.ev("EXECUTION_SKIPPED", state_id=key[0], action_id=key[1], reason=reason)

    async def execute_pair(self, page, expected_state, action):
        key = (expected_state.state_id, action.action_id)
        pair = self.pairs[key]
        if key in self.in_progress or pair["status"] != "PENDING":
            self.metrics["duplicate_pair_skips"] += 1
            return None
        if action.semantic == "RADIO" and self.control_key(action) in self.verified_radio_controls:
            self.block_pair(key, "RADIO_LOCAL_TRANSITION_ALREADY_VERIFIED")
            self.metrics["redundant_radio_skips"] += 1
            return None
        if self.control_key(action) in self.terminal_controls:
            self.block_pair(key, "FAILED_OR_UNCHANGED_CONTROL_ALREADY_ATTEMPTED")
            self.metrics["terminal_control_skips"] += 1
            return None
        try:
            locator = await self.resolve(page, action)
        except RunStopped:
            raise
        except Exception as error:
            self.block_pair(key, "UNRESOLVABLE")
            self.terminal_controls.add(self.control_key(action))
            self.err("resolution", error, state_id=key[0], action_id=key[1])
            return None
        before_state = await self.capture_state(page, "immediately_before_action")
        if before_state.state_id != expected_state.state_id:
            self.block_pair(key, "STATE_DRIFT_AFTER_REPLAY")
            self.metrics["state_drift_skips"] += 1
            return before_state
        self.check()
        if self.metrics["execution_attempts"] >= self.config.max_actions:
            self.stop("MAX_ACTIONS")
            raise RunStopped("MAX_ACTIONS")
        page_key = surface(before_state.url)
        if self.page_action_counts[page_key] >= self.config.max_actions_per_page:
            self.limit_reasons.add("MAX_ACTIONS_PER_PAGE")
            return None
        if len(self.states) >= self.config.max_states:
            self.stop("MAX_STATES")
            raise RunStopped("MAX_STATES")
        self.in_progress.add(key)
        self.metrics["guard_acquires"] += 1
        pair.update(status="IN_PROGRESS", attempted=True)
        self.metrics["execution_attempts"] += 1
        self.page_action_counts[page_key] += 1
        outcome, detail, after_state = "INCOMPLETE", {}, None
        try:
            self.active_dialogs.clear()
            self.current_http_errors.clear()
            self.navigation_blocks.clear()
            self.phase = "action execution"
            self.log(f"ACTION {self.metrics['execution_attempts']}/{self.config.max_actions} | {action.semantic} | {action.label[:85]}")
            pair["start_evidence_id"] = self.ev("EXECUTION_STARTED", state_id=key[0], action_id=key[1], identity=action.identity)
            detail = await self.execute_control(page, locator, action)
            await self.call(asyncio.sleep(self.config.settle_ms / 1000))
            after_state = await self.capture_state(page, "after_action")
            changed = before_state.state_id != after_state.state_id
            detail.update(changed=changed, dialogs=list(self.active_dialogs), http_errors=list(self.current_http_errors),
                          blocked_navigation=list(self.navigation_blocks))
            verified = detail.get("verified")
            detail["observation_strength"] = "DIRECT_POSTCONDITION" if verified is not None else "STATE_CHANGE_OR_DIALOG_ONLY"
            if self.current_http_errors or self.navigation_blocks:
                outcome = "FAILED"
            elif not changed and not self.active_dialogs:
                outcome = "UNCHANGED"
            elif verified is False:
                outcome = "FAILED"
            elif verified is True or changed or self.active_dialogs:
                outcome = "VERIFIED"
            if outcome == "VERIFIED" and surface(after_state.url) in self.navigation:
                self.navigation[surface(after_state.url)].update(status="VERIFIED", final_url=after_state.url)
            self.transitions.append({"transition_id": sha([key, after_state.state_id]), "from_state": key[0],
                                     "action_id": key[1], "to_state": after_state.state_id, "status": outcome,
                                     "classification": "state_change" if changed else "self_loop", "timestamp": now()})
            return after_state
        except RunStopped as error:
            detail["stop_reason"] = str(error)
            raise
        except asyncio.CancelledError:
            self.stop("INTERRUPTED")
            detail["stop_reason"] = "INTERRUPTED"
            raise
        except Exception as error:
            outcome = "FAILED"
            detail["error"] = str(error)[:2000]
            self.err("action", error, state_id=key[0], action_id=key[1])
            return None
        finally:
            self.in_progress.remove(key)
            self.metrics["guard_releases"] += 1
            pair.update(status=outcome, detail=detail, after_state_id=after_state.state_id if after_state else None)
            if outcome in {"FAILED", "UNCHANGED"}:
                self.terminal_controls.add(self.control_key(action))
            if outcome == "VERIFIED" and action.semantic == "RADIO":
                self.verified_radio_controls.add(self.control_key(action))
            pair["execution_evidence_id"] = self.ev("EXECUTION_FINISHED", state_id=key[0], action_id=key[1],
                                                    identity=action.identity, outcome=outcome,
                                                    after_state_id=pair["after_state_id"], observation=detail)
            self.log(f"RESULT | {outcome} | {action.label[:85]}")

    async def replay(self, page, target_state, root_url, steps):
        live = await self.capture_state(page, "replay_precheck", register=False)
        if live.state_id == target_state.state_id:
            self.metrics["replays_avoided"] += 1
            return True
        if self.metrics["replay_attempts"] >= self.config.max_replays:
            self.stop("MAX_REPLAYS")
            raise RunStopped("MAX_REPLAYS")
        self.metrics["replay_attempts"] += 1
        replay_deadline = time.monotonic() + self.config.journey_timeout / 1000
        try:
            if await self.navigate(page, root_url, replay=True) is None:
                return False
            for step in steps:
                self.check()
                if time.monotonic() >= replay_deadline:
                    raise RuntimeError("JOURNEY_TIMEOUT")
                if self.metrics["replay_actions"] >= self.config.max_replay_actions:
                    self.stop("MAX_REPLAY_ACTIONS")
                    raise RunStopped("MAX_REPLAY_ACTIONS")
                actual = await self.capture_state(page, "before_replay_step", register=False)
                if actual.state_id != step["from_state"]:
                    raise RuntimeError("replay source state diverged")
                original_pair = self.pairs[(step["from_state"], step["action_id"])]
                if original_pair["status"] != "VERIFIED":
                    raise RuntimeError("only verified current-run steps may be replayed")
                action = next((item for item in actual.actions if item.action_id == step["action_id"]), None)
                if not action:
                    raise RuntimeError("replay action is absent from current DOM")
                locator = await self.resolve(page, action)
                before = await self.capture_state(page, "immediately_before_replay", register=False)
                if before.state_id != step["from_state"]:
                    raise RuntimeError("replay state drifted before execution")
                self.metrics["replay_actions"] += 1
                await self.execute_control(page, locator, action)
                await self.call(asyncio.sleep(self.config.settle_ms / 1000))
                after = await self.capture_state(page, "after_replay_step", register=False)
                self.ev("REPLAY_STEP", state_id=before.state_id, action_id=action.action_id,
                        expected_state=step["to_state"], observed_state=after.state_id)
                if after.state_id != step["to_state"]:
                    raise RuntimeError("replay destination state diverged")
            live = await self.capture_state(page, "after_replay", register=False)
            if live.state_id != target_state.state_id:
                raise RuntimeError("replay did not reach the requested state")
            return True
        except RunStopped:
            raise
        except Exception as error:
            self.metrics["replay_failures"] += 1
            self.err("replay", error, target_state=target_state.state_id)
            return False

    async def explore(self, page, state, root_url, steps):
        self.check_exploration()
        self.paths.setdefault(state.state_id, {"entry_url": root_url, "steps": list(steps)})
        if state.state_id in self.expanded:
            self.metrics["duplicate_state_skips"] += 1
            return
        self.expanded.add(state.state_id)
        self.metrics["max_depth_reached"] = max(self.metrics["max_depth_reached"], len(steps))
        self.log(f"EXPLORE | depth={len(steps)} state={state.state_id[:12]} actions={len(state.actions)}")
        eligible = [action for action in state.actions if self.pairs[(state.state_id, action.action_id)]["status"] == "PENDING"]
        if len(steps) >= self.config.max_depth:
            if eligible:
                self.limit_reasons.add("MAX_DEPTH")
            return
        if len(eligible) > self.config.max_actions_per_state:
            self.limit_reasons.add("MAX_ACTIONS_PER_STATE")
        for action in eligible[:self.config.max_actions_per_state]:
            self.check_exploration()
            key = (state.state_id, action.action_id)
            if self.pairs[key]["status"] != "PENDING":
                continue
            if action.semantic == "RADIO" and self.control_key(action) in self.verified_radio_controls:
                self.block_pair(key, "RADIO_LOCAL_TRANSITION_ALREADY_VERIFIED")
                self.metrics["redundant_radio_skips"] += 1
                continue
            if self.page_action_counts[surface(state.url)] >= self.config.max_actions_per_page:
                self.limit_reasons.add("MAX_ACTIONS_PER_PAGE")
                return
            if action.control.get("navigation_control") and self.queue:
                self.pairs[key]["reason"] = "DEFERRED_NAVIGATION_CONTROL"
                self.metrics["deferred_navigation_controls"] += 1
                continue
            satisfied = (action.operation == "fill" and action.control["value"] == self.probe(action) or
                         action.operation == "check" and action.control["checked"] in (True, "true"))
            if satisfied:
                self.block_pair(key, "POSTCONDITION_ALREADY_SATISFIED")
                self.metrics["satisfied_control_skips"] += 1
                continue
            if self.control_key(action) in self.terminal_controls:
                self.block_pair(key, "FAILED_OR_UNCHANGED_CONTROL_ALREADY_ATTEMPTED")
                self.metrics["terminal_control_skips"] += 1
                continue
            if self.metrics["execution_attempts"] >= self.config.max_actions:
                self.stop("MAX_ACTIONS")
                raise RunStopped("MAX_ACTIONS")
            if not await self.replay(page, state, root_url, steps):
                for remaining in eligible:
                    self.block_pair((state.state_id, remaining.action_id), "STATE_UNREACHABLE_AFTER_REPLAY")
                return
            after = await self.execute_pair(page, state, action)
            if after and after.state_id != state.state_id:
                step = {"from_state": state.state_id, "action_id": action.action_id, "to_state": after.state_id}
                if self.pairs[key]["status"] == "VERIFIED":
                    await self.explore(page, after, root_url, steps + [step])

    async def route_request(self, route):
        try:
            await self.route_navigation(route)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.err("route", error)
            try:
                await route.abort("blockedbyclient")
            except Exception as abort_error:
                self.err("route_abort", abort_error)

    async def route_navigation(self, route):
        request = route.request
        top_level = False
        if request.is_navigation_request() and request.resource_type == "document":
            try:
                top_level = request.frame.parent_frame is None
            except Exception:
                if self.workflow_routing and self.expected_popup:
                    top_level = True
                    self.ev("NAVIGATION_FRAME_PENDING", url=request.url,
                            handling="EXPECTED_POPUP_APPLY_TOP_LEVEL_ORIGIN_AND_PAGE_BUDGET_GUARDS")
                else:
                    self.navigation_blocks.append({"url": request.url, "reason": "FRAME_UNAVAILABLE"})
                    self.ev("NAVIGATION_BLOCKED", url=request.url, reason="FRAME_UNAVAILABLE")
                    await route.abort("blockedbyclient")
                    return
        if top_level:
            destination = canon(request.url)
            allowed = any(same_origin(destination, origin) for origin in self.config.allowed_origins) if destination and self.workflow_routing else destination and same_origin(destination, self.target)
            if not allowed:
                self.navigation_blocks.append({"url": request.url, "reason": "OUT_OF_ORIGIN"})
                self.ev("NAVIGATION_BLOCKED", url=request.url, reason="OUT_OF_ORIGIN")
                await route.abort("blockedbyclient")
                return
            key = surface(destination)
            transport_known = any(urlsplit(item)._replace(fragment="").geturl() == key for item in self.page_attempts)
            if not transport_known and key not in self.page_attempts and len(self.page_attempts) >= self.config.max_pages:
                self.limit_reasons.add("MAX_PAGES")
                self.navigation_blocks.append({"url": request.url, "reason": "MAX_PAGES"})
                self.ev("NAVIGATION_BLOCKED", url=request.url, reason="MAX_PAGES")
                await route.abort("blockedbyclient")
                return
            if not transport_known:
                self.page_attempts.add(key)
        await route.continue_()

    async def pipeline(self):
        while self.queue:
            self.check_exploration()
            url = self.queue.popleft()
            key = surface(url)
            self.queued.discard(key)
            if key in self.pages:
                self.metrics["duplicate_navigation_skips"] += 1
                continue
            if key in self.page_attempts:
                continue
            if len(self.page_attempts) >= self.config.max_pages:
                self.queue.appendleft(url)
                self.stop("MAX_PAGES")
                return
            self.page_attempts.add(key)
            page = await self.new_page()
            try:
                status = await self.navigate(page, url)
                if status is None:
                    if key in self.navigation:
                        self.navigation[key]["status"] = "FAILED"
                    continue
                state = await self.capture_state(page, "page_entry")
                if self.initial_state_id is None and key == surface(self.target):
                    self.initial_state_id = state.state_id
                    self.root_discovered = True
                if key in self.navigation:
                    self.navigation[key].update(status="VERIFIED", final_url=state.url)
                self.pages[surface(state.url)]["http_status"] = status
                await self.explore(page, state, state.url, [])
            except RunStopped:
                raise
            except Exception as error:
                self.err("page", error, url=url)
            finally:
                if not self.interrupted.is_set() and time.monotonic() < self.deadline:
                    await self.call(page.close())
        if self.limit_reasons:
            self.stop("LOCAL_LIMITS_REACHED")
        elif any(pair["status"] in {"PENDING", "BLOCKED"} for pair in self.pairs.values()):
            self.stop("FRONTIER_BLOCKED")
        else:
            self.stop("SCOPED_FRONTIER_EXHAUSTED")

    def generate_workflows(self):
        if self.config.contract or not self.config.auto_workflows:
            return
        tests = []
        seen = set()
        for state in self.states.values():
            if state.source != "page_entry" or surface(state.url) in seen:
                continue
            seen.add(surface(state.url))
            steps, assertions, source_actions, radio_groups = [], [], [], set()
            for action in state.actions:
                control = action.control
                if action.blocked or not action.selector or control.get("frame_path") or control["editable"]:
                    continue
                if action.operation == "fill" and control["tag"] in {"input", "textarea"} and control["type"] not in {"password", "hidden", "file"}:
                    value = self.probe(action)
                    steps.append({"operation": "fill", "selector": action.selector, "value": value})
                    assertions.append({"kind": "value", "selector": action.selector, "expected": value})
                elif action.operation == "check" and action.semantic == "CHECKBOX" and control["tag"] == "input":
                    steps.append({"operation": "check", "selector": action.selector})
                    assertions.append({"kind": "checked", "selector": action.selector, "expected": True})
                elif action.operation == "select" and control["tag"] == "select":
                    choice = next((option for option in control["options"] if not option["disabled"] and not option["selected"]), None)
                    if not choice:
                        continue
                    steps.append({"operation": "select", "selector": action.selector, "value": choice["value"]})
                    assertions.append({"kind": "value", "selector": action.selector, "expected": choice["value"]})
                elif action.operation == "check" and action.semantic == "RADIO" and control["tag"] == "input":
                    group = control.get("name") or control.get("context") or action.identity["surface"] + ":anonymous-radio"
                    if group in radio_groups:
                        continue
                    radio_groups.add(group)
                    steps.append({"operation": "check", "selector": action.selector})
                    assertions.append({"kind": "checked", "selector": action.selector, "expected": True})
                else:
                    continue
                source_actions.append(action.action_id)
                if len(steps) == 4:
                    break
            if steps:
                tests.append({"id": "observed-controls-" + sha(state.url)[:12], "url": state.url,
                              "steps": steps, "assertions": assertions, "source_state_id": state.state_id,
                              "source_action_ids": source_actions})
            if len(tests) >= self.config.max_auto_workflows:
                break
        if tests:
            self.config.contract = {"scope": "Automatically inferred control postconditions only; no business acceptance or submission coverage",
                                    "scope_approved": False, "origin": "CURRENT_RUN_DOM", "tests": tests}
            self.log(f"AUTO WORKFLOWS | {len(tests)} generated from current-run entry states; no automatic scope approval")

    async def audit_pages(self):
        if not self.config.page_audits or not self.pages and not self.config.contract:
            return
        viewports = ((375, 812, "mobile"), (768, 1024, "tablet"), (1440, 900, "desktop"))
        baseline_by_key = {(item["url"], item["viewport"]): item for item in
                           (self.config.baseline_data or {}).get("audits", [])}
        page_urls = []
        if self.config.contract:
            page_urls.extend(test["url"] for test in self.config.contract["tests"])
        ranked_pages = sorted((item["url"] for item in self.pages.values()), key=lambda url: (
            -sum(action.operation in {"fill", "select", "check", "upload"} for action in self.actions.values()
                 if action.identity["surface"] == surface(url)), url))
        page_urls.extend(ranked_pages)
        page_urls.extend(item["url"] for item in (self.config.baseline_data or {}).get("audits", []))
        page_urls = list(dict.fromkeys(page_urls))
        self.audit_scope_truncated = len(page_urls) > self.config.max_audit_pages
        page_urls = page_urls[:self.config.max_audit_pages]
        self.audit_plan = [(url, name) for url in page_urls for _, _, name in viewports]
        for url in page_urls:
            for width, height, viewport_name in viewports:
                self.check()
                self.phase = "page quality audit"
                result = {"url": url, "viewport": viewport_name, "width": width, "height": height,
                          "status": "INCOMPLETE", "console_errors": [], "page_errors": [],
                          "network_failures": [], "http_errors": []}
                context = page = None
                try:
                    context = await self.call(self.browser.new_context(viewport={"width": width, "height": height},
                                                                       service_workers="block", accept_downloads=False))
                    await self.call(context.route("**/*", self.route_request))
                    await self.call(context.add_init_script(r'''(() => {
                        window.__qaVitals = {cls: 0, lcp: null};
                        try { new PerformanceObserver(list => { for (const entry of list.getEntries()) window.__qaVitals.lcp = entry.startTime; }).observe({type:'largest-contentful-paint', buffered:true}); } catch (_) {}
                        try { new PerformanceObserver(list => { for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__qaVitals.cls += entry.value; }).observe({type:'layout-shift', buffered:true}); } catch (_) {}
                    })()'''))
                    page = await self.call(context.new_page())
                    page.set_default_timeout(self.config.workflow_timeout)
                    page.set_default_navigation_timeout(self.config.nav_timeout)
                    page.on("console", lambda message, bucket=result["console_errors"]:
                            bucket.append(message.text[:1000]) if message.type == "error" else None)
                    page.on("pageerror", lambda error, bucket=result["page_errors"]: bucket.append(str(error)[:1000]))
                    page.on("requestfailed", lambda request, bucket=result["network_failures"]: bucket.append(
                        {"url": request.url, "failure": request.failure}) if same_origin(request.url, self.target) else None)
                    page.on("response", lambda response, bucket=result["http_errors"]: bucket.append(
                        {"url": response.url, "status": response.status}) if response.status >= 400 and same_origin(response.url, self.target) else None)
                    response = None
                    last_error = None
                    for attempt in range(self.config.max_recovery_attempts + 1):
                        try:
                            response = await self.call(page.goto(url, wait_until="domcontentloaded", timeout=self.config.nav_timeout))
                            if response and response.status < 400:
                                break
                            raise RuntimeError(f"audit navigation status {response.status if response else None}")
                        except RunStopped:
                            raise
                        except Exception as error:
                            last_error = error
                            if attempt == self.config.max_recovery_attempts:
                                raise
                            self.metrics["audit_recovery_attempts"] += 1
                            await self.call(asyncio.sleep(0.25 * (attempt + 1)))
                    await self.call(page.wait_for_function("() => !!document.body", timeout=self.config.dom_timeout))
                    await self.call(asyncio.sleep(self.config.settle_ms / 1000))
                    observation = await self.call(page.evaluate(r'''() => {
                        const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                        const visible = element => { const r=element.getBoundingClientRect(), s=getComputedStyle(element); return !!(r.width&&r.height)&&s.display!=='none'&&s.visibility!=='hidden'; };
                        const named = element => clean(element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText || element.value);
                        const ids = Array.from(document.querySelectorAll('[id]')).map(element => element.id);
                        const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(visible).map(element => Number(element.tagName[1]));
                        return {
                            title: document.title, text: clean(document.body.innerText).slice(0,24000),
                            structure: Array.from(document.querySelectorAll('main,nav,form,button,input,textarea,select,a[href],h1,h2,h3')).map(element =>
                                [element.tagName.toLowerCase(), element.getAttribute('role') || '', element.getAttribute('type') || '']).slice(0,2000),
                            accessibility: {
                                images_missing_alt: Array.from(document.images).filter(image => !image.hasAttribute('alt')).length,
                                inputs_missing_name: Array.from(document.querySelectorAll('input:not([type=hidden]),textarea,select')).filter(element =>
                                    !element.labels?.length && !element.getAttribute('aria-label') && !element.getAttribute('aria-labelledby') && !element.getAttribute('title')).length,
                                buttons_missing_name: Array.from(document.querySelectorAll('button,[role=button]')).filter(element => !named(element)).length,
                                duplicate_ids: ids.filter((id,index) => ids.indexOf(id) !== index).filter((id,index,array) => array.indexOf(id) === index),
                                heading_level_skips: headings.filter((level,index) => index && level > headings[index-1] + 1).length,
                                has_main_landmark: !!document.querySelector('main,[role=main]')
                            },
                            visual: {horizontal_overflow_px: Math.max(0, document.documentElement.scrollWidth - innerWidth)},
                            performance: {...(window.__qaVitals || {}), navigation: (() => { const n=performance.getEntriesByType('navigation')[0]; return n ? {dom_content_loaded_ms:n.domContentLoadedEventEnd, load_ms:n.loadEventEnd || null} : {}; })()},
                        };
                    }'''))
                    result.update(observation)
                    result["console_errors_filtered"] = [message for message in result["console_errors"] if not re.search(
                        r"(googlesyndication|doubleclick|criteo|analytics|favicon|ERR_BLOCKED_BY_CLIENT)", message, re.I)]
                    performance = observation["performance"]
                    result["performance_risks"] = (["LCP_OVER_2500MS"] if performance.get("lcp") and performance["lcp"] > 2500 else []) + (
                        ["CLS_OVER_0_1"] if performance.get("cls") is not None and performance["cls"] > 0.1 else [])
                    result["performance_unknowns"] = [metric for metric, value in
                                                      (("LCP", performance.get("lcp")), ("LOAD", performance.get("navigation", {}).get("load_ms")),
                                                       ("INP", None)) if value is None]
                    result["structure_signature"] = sha({"title": observation["title"], "text": observation["text"],
                                                          "structure": observation["structure"], "accessibility": observation["accessibility"]})
                    screenshot = f"{PREFIX}_audit_{sha([url, viewport_name])[:16]}.png"
                    await self.call(page.screenshot(path=str(self.output_dir / screenshot), full_page=True,
                                                    animations="disabled", timeout=self.config.workflow_timeout))
                    result["screenshot"] = screenshot
                    result["screenshot_sha256"] = hashlib.sha256((self.output_dir / screenshot).read_bytes()).hexdigest()
                    focusable = await self.call(page.locator('a[href],button,input,textarea,select,[tabindex]:not([tabindex="-1"])').count())
                    focus_path = []
                    for _ in range(min(focusable, 12)):
                        await self.call(page.keyboard.press("Tab"))
                        focus_path.append(await self.call(page.evaluate("() => { const e=document.activeElement; return e ? [e.tagName,e.id,e.getAttribute('aria-label'),e.innerText?.trim().slice(0,80)] : null; }")))
                    result["keyboard"] = {"focusable_count": focusable,
                                          "unique_focus_targets_reached": len({stable_json(item) for item in focus_path if item}),
                                          "sample_path": focus_path,
                                          "status": "UNKNOWN_NO_FOCUSABLE_CONTROLS" if not focusable else
                                                    "RISK" if not any(item and item[0] != "BODY" for item in focus_path) else "OBSERVED"}
                    baseline = baseline_by_key.get((url, viewport_name))
                    if baseline:
                        baseline_image = self.config.baseline.parent / baseline["screenshot"]
                        if hashlib.sha256(baseline_image.read_bytes()).hexdigest() != baseline.get("screenshot_sha256"):
                            raise RuntimeError("baseline screenshot integrity check failed")
                        ratio = await self.call(asyncio.to_thread(png_difference, baseline_image, self.output_dir / screenshot, self.check))
                        structure_changed = baseline.get("structure_signature") != result["structure_signature"]
                        result["regression"] = {"baseline_found": True, "visual_difference_ratio": round(ratio, 6),
                                                "visual_threshold": self.config.visual_threshold,
                                                "structure_changed": structure_changed,
                                                "status": "REGRESSION" if structure_changed and ratio > self.config.visual_threshold
                                                else "VISUAL_CHANGE" if ratio > self.config.visual_threshold
                                                else "STRUCTURE_CHANGE" if structure_changed else "MATCH"}
                    else:
                        result["regression"] = {"baseline_found": False, "status": "UNKNOWN_NO_BASELINE"}
                    risk_findings = []
                    def audit_risk(severity, classification, count=1):
                        if count:
                            risk_findings.append({"severity": severity, "classification": classification, "count": count})
                    audit_risk("HIGH", "HTTP_FAILURE", len(result["http_errors"]))
                    audit_risk("HIGH", "PAGE_RUNTIME_ERROR", len(result["page_errors"]))
                    audit_risk("MEDIUM", "NETWORK_FAILURE", len(result["network_failures"]))
                    audit_risk("MEDIUM", "CONSOLE_ERROR", len(result["console_errors_filtered"]))
                    for performance_risk in result["performance_risks"]:
                        audit_risk("MEDIUM", performance_risk)
                    audit_risk("MEDIUM", "HORIZONTAL_OVERFLOW", observation["visual"]["horizontal_overflow_px"] > 5)
                    audit_risk("MEDIUM", "BUTTON_MISSING_NAME", observation["accessibility"]["buttons_missing_name"])
                    audit_risk("MEDIUM", "INPUT_MISSING_NAME", observation["accessibility"]["inputs_missing_name"])
                    audit_risk("MEDIUM", "DUPLICATE_ID", len(observation["accessibility"]["duplicate_ids"]))
                    audit_risk("MEDIUM", "KEYBOARD_FOCUS_RISK", result["keyboard"]["status"] == "RISK")
                    audit_risk("HIGH", result["regression"]["status"],
                               result["regression"]["status"] in {"REGRESSION", "VISUAL_CHANGE", "STRUCTURE_CHANGE"})
                    audit_risk("LOW", "IMAGE_MISSING_ALT", observation["accessibility"]["images_missing_alt"])
                    audit_risk("LOW", "HEADING_LEVEL_SKIP", observation["accessibility"]["heading_level_skips"])
                    audit_risk("LOW", "MAIN_LANDMARK_MISSING", not observation["accessibility"]["has_main_landmark"])
                    result["risk_findings"] = risk_findings
                    result["risk_summary"] = dict(Counter(item["severity"] for item in risk_findings))
                    result["status"] = "RISK" if any(item["severity"] in {"HIGH", "MEDIUM"} for item in risk_findings) else "PASS"
                except RunStopped:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.update(status="ERROR", error=str(error)[:2000])
                finally:
                    result["evidence_id"] = self.ev("PAGE_AUDIT", **result)
                    self.page_audit_results.append(result)
                    self.log(f"AUDIT | {url} | {viewport_name} | {result['status']}")
                    if context and not self.interrupted.is_set() and time.monotonic() < self.deadline:
                        await self.call(context.close())
        self.compare_regressions()
        if self.config.write_baseline:
            self.write_regression_baseline()

    def compare_regressions(self):
        self.regression_results = []
        for audit in self.page_audit_results:
            status = audit.get("regression", {}).get("status", "UNKNOWN_NO_BASELINE")
            classification = ("CONFIRMED_REGRESSION" if status == "REGRESSION" and self.config.baseline_approved else
                              "UNAPPROVED_BASELINE_CHANGE" if status == "REGRESSION" else
                              "POSSIBLE_VISUAL_REGRESSION" if status in {"VISUAL_CHANGE", "STRUCTURE_CHANGE"} else status)
            self.regression_results.append({"url": audit["url"], "viewport": audit["viewport"],
                                            "classification": classification, "audit_evidence_id": audit["evidence_id"],
                                            **audit.get("regression", {})})

    def write_regression_baseline(self):
        destination = self.config.write_baseline
        destination.parent.mkdir(parents=True, exist_ok=True)
        audits = []
        for audit in self.page_audit_results:
            if audit["status"] == "ERROR" or not audit.get("screenshot"):
                continue
            image_name = f"{destination.stem}_{sha([audit['url'], audit['viewport']])[:12]}.png"
            shutil.copy2(self.output_dir / audit["screenshot"], destination.parent / image_name)
            audits.append({"url": audit["url"], "viewport": audit["viewport"], "width": audit["width"],
                           "height": audit["height"], "structure_signature": audit["structure_signature"],
                           "screenshot": image_name, "screenshot_sha256": hashlib.sha256((destination.parent / image_name).read_bytes()).hexdigest()})
        payload = {"schema": "qa-v9.2.10-regression-baseline-v1", "target": self.target,
                   "approved": self.config.baseline_approved, "created_at": now(), "source_run_id": self.run_id,
                   "audits": audits}
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        self.ev("BASELINE_WRITTEN", path=str(destination.resolve()), approved=self.config.baseline_approved, audit_count=len(audits))

    async def workflow_screenshot(self, page, result, suffix="result"):
        screenshot = f"{PREFIX}_workflow_{sha([result['test_id'], result['repetition'], suffix])[:16]}.png"
        await self.call(page.screenshot(path=str(self.output_dir / screenshot), timeout=self.config.workflow_timeout))
        result["screenshots"].append(screenshot)
        result["screenshot_hashes"][screenshot] = hashlib.sha256((self.output_dir / screenshot).read_bytes()).hexdigest()

    async def observe_assertion(self, page, assertion):
        locator = page.locator(assertion["selector"])
        count = await self.call(locator.count())
        kind = assertion["kind"]
        if kind == "count":
            return count
        if count > 1:
            raise RuntimeError("assertion selector is ambiguous")
        if not count:
            return False if kind == "visible" else None
        if kind == "visible":
            return await self.call(locator.is_visible())
        if kind == "checked":
            return await self.call(locator.is_checked())
        if kind == "value":
            return await self.call(locator.input_value())
        return await self.call(locator.inner_text())

    async def test_assertion(self, page, assertion):
        deadline = time.monotonic() + self.config.dom_timeout / 1000
        consecutive = 0
        while True:
            observed = await self.observe_assertion(page, assertion)
            expected = assertion["expected"]
            matches = expected in observed if assertion["kind"] == "text_contains" and isinstance(observed, str) else observed == expected
            consecutive = consecutive + 1 if matches else 0
            if consecutive >= 2 or time.monotonic() >= deadline:
                return {**assertion, "observed": observed, "passed": consecutive >= 2}
            await self.call(asyncio.sleep(0.15))

    async def run_workflows(self):
        if not self.config.contract:
            return
        self.ev("WORKFLOW_SCOPE", scope=self.config.contract["scope"],
                approved=self.config.contract.get("scope_approved") is True,
                requirements_sha256=sha(self.config.contract))
        for test in self.config.contract["tests"]:
            for repetition in (1, 2):
                self.check()
                self.phase = "workflow verification"
                self.log(f"WORKFLOW | {test['id']} | repeat={repetition}/2")
                result = {"test_id": test["id"], "repetition": repetition, "url": test["url"],
                          "status": "INCOMPLETE", "assertions": [], "steps": [], "screenshots": [], "screenshot_hashes": {},
                          "http_errors": [], "network_failures": [], "page_errors": []}
                context = None
                page = None
                try:
                    context = await self.call(self.browser.new_context(service_workers="block", accept_downloads=False))
                    self.workflow_routing = True
                    await self.call(context.route("**/*", self.route_request))
                    page = await self.call(context.new_page())
                    page.on("dialog", self.on_dialog)
                    page.on("popup", lambda popup: None if self.expected_popup else self.task(popup.close()))
                    page.on("response", self.on_response)
                    page.on("pageerror", lambda error, bucket=result["page_errors"]:
                            bucket.append(str(error)[:1000]) if len(bucket) < 100 else None)
                    page.on("requestfailed", lambda request, bucket=result["network_failures"]:
                            bucket.append({"url": request.url, "failure": request.failure})
                            if len(bucket) < 100 and same_origin(request.url, self.target) else None)
                    page.on("response", lambda response, bucket=result["http_errors"]:
                            bucket.append({"url": response.url, "status": response.status,
                                           "resource_type": response.request.resource_type})
                            if len(bucket) < 100 and response.status >= 400 and same_origin(response.url, self.target)
                            and response.request.resource_type in {"document", "xhr", "fetch", "script"} else None)
                    page.set_default_timeout(self.config.workflow_timeout)
                    page.set_default_navigation_timeout(self.config.nav_timeout)
                    self.navigation_blocks.clear()
                    if test.get("authentication"):
                        page = await self.authenticate(page, test, result, repetition)
                    response = await self.call(page.goto(test["url"], wait_until="commit"))
                    if not response or response.status >= 400 or self.navigation_blocks:
                        raise RuntimeError("workflow entry failed or was blocked; not a confirmed application assertion failure")
                    await self.call(page.wait_for_function("() => !!document.body", timeout=self.config.dom_timeout))
                    self.metrics["workflow_navigation_attempts"] += 1
                    self.ev("WORKFLOW_ENTRY", test_id=test["id"], repetition=repetition, url=page.url, status=response.status)
                    page = await self.execute_workflow_steps(page, test, result, repetition)
                    for assertion in test["assertions"]:
                        result["assertions"].append(await self.test_assertion(page, assertion))
                    for check in test.get("api_checks", []):
                        result["assertions"].extend(await self.linked_api_check(context.request, check))
                    if self.navigation_blocks:
                        raise RuntimeError("workflow navigation was blocked during verification")
                    await self.workflow_screenshot(page, result)
                    if result["http_errors"] or result["network_failures"] or result["page_errors"]:
                        result.update(status="ERROR", classification="RUNTIME_OR_NETWORK_FAILURE",
                                      error="Runtime/network evidence prevents an isolated business-assertion conclusion")
                    else:
                        result["status"] = "PASS" if all(item["passed"] for item in result["assertions"]) else "ASSERTION_FAILED"
                    if result["status"] == "PASS" and repetition == 1 and self.config.mutation_testing:
                        await self.run_dom_mutations(page, test)
                except RunStopped:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.update(status="ERROR", error=self.safe_error(error)[:2000])
                    if page:
                        try:
                            await self.workflow_screenshot(page, result, "error")
                        except (Exception, RunStopped) as screenshot_error:
                            result["diagnostic_error"] = str(screenshot_error)[:300]
                finally:
                    result["evidence_id"] = self.ev("WORKFLOW_RESULT", **result)
                    self.workflow_results.append(result)
                    self.log(f"WORKFLOW RESULT | {test['id']} | {result['status']}")
                    if context and not self.interrupted.is_set() and time.monotonic() < self.deadline:
                        await self.call(context.close())
                    self.workflow_routing = False

    async def execute_workflow_steps(self, page, test, result, repetition):
        openers = []
        for step in test.get("steps", []):
            if step["operation"] == "return_to_opener":
                if not openers:
                    raise RuntimeError("No explicitly opened popup to return from")
                popup = page
                page = openers.pop()
                if not popup.is_closed():
                    await self.call(popup.close())
                continue
            locator, resolved_selector = await self.resolve_workflow_step(page, step)
            operation = step["operation"]
            if operation in {"click", "double_click", "right_click", "click_popup"}:
                control = await self.call(locator.evaluate(r'''element => ({
                    tag:element.tagName.toLowerCase(), type:(element.type || '').toLowerCase(),
                    in_form:!!(element.form || element.closest('form')),
                    label:[element.getAttribute('aria-label'), element.title, element.innerText, element.value].filter(Boolean).join(' ')
                })'''))
                reason = self.action_block_reason(control, "click" if operation == "click_popup" else operation)
                if reason:
                    raise RuntimeError(reason)
            if operation == "fill":
                await self.call(locator.fill(self.workflow_value(step)))
            elif operation == "select":
                await self.call(locator.select_option(self.workflow_value(step)))
            elif operation == "click_popup":
                self.expected_popup = True
                try:
                    async with page.expect_popup(timeout=self.config.workflow_timeout) as pending:
                        await self.call(locator.click())
                        popup = await self.call(pending.value)
                    await self.call(popup.wait_for_function("() => ['http:', 'https:'].includes(location.protocol)",
                                                            timeout=self.config.workflow_timeout))
                    await self.call(popup.wait_for_load_state("domcontentloaded", timeout=self.config.workflow_timeout))
                    if not any(same_origin(popup.url, origin) for origin in self.config.allowed_origins):
                        raise RuntimeError("Popup origin is not explicitly allowed")
                    openers.append(page)
                    page = popup
                    page.on("dialog", self.on_dialog)
                    page.on("popup", lambda popup: None if self.expected_popup else self.task(popup.close()))
                    page.on("response", self.on_response)
                    page.on("pageerror", lambda error: result["page_errors"].append(self.safe_error(error)[:1000]))
                    page.on("requestfailed", lambda request: result["network_failures"].append({"url": request.url, "failure": request.failure})
                            if len(result["network_failures"]) < 100
                            and any(same_origin(request.url, origin) for origin in self.config.allowed_origins) else None)
                    page.on("response", lambda response: result["http_errors"].append({"url": response.url, "status": response.status})
                            if len(result["http_errors"]) < 100 and response.status >= 400
                            and response.request.resource_type in {"document", "xhr", "fetch", "script"}
                            and any(same_origin(response.url, origin) for origin in self.config.allowed_origins) else None)
                finally:
                    self.expected_popup = False
            elif operation == "check":
                if await self.call(locator.is_visible()):
                    await self.call(locator.check())
                else:
                    identifier = await self.call(locator.get_attribute("id"))
                    label = page.locator("label[for=" + json.dumps(identifier) + "]") if identifier else None
                    if label is None or await self.call(label.count()) != 1:
                        raise RuntimeError("hidden check control has no unique associated label")
                    if not await self.call(locator.is_checked()):
                        await self.call(label.click())
            elif operation == "double_click":
                await self.call(locator.dblclick())
            else:
                await self.call(locator.click(button="right" if operation == "right_click" else "left"))
            if self.navigation_blocks:
                raise RuntimeError("workflow navigation blocked")
            result["steps"].append({"operation": operation, "selector": step["selector"], "resolved_selector": resolved_selector})
            self.ev("WORKFLOW_STEP", test_id=test["id"], repetition=repetition,
                    step_index=len(result["steps"]), operation=operation, selector=step["selector"])
            self.metrics["workflow_steps"] += 1
        return page

    def workflow_value(self, step):
        if "value_env" not in step:
            return step["value"]
        value = os.environ.get(step["value_env"])
        if value is None:
            raise RuntimeError(f"Missing workflow environment variable: {step['value_env']}")
        if value:
            self.secret_values.add(value)
        return value

    def safe_error(self, error):
        message = str(error)
        for secret in self.secret_values:
            message = message.replace(secret, "<redacted>")
        return message

    async def authenticate(self, page, test, result, repetition):
        auth = test["authentication"]
        response = await self.call(page.goto(canon(auth["url"], self.target), wait_until="domcontentloaded", timeout=self.config.nav_timeout))
        if not response or response.status >= 400:
            raise RuntimeError("Authentication entry failed")
        page = await self.execute_workflow_steps(page, {**auth, "id": test["id"] + ":authentication"}, result, repetition)
        assertions = [await self.test_assertion(page, assertion) for assertion in auth["assertions"]]
        if not all(assertion["passed"] for assertion in assertions) or self.navigation_blocks:
            raise RuntimeError("Authentication postcondition was not established")
        self.ev("AUTHENTICATION_VERIFIED", test_id=test["id"], repetition=repetition, assertion_count=len(assertions))
        return page

    async def linked_api_check(self, request_context, check):
        if self.metrics["api_requests"] >= self.config.max_api_requests:
            raise RuntimeError("MAX_API_REQUESTS")
        destination = canon(check["url"], self.target)
        headers = {name: self.workflow_value({"value_env": variable}) for name, variable in check.get("headers_env", {}).items()}
        self.metrics["api_requests"] += 1
        response = await self.call(request_context.get(destination, headers=headers, timeout=self.config.workflow_timeout, max_redirects=0))
        try:
            if response.status >= 500 or response.status in {401, 403} or 300 <= response.status < 400:
                raise RuntimeError(f"Linked API service/auth/redirect failure: HTTP {response.status}")
            size = response.headers.get("content-length")
            if size and int(size) > self.config.max_api_response_bytes:
                raise RuntimeError("API_RESPONSE_SIZE_LIMIT")
            body = await self.call(response.body())
            if len(body) > self.config.max_api_response_bytes:
                raise RuntimeError("API_RESPONSE_SIZE_LIMIT")
            results = [{"kind": "http_status", "selector": destination, "expected": check["expected_status"],
                        "observed": response.status, "passed": response.status == check["expected_status"]}]
            if check.get("assertions"):
                payload = json.loads(body)
                for assertion in check["assertions"]:
                    observed, found = payload, True
                    for part in assertion["path"]:
                        try:
                            observed = observed[part]
                        except (KeyError, IndexError, TypeError):
                            observed, found = None, False
                            break
                    results.append({**assertion, "selector": destination + " " + stable_json(assertion["path"]),
                                    "observed": observed, "passed": found and stable_json(observed) == stable_json(assertion["expected"])})
            self.ev("LINKED_API_CHECK", url=destination, http_status=response.status,
                    response_sha256=hashlib.sha256(body).hexdigest(), assertions=results)
            return results
        finally:
            await self.call(response.dispose())

    async def resolve_workflow_step(self, page, step):
        selector = step["selector"]
        locator = page.locator(selector)
        timeout = min(self.config.dom_timeout, self.config.workflow_timeout) if step.get("fallback_selector") else self.config.workflow_timeout
        try:
            await self.call(locator.wait_for(state="attached", timeout=timeout))
        except RunStopped:
            raise
        except Exception:
            if not step.get("fallback_selector") or await self.call(locator.count()) != 0:
                raise
            selector = step["fallback_selector"]
            locator = page.locator(selector)
            for _ in range(2):
                if await self.call(locator.count()) != 1:
                    raise RuntimeError("safe fallback did not resolve uniquely")
                signature = await self.call(locator.evaluate(r'''element => ({tag:element.tagName.toLowerCase(),
                    name:(element.getAttribute('aria-label') || Array.from(element.labels || []).map(label=>label.textContent).join(' ') ||
                          element.innerText || element.getAttribute('placeholder') || '').replace(/\s+/g,' ').trim()})'''))
                if signature != {"tag": step["target_tag"], "name": step["target_name"]}:
                    raise RuntimeError("safe fallback semantic signature mismatch")
                if not await self.call(locator.is_visible()) or not await self.call(locator.is_enabled()):
                    raise RuntimeError("safe fallback is not actionable")
                await self.call(asyncio.sleep(0.1))
            event = {"original_selector": step["selector"], "resolved_selector": selector, "signature": signature,
                     "reason": "Explicit fallback matched the approved tag/name twice; primary selector was absent"}
            event["evidence_id"] = self.ev("SELF_HEAL", **event)
            self.healing_events.append(event)
        if await self.call(locator.count()) != 1:
            raise RuntimeError("workflow target must resolve exactly once")
        return locator, selector

    async def run_dom_mutations(self, page, test):
        for assertion in test["assertions"]:
            if len(self.mutation_results) >= self.config.max_mutations:
                return
            if assertion["kind"] not in {"value", "checked", "text_contains"}:
                continue
            self.check()
            result = {"test_id": test["id"], "selector": assertion["selector"], "assertion_kind": assertion["kind"],
                      "status": "INCOMPLETE", "scope": "DOM postcondition assertion sensitivity"}
            locator = page.locator(assertion["selector"])
            backup = None
            self.phase = "DOM mutation verification"
            try:
                if await self.call(locator.count()) != 1:
                    raise RuntimeError("mutation target is ambiguous or absent")
                backup = await self.call(locator.evaluate(r'''(element, kind) => {
                    if (kind==='value' && ['INPUT','TEXTAREA'].includes(element.tagName) && element.type!=='password') {
                        const original=element.value; element.value='QA_MUTANT_' + original; return {kind,original};
                    }
                    if (kind==='checked' && element.tagName==='INPUT' && (element.type==='checkbox' || element.type==='radio' && element.checked)) {
                        const original=element.checked; element.checked=!original; return {kind,original};
                    }
                    if (kind==='text_contains' && !element.children.length && ['P','SPAN','DIV','LABEL','H1','H2','OUTPUT'].includes(element.tagName)) {
                        const original=element.textContent; element.textContent=''; return {kind,original};
                    }
                    return null;
                }''', assertion["kind"]))
                if backup is None:
                    result["status"] = "UNSUPPORTED_TARGET"
                    continue
                observation = await self.test_assertion(page, assertion)
                result.update(status="SURVIVED" if observation["passed"] else "KILLED", assertion=observation)
                name = f"{PREFIX}_mutation_{len(self.mutation_results)}.png"
                await self.call(page.screenshot(path=str(self.output_dir / name), timeout=self.config.workflow_timeout))
                result.update(screenshot=name, screenshot_sha256=hashlib.sha256((self.output_dir / name).read_bytes()).hexdigest())
            except RunStopped:
                raise
            except Exception as error:
                result.update(status="ERROR", error=str(error)[:1000])
            finally:
                try:
                    if backup is not None and time.monotonic() < self.deadline and not self.interrupted.is_set():
                        await self.call(locator.evaluate(r'''(element, saved) => {
                            if(saved.kind==='value') element.value=saved.original;
                            else if(saved.kind==='checked') element.checked=saved.original;
                            else element.textContent=saved.original;
                        }''', backup))
                        restored = await self.observe_assertion(page, assertion)
                        if restored != backup["original"]:
                            result.update(status="ERROR", error="Mutation restoration could not be verified")
                finally:
                    result["evidence_id"] = self.ev("MUTATION_RESULT", **result)
                    self.mutation_results.append(result)
                    self.log(f"MUTATION | {test['id']} | {result['status']}")

    async def run_api_tests(self):
        tests = (self.config.contract or {}).get("api_tests", [])
        for test in tests:
            for repetition in (1, 2):
                self.check()
                self.phase = "API verification"
                result = {"test_id": test["id"], "repetition": repetition, "url": test["url"], "method": test["method"],
                          "status": "INCOMPLETE", "assertions": []}
                request_context = None
                headers = {}
                try:
                    if test["method"] not in {"GET", "HEAD"} and not self.config.allow_api_writes:
                        result.update(status="BLOCKED", error="Explicit --allow-api-writes is required")
                        continue
                    if self.metrics["api_requests"] >= self.config.max_api_requests:
                        result.update(status="BLOCKED", error="MAX_API_REQUESTS")
                        continue
                    for header, variable in test.get("headers_env", {}).items():
                        if not os.environ.get(variable):
                            raise RuntimeError(f"API header environment variable is missing: {variable}")
                        headers[header] = os.environ[variable]
                    request_context = await self.call(self.playwright.request.new_context())
                    self.metrics["api_requests"] += 1
                    response = await self.call(request_context.fetch(test["url"], method=test["method"], headers=headers,
                                                                     data=test.get("json"), timeout=self.config.workflow_timeout,
                                                                     max_redirects=0, fail_on_status_code=False))
                    declared_size = response.headers.get("content-length")
                    if test["method"] != "HEAD" and declared_size and int(declared_size) > self.config.max_api_response_bytes:
                        raise RuntimeError("API_RESPONSE_SIZE_LIMIT")
                    body = await self.call(response.body())
                    if len(body) > self.config.max_api_response_bytes:
                        raise RuntimeError("API_RESPONSE_SIZE_LIMIT")
                    result.update(http_status=response.status, response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest())
                    expected_status = test["expected_status"]
                    result["assertions"].append({"kind": "http_status", "selector": test["url"], "expected": expected_status,
                                                 "observed": response.status, "passed": response.status == expected_status})
                    if response.status != expected_status and (response.status >= 500 or response.status in {401, 403} or 300 <= response.status < 400):
                        result.update(status="ERROR", classification="SERVICE_AUTH_OR_REDIRECT_FAILURE")
                        continue
                    if test.get("assertions"):
                        payload = json.loads(body)
                        for assertion in test["assertions"]:
                            observed, found = payload, True
                            for part in assertion["path"]:
                                try:
                                    if isinstance(observed, list) and (type(part) is not int or part < 0):
                                        raise KeyError(part)
                                    observed = observed[part]
                                except (KeyError, IndexError, TypeError):
                                    observed, found = None, False
                                    break
                            result["assertions"].append({**assertion, "selector": stable_json(assertion["path"]),
                                                         "observed": observed, "found": found,
                                                         "passed": found and stable_json(observed) == stable_json(assertion["expected"])})
                    result["status"] = "PASS" if all(item["passed"] for item in result["assertions"]) else "ASSERTION_FAILED"
                except RunStopped:
                    raise
                except Exception as error:
                    message = str(error)
                    for secret in headers.values():
                        message = message.replace(secret, "<redacted>")
                    result.update(status="ERROR", classification="API_TOOL_OR_RESPONSE_FAILURE", error=message[:1000])
                finally:
                    result["evidence_id"] = self.ev("API_RESULT", **result)
                    self.api_results.append(result)
                    self.log(f"API | {test['id']} | repeat={repetition}/2 | {result['status']}")
                    if request_context:
                        try:
                            await asyncio.wait_for(request_context.dispose(), timeout=2)
                        except Exception as error:
                            self.err("api_cleanup", error)

    def api_summary(self):
        tests = (self.config.contract or {}).get("api_tests", [])
        events = {item["evidence_id"]: item for item in self.evidence if item["type"] == "API_RESULT"}
        valid = self.evidence_intact() and all(events.get(result.get("evidence_id"), {}).get("details") ==
                                               {key: value for key, value in result.items() if key != "evidence_id"} for result in self.api_results)
        valid = valid and len({(item["test_id"], item["repetition"]) for item in self.api_results}) == len(self.api_results)
        passed, findings = 0, []
        approved = (self.config.contract or {}).get("scope_approved") is True
        for test in tests:
            runs = [item for item in self.api_results if item["test_id"] == test["id"]]
            if len(runs) == 2 and all(item["status"] == "PASS" for item in runs):
                passed += 1
            failures = [{sha({key: assertion[key] for key in ("kind", "selector", "expected")}): assertion
                         for assertion in run["assertions"] if not assertion["passed"]}
                        for run in runs if run["status"] == "ASSERTION_FAILED"]
            if valid and len(failures) == 2:
                for signature in sorted(set(failures[0]) & set(failures[1])):
                    findings.append({"classification": "CONFIRMED_REQUIREMENT_FAILURE" if approved else "UNAPPROVED_EXPECTATION_MISMATCH",
                                     "test_id": test["id"], "family": "API", "assertion": failures[0][signature],
                                     "evidence_ids": [item["evidence_id"] for item in runs],
                                     "why": "The same declared API assertion failed in two fresh request contexts."})
        return {"declared": len(tests), "twice_verified": passed, "evidence_valid": bool(valid), "findings": findings,
                "coverage_percent": round(100 * passed / len(tests), 2) if tests else None}

    def requirement_coverage(self):
        contract = self.config.contract or {}
        results = self.workflow_results + self.api_results
        items = []
        for requirement in contract.get("requirements", []):
            linked = requirement["test_ids"]
            verified = [identifier for identifier in linked if len([item for item in results if item["test_id"] == identifier]) == 2 and
                        all(item["status"] == "PASS" for item in results if item["test_id"] == identifier)]
            items.append({"id": requirement["id"], "title": requirement["title"], "critical": requirement.get("critical", False),
                          "test_ids": linked, "twice_verified_test_ids": verified,
                          "status": "UNMAPPED" if not linked else "VERIFIED" if len(verified) == len(linked) else "MISSING_EVIDENCE"})
        return {"requirements": items, "declared": len(items), "verified": sum(item["status"] == "VERIFIED" for item in items),
                "gaps": [item for item in items if item["status"] != "VERIFIED"],
                "scope": "Explicit requirement IDs only; undiscovered requirements remain unknown"}

    def workflow_verdict(self):
        contract = self.config.contract
        approved = bool(contract and contract.get("scope_approved") is True)
        findings = []
        passed = 0
        for test in contract["tests"] if contract else []:
            runs = [item for item in self.workflow_results if item["test_id"] == test["id"]]
            if len(runs) == 2 and all(item["status"] == "PASS" for item in runs):
                passed += 1
            failures = [{sha({key: assertion[key] for key in ("kind", "selector", "expected")}): assertion
                         for assertion in run["assertions"] if not assertion["passed"]}
                        for run in runs if run["status"] == "ASSERTION_FAILED"]
            repeated = set(failures[0]) & set(failures[1]) if len(failures) == 2 else set()
            for signature in sorted(repeated):
                findings.append({"classification": "CONFIRMED_REQUIREMENT_FAILURE" if approved else "UNAPPROVED_EXPECTATION_MISMATCH",
                                 "test_id": test["id"], "assertion": failures[0][signature],
                                 "evidence_ids": [run["evidence_id"] for run in runs],
                                 "why": "The same declared assertion failed in two fresh browser contexts."})
        total = len(contract["tests"]) if contract else 0
        evidence_by_id = {item["evidence_id"]: item for item in self.evidence if item["type"] == "WORKFLOW_RESULT"}
        scope_valid = bool(contract) and any(item["type"] == "WORKFLOW_SCOPE" and
                                             item["details"].get("requirements_sha256") == sha(contract) for item in self.evidence)
        persisted = self.evidence_intact()
        evidence_valid = (not total or bool(self.workflow_results)) and scope_valid and persisted
        evidence_valid = evidence_valid and len({(item["test_id"], item["repetition"]) for item in self.workflow_results}) == len(self.workflow_results)
        for result in self.workflow_results:
            event = evidence_by_id.get(result.get("evidence_id"), {})
            evidence_valid = evidence_valid and event.get("run_id") == self.run_id and event.get("current_run") is True
            evidence_valid = evidence_valid and event.get("details") == {key: value for key, value in result.items() if key != "evidence_id"}
            if result["status"] in {"PASS", "ASSERTION_FAILED"}:
                evidence_valid = evidence_valid and bool(result["screenshots"]) and all(
                    self.image_intact(name, result.get("screenshot_hashes", {}).get(name)) for name in result["screenshots"])
        api = self.api_summary()
        findings.extend(api["findings"])
        evidence_valid = evidence_valid and api["evidence_valid"]
        confirmed = any(item["classification"] == "CONFIRMED_REQUIREMENT_FAILURE" for item in findings)
        confirmed_regressions = [item for item in self.regression_results if item["classification"] == "CONFIRMED_REGRESSION"]
        audit_errors = [item for item in self.page_audit_results if item["status"] not in {"PASS", "RISK"}]
        audit_risks = [item for item in self.page_audit_results if item["status"] == "RISK"]
        audit_events = {item["evidence_id"]: item for item in self.evidence if item["type"] == "PAGE_AUDIT"}
        valid_audit_ids = set()
        for audit in self.page_audit_results:
            event = audit_events.get(audit.get("evidence_id"), {})
            if (persisted and event.get("run_id") == self.run_id and
                    event.get("details") == {key: value for key, value in audit.items() if key != "evidence_id"} and
                    self.image_intact(audit.get("screenshot"), audit.get("screenshot_sha256"))):
                valid_audit_ids.add(audit["evidence_id"])
        observed_audits = {(item["url"], item["viewport"]) for item in self.page_audit_results}
        audit_required = self.config.page_audits and bool(self.pages or total or self.config.baseline)
        audit_complete = (not audit_required or bool(self.audit_plan) and not self.audit_scope_truncated and
                          set(self.audit_plan) == observed_audits and len(valid_audit_ids) == len(self.audit_plan) and not audit_errors)
        baseline_keys = {(item["url"], item["viewport"]) for item in (self.config.baseline_data or {}).get("audits", [])}
        baseline_matches = {(item["url"], item["viewport"]) for item in self.regression_results if item.get("status") == "MATCH"}
        baseline_complete = not self.config.baseline or (self.config.baseline_approved and bool(baseline_keys) and
                             baseline_keys == observed_audits == baseline_matches and audit_complete)
        regression_evidence_valid = bool(confirmed_regressions) and self.config.baseline_approved and all(
            item["audit_evidence_id"] in valid_audit_ids for item in confirmed_regressions)
        requirement_coverage = self.requirement_coverage()
        mutation_events = {item["evidence_id"]: item for item in self.evidence if item["type"] == "MUTATION_RESULT"}
        mutations_valid = bool(self.mutation_results) and persisted and all(
            item["status"] == "KILLED" and mutation_events.get(item.get("evidence_id"), {}).get("details") ==
            {key: value for key, value in item.items() if key != "evidence_id"} and
            self.image_intact(item.get("screenshot"), item.get("screenshot_sha256")) for item in self.mutation_results)
        eligible_mutations = sum(assertion["kind"] in {"value", "checked", "text_contains"}
                                 for test in (contract or {}).get("tests", []) for assertion in test["assertions"])
        mutation_complete = not self.config.mutation_testing or mutations_valid and len(self.mutation_results) == eligible_mutations
        source_events = {item["evidence_id"]: item["details"] for item in self.evidence if item["type"] == "SOURCE_MUTATION"}
        source_complete = not self.config.source_plan or (persisted and len(self.source_results) == 2 + len(self.config.source_plan["mutants"]) and all(
            item["status"] == ("PASS" if item["type"] == "BASELINE" else "KILLED") and
            source_events.get(item["evidence_id"]) == {key: value for key, value in item.items() if key != "evidence_id"}
            for item in self.source_results))
        if confirmed and evidence_valid or regression_evidence_valid:
            decision = "NOT SAFE TO RELEASE"
        elif (approved and total + api["declared"] and passed == total and api["twice_verified"] == api["declared"] and
              evidence_valid and audit_complete and baseline_complete and mutation_complete and source_complete and not requirement_coverage["gaps"] and
              not self.errors and not audit_errors and not audit_risks and not self.interrupted.is_set() and
              not set(self.stop_reasons) & {"MAX_SECONDS", "INTERRUPTED", "RUNTIME_ERROR", "SETUP_ERROR", "INCOMPLETE"}):
            decision = "SAFE TO RELEASE"
        else:
            decision = "INSUFFICIENT EVIDENCE"
        reasons = []
        if not approved:
            reasons.append("Business scope is missing or unapproved")
        if not total + api["declared"] or passed != total or api["twice_verified"] != api["declared"]:
            reasons.append("Some declared workflows lack two passing executions")
        if not evidence_valid:
            reasons.append("Workflow evidence is missing, incomplete, or altered")
        if not audit_complete:
            reasons.append("Enabled page audits are incomplete, truncated, or have invalid evidence")
        if audit_risks:
            reasons.append("Page audits recorded quality risks or baseline changes")
        if not baseline_complete:
            reasons.append("The supplied baseline lacks a complete approved comparison")
        if requirement_coverage["gaps"]:
            reasons.append("Declared requirements have missing tests or incomplete verification")
        if not mutation_complete:
            reasons.append("Requested DOM mutation checks lack intact killed-mutant evidence")
        if not source_complete:
            reasons.append("Requested source mutation campaign is incomplete, invalid, or contains surviving mutants")
        if self.errors or set(self.stop_reasons) & {"MAX_SECONDS", "INTERRUPTED", "RUNTIME_ERROR", "SETUP_ERROR", "INCOMPLETE"}:
            reasons.append("Runtime failure, interruption, or deadline prevents a complete assessment")
        if confirmed and evidence_valid:
            reasons.append("An approved requirement failed repeatedly with intact evidence")
        if regression_evidence_valid:
            reasons.append("An approved baseline regression has intact evidence")
        return {"decision": decision, "scope": contract["scope"] if contract else "Unknown business/release requirements",
                "reasons": reasons if decision != "SAFE TO RELEASE" else ["All enabled checks passed for the approved declared scope"],
                "scope_approved": approved, "declared_workflows": total, "twice_verified_workflows": passed,
                "workflow_coverage_percent": round(100 * passed / total, 2) if total else None,
                "evidence_valid": evidence_valid, "regression_evidence_valid": regression_evidence_valid,
                "api_coverage": api, "requirement_coverage": requirement_coverage,
                "mutation_gate_complete": bool(mutation_complete) if self.config.mutation_testing else None,
                "source_mutation_gate_complete": bool(source_complete) if self.config.source_plan else None,
                "page_audit_scope_complete": bool(audit_complete) if self.config.page_audits else None,
                "baseline_comparison_complete": bool(baseline_complete) if self.config.baseline else None,
                "page_audit_scope_truncated": self.audit_scope_truncated,
                "findings": findings, "confirmed_regressions": confirmed_regressions,
                "page_audit_risks": len(audit_risks), "page_audit_errors": len(audit_errors),
                "limitations": ["Decision applies only to the explicitly declared, approved workflow scope.",
                                "Exploration state changes are UI observations, not business acceptance assertions.",
                                "Unknown requirements, backend correctness, and untested security properties are not inferred."]}

    def evidence_intact(self):
        if not self.persistence_ok:
            return False
        try:
            if not self.evidence_file.closed:
                self.evidence_file.flush()
            return [json.loads(line) for line in (self.output_dir / f"{PREFIX}_evidence.jsonl").read_text(encoding="utf-8").splitlines()] == self.evidence
        except (OSError, ValueError):
            return False

    def image_intact(self, name, expected_hash):
        try:
            return bool(name and Path(name).name == name and expected_hash and
                        hashlib.sha256((self.output_dir / name).read_bytes()).hexdigest() == expected_hash)
        except (OSError, ValueError):
            return False

    async def source_test_command(self, directory):
        process = await asyncio.create_subprocess_exec(*self.config.source_plan["command"], cwd=str(directory),
                    env={"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        async def collect():
            output = bytearray()
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 65536:
                    raise RuntimeError("SOURCE_TEST_OUTPUT_LIMIT")
            return await process.wait(), bytes(output)
        try:
            code, output = await self.call(asyncio.wait_for(collect(), timeout=min(30, self.config.workflow_timeout / 1000)))
            try:
                report = json.loads(output.decode().splitlines()[-1])
            except (ValueError, IndexError, UnicodeError):
                report = {"status": "ERROR"}
            status = report.get("status") if isinstance(report, dict) else "ERROR"
            if (status, code) not in {("PASS", 0), ("ASSERTION_FAILED", 1)}:
                status = "ERROR"
            return {"status": status, "exit_code": code, "output_sha256": hashlib.sha256(output).hexdigest()}
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                await process.wait()

    async def run_source_mutations(self):
        if not self.config.source_plan:
            return
        self.phase = "source mutation tests"
        root = self.config.source_root
        with tempfile.TemporaryDirectory(prefix="qa-source-") as scratch:
            pristine = Path(scratch) / "pristine"
            pristine.mkdir()
            count, total = 0, 0
            for directory, folders, names in os.walk(root, followlinks=False):
                folders[:] = [name for name in folders if name not in {".git", ".venv", "venv", "node_modules", "__pycache__"}
                               and not (Path(directory) / name).is_symlink()]
                for name in names:
                    self.check()
                    source = Path(directory) / name
                    if source.is_symlink() or name.startswith(".env"):
                        continue
                    count += 1
                    total += source.stat().st_size
                    if count > 2000 or total > 20 * 1024 * 1024:
                        raise RuntimeError("SOURCE_COPY_LIMIT")
                    destination = pristine / source.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            for repetition in (1, 2):
                trial = Path(scratch) / f"baseline-{repetition}"
                shutil.copytree(pristine, trial)
                result = {"type": "BASELINE", "repetition": repetition, **await self.source_test_command(trial)}
                result["evidence_id"] = self.ev("SOURCE_MUTATION", **result)
                self.source_results.append(result)
                if result["status"] != "PASS":
                    return
            for index, mutant in enumerate(self.config.source_plan["mutants"]):
                self.check()
                trial = Path(scratch) / f"mutant-{index}"
                shutil.copytree(pristine, trial)
                target = trial / Path(mutant["path"])
                if not target.resolve().is_relative_to(trial.resolve()) or not target.is_file():
                    raise RuntimeError("Mutation target was excluded from the isolated copy")
                original = target.read_text()
                result = {"type": "MUTANT", "index": index, "path": mutant["path"], "status": "INVALID",
                          "original_sha256": hashlib.sha256(original.encode()).hexdigest()}
                if original.count(mutant["find"]) == 1 and mutant["find"] != mutant["replace"]:
                    changed = original.replace(mutant["find"], mutant["replace"], 1)
                    try:
                        if target.suffix == ".py":
                            ast.parse(changed)
                        target.write_text(changed)
                        outcome = await self.source_test_command(trial)
                        result.update(outcome, status={"ASSERTION_FAILED": "KILLED", "PASS": "SURVIVED"}.get(outcome["status"], "ERROR"),
                                      mutated_sha256=hashlib.sha256(changed.encode()).hexdigest())
                    except SyntaxError:
                        result["status"] = "INVALID_SYNTAX"
                result["evidence_id"] = self.ev("SOURCE_MUTATION", **result)
                self.source_results.append(result)
                self.log(f"SOURCE MUTATION | {mutant['path']} | {result['status']}")

    def source_investigation(self):
        candidates = []
        root = self.config.source_root
        if root:
            for requirement in (self.config.contract or {}).get("requirements", []):
                for reference in requirement.get("source_refs", []):
                    try:
                        path = (root / reference["path"]).resolve()
                        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1048576:
                            raise ValueError("source reference outside scope or size limit")
                        lines = path.read_text().splitlines()
                        line = reference["line"]
                        if type(line) is not int or not 1 <= line <= len(lines):
                            raise ValueError("invalid source line")
                        linked = [item["evidence_id"] for item in self.workflow_results + self.api_results
                                  if item["test_id"] in requirement["test_ids"] and item["status"] != "PASS"]
                        candidates.append({"requirement_id": requirement["id"], "path": str(path), "line": line,
                                           "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                           "failure_evidence_ids": linked, "classification": "DECLARED_SOURCE_CANDIDATE",
                                           "causality_proven": False})
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        candidates.append({"classification": "INVALID_SOURCE_REFERENCE", "error": str(error)})
        return {"configured": bool(root), "candidates": candidates,
                "scope": "Explicit requirement-to-source references; correlation is not source-code causal proof"}

    def root_cause_analysis(self):
        findings = []
        for result in self.api_results:
            if result["status"] in {"ERROR", "BLOCKED", "INCOMPLETE"}:
                findings.append({"severity": "high", "classification": result.get("classification", "API_TEST_BLOCKED_OR_INCOMPLETE"),
                                 "layer": "api_service_or_test_environment", "confidence": 0.8,
                                 "summary": f"{result['test_id']}: {result['status']} (HTTP {result.get('http_status', 'unobserved')}).",
                                 "evidence_refs": [result["evidence_id"]],
                                 "next_diagnostic": "Check the explicit API contract, authentication environment, request budget, and service logs."})
        for result in self.workflow_results:
            if result.get("http_errors") or result.get("network_failures") or result.get("page_errors"):
                findings.append({"severity": "high", "classification": "WORKFLOW_RUNTIME_OR_NETWORK_FAILURE",
                                 "layer": "runtime_or_network", "confidence": 0.8,
                                 "summary": f"{result['test_id']} observed runtime/network errors during verification.",
                                 "evidence_refs": [result["evidence_id"]],
                                 "next_diagnostic": "Inspect recorded request URLs/statuses and JavaScript errors before attributing the assertion mismatch."})
        for finding in self.workflow_verdict()["findings"]:
            findings.append({"severity": "high", "classification": finding["classification"],
                             "layer": "application_behavior" if finding["classification"] == "CONFIRMED_REQUIREMENT_FAILURE" else "requirements_scope",
                             "confidence": 0.9 if finding["classification"] == "CONFIRMED_REQUIREMENT_FAILURE" else 0.65,
                             "summary": finding["why"], "evidence_refs": finding["evidence_ids"],
                             "next_diagnostic": "Inspect the application handler and backend request for the failed declared postcondition."})
        for regression in self.regression_results:
            if regression["classification"] not in {"MATCH", "UNKNOWN_NO_BASELINE"}:
                findings.append({"severity": "high" if regression["classification"] == "CONFIRMED_REGRESSION" else "medium",
                                 "classification": regression["classification"], "layer": "rendering_or_content",
                                 "confidence": 0.85 if regression["classification"] == "CONFIRMED_REGRESSION" else 0.55,
                                 "summary": f"{regression['url']} changed at {regression['viewport']} relative to the explicit baseline.",
                                 "evidence_refs": [regression["audit_evidence_id"]],
                                 "next_diagnostic": "Compare the current and baseline screenshots, then map the changed DOM region to the owning component."})
        for audit in self.page_audit_results:
            if audit.get("http_errors"):
                findings.append({"severity": "high", "classification": "APPLICATION_HTTP_FAILURE", "layer": "application_or_server",
                                 "confidence": 0.85, "summary": f"Same-origin HTTP failures occurred while auditing {audit['url']}.",
                                 "evidence_refs": [audit["evidence_id"]], "next_diagnostic": "Inspect server logs and the failed request response."})
            accessibility = audit.get("accessibility", {})
            if accessibility.get("inputs_missing_name") or accessibility.get("buttons_missing_name"):
                findings.append({"severity": "medium", "classification": "ACCESSIBILITY_NAME_RISK", "layer": "application_markup",
                                 "confidence": 0.8, "summary": f"Interactive controls lack programmatic names on {audit['url']}.",
                                 "evidence_refs": [audit["evidence_id"]], "next_diagnostic": "Inspect the unnamed controls and add explicit labels or accessible names."})
        phase_map = {"navigation": ("network_or_navigation", 0.6), "replay": ("automation_recovery", 0.75),
                     "resolution": ("automation_locator", 0.8), "action": ("automation_or_application", 0.55),
                     "route": ("browser_framework", 0.8), "cleanup": ("browser_framework", 0.8), "setup": ("test_environment", 0.9)}
        for error in self.errors:
            layer, confidence = phase_map.get(error["phase"], ("unknown", 0.4))
            findings.append({"severity": "medium", "classification": "TEST_INFRASTRUCTURE_OR_AUTOMATION_FAILURE",
                             "layer": layer, "confidence": confidence, "summary": error["error"].splitlines()[0][:500],
                             "evidence_refs": [entry["evidence_id"] for entry in self.evidence if entry["type"] == "ERROR" and entry["details"] == error],
                             "next_diagnostic": "Reproduce with the saved state path and inspect the named automation layer; do not classify as an app defect without a failed oracle."})
        return {"method": "Evidence-ranked causal layer classification; source-code root cause is UNKNOWN until correlated with application telemetry or code.",
                "findings": sorted(findings, key=lambda item: {"high": 0, "medium": 1, "low": 2}[item["severity"]]),
                "unknowns": ["Backend implementation and logs were not available", "Browser evidence cannot prove source-code causality"]}

    def coverage(self):
        discovered = set(self.pairs)
        executed = {key for key, pair in self.pairs.items() if pair["attempted"]}
        verified = {key for key, pair in self.pairs.items() if pair["status"] == "VERIFIED"}
        pending = {key for key, pair in self.pairs.items() if pair["status"] == "PENDING"}
        blocked = {key for key, pair in self.pairs.items() if pair["status"] == "BLOCKED"}
        discovered_actions = {key[1] for key in discovered}
        executed_actions = {key[1] for key in executed}
        verified_actions = {key[1] for key in verified}
        evidence_by_id = {entry["evidence_id"]: entry for entry in self.evidence}
        current_evidence = all(entry["run_id"] == self.run_id and entry["version"] == VERSION
                               and entry["current_run"] is True for entry in self.evidence)
        def matches_evidence(evidence_id, kind, pair):
            entry = evidence_by_id.get(evidence_id, {})
            detail = entry.get("details", {})
            return (entry.get("type") == kind and detail.get("state_id") == pair["state_id"] and
                    detail.get("action_id") == pair["action_id"] and
                    sha(detail.get("identity")) == pair["action_id"] and
                    (kind != "EXECUTION_FINISHED" or detail.get("outcome") == pair["status"]))
        complete_evidence = all(matches_evidence(pair["discovery_evidence_id"], "STATE_ACTION_DISCOVERY", pair) and
                                (not pair["attempted"] or
                                 matches_evidence(pair.get("start_evidence_id"), "EXECUTION_STARTED", pair) and
                                 matches_evidence(pair["execution_evidence_id"], "EXECUTION_FINISHED", pair))
                                for pair in self.pairs.values())
        started_pairs = [(entry["details"].get("state_id"), entry["details"].get("action_id"))
                         for entry in self.evidence if entry["type"] == "EXECUTION_STARTED"]
        finished_pairs = [(entry["details"].get("state_id"), entry["details"].get("action_id"))
                          for entry in self.evidence if entry["type"] == "EXECUTION_FINISHED"]
        reconciled = (executed <= discovered and verified <= executed and
                      len(discovered) == len(executed) + len(pending) + len(blocked) and
                      not (executed & (pending | blocked)) and
                      len(executed) == self.metrics["execution_attempts"] and
                      self.metrics["guard_acquires"] == self.metrics["guard_releases"] == len(executed) and
                      len(started_pairs) == len(finished_pairs) == len(executed) and
                      set(started_pairs) == set(finished_pairs) == executed and
                      not self.in_progress and all(key[1] == sha(self.actions[key[1]].identity) for key in discovered))
        def percent(numerator, denominator):
            return round(100 * numerator / denominator, 2) if denominator else None
        return {
            "unique_discovered_actions": len(discovered_actions),
            "unique_executed_actions": len(executed_actions),
            "unique_unexecuted_actions": len(discovered_actions - executed_actions),
            "unique_actions_with_verified_observation": len(verified_actions),
            "discovered_state_action_pairs": len(discovered), "executed_state_action_pairs": len(executed),
            "verified_state_action_pairs": len(verified), "pending_state_action_pairs": len(pending),
            "blocked_state_action_pairs": len(blocked),
            "unexecuted_state_action_pairs": len(discovered - executed),
            "unverified_state_action_pairs": len(discovered - verified),
            "outcome_counts": dict(Counter(pair["status"] for pair in self.pairs.values())),
            "unique_action_execution_percent": percent(len(executed_actions), len(discovered_actions)),
            "state_action_execution_percent": percent(len(executed), len(discovered)),
            "state_action_verified_percent": percent(len(verified), len(discovered)),
            "verified_direct_postconditions": sum(self.pairs[key].get("detail", {}).get("observation_strength") == "DIRECT_POSTCONDITION" for key in verified),
            "verified_state_change_observations_only": sum(self.pairs[key].get("detail", {}).get("observation_strength") != "DIRECT_POSTCONDITION" for key in verified),
            "reconciled": reconciled, "complete_execution_evidence": complete_evidence,
            "current_run_evidence_only": current_evidence,
            "identity_schema": "(state_id, action_id); action_id = SHA256(canonical semantic identity JSON)",
            "denominator": "current-run discovered semantic state-action pairs, including blocked controls; direct navigation is separate",
            "replay_policy": "restoration steps are counted separately, consume replay/time budgets, and never increase coverage",
        }

    def behavior_plan(self):
        plans = []
        for url in sorted(self.pages):
            actions = [action for action in self.actions.values() if action.identity["surface"] == url]
            inputs = [action.action_id for action in actions if action.operation in {"fill", "select", "check", "upload"}]
            submissions = [action.action_id for action in actions if re.search(r"\b(submit|save|sign in|log in|search)\b", action.label, re.I)]
            plans.append({"url": url, "inferred_behavior": "form or data-entry workflow" if inputs else "navigation or interaction surface",
                          "priority": "high" if submissions else "medium",
                          "input_actions": inputs, "completion_candidates": submissions,
                          "blocked_risks": [{"action_id": action.action_id, "reason": action.blocked} for action in actions if action.blocked],
                          "required_oracle": "Explicit expected outcome is needed; changing DOM alone is not acceptance."})
        return {"basis": "Heuristic plan from current-run DOM; inferred priorities are not business requirements", "plans": plans}

    def graph(self):
        outgoing, incoming = {}, {}
        for transition in self.transitions:
            outgoing.setdefault(transition["from_state"], set()).add(transition["to_state"])
            incoming.setdefault(transition["to_state"], set()).add(transition["from_state"])
        reachable = set()
        pending = deque([self.initial_state_id] if self.initial_state_id else [])
        while pending:
            state_id = pending.popleft()
            if state_id in reachable:
                continue
            reachable.add(state_id)
            pending.extend(outgoing.get(state_id, set()) - reachable)
        reversible = {tuple(sorted((source, target))) for source, targets in outgoing.items() for target in targets
                      if source != target and source in outgoing.get(target, set())}
        return {"initial_state": self.initial_state_id, "state_count": len(self.states),
                "transition_count": len(self.transitions), "transitions": self.transitions,
                "states": [{"state_id": state.state_id, "url": state.url, "title": state.title} for state in self.states.values()],
                "intelligence": {"reachable_from_initial_by_semantic_actions": sorted(reachable),
                                 "direct_navigation_entry_states": [state_id for state_id, path in self.paths.items() if not path["steps"]],
                                 "self_loops": sum(item["from_state"] == item["to_state"] for item in self.transitions),
                                 "reversible_state_pairs": sorted(reversible),
                                 "observed_dead_ends": [state_id for state_id in self.states if not outgoing.get(state_id)],
                                 "branching_states": {state_id: len(targets) for state_id, targets in outgoing.items() if len(targets) > 1}}}

    def frontier(self):
        by_state = {}
        for pair in self.pairs.values():
            if pair["status"] != "VERIFIED":
                by_state.setdefault(pair["state_id"], []).append({"action_id": pair["action_id"],
                                                                 "status": pair["status"], "reason": pair.get("reason")})
        return {"frontier_states": by_state, "frontier_state_count": len(by_state),
                "pending_navigation": [item for item in self.navigation.values() if item["status"] != "VERIFIED"],
                "unexpanded_states": sorted(set(self.states) - self.expanded),
                "fully_verified_states": sorted(state_id for state_id in self.expanded if state_id not in by_state),
                "stop_reasons": self.stop_reasons, "limit_reasons": sorted(self.limit_reasons),
                "scope": "current-run observed main-document DOM; frames and truncated snapshots cannot authorize release"}

    def truth(self):
        coverage = self.coverage()
        frontier = self.frontier()
        verdict = self.workflow_verdict()
        gates = {
            "current_root_discovery": self.root_discovered,
            "nonempty_semantic_model": bool(self.pairs),
            "no_legacy_state_reuse": True,
            "current_run_evidence_only": coverage["current_run_evidence_only"],
            "execution_evidence_complete": coverage["complete_execution_evidence"],
            "canonical_counts_reconcile": coverage["reconciled"],
            "all_discovered_pairs_verified": bool(self.pairs) and coverage["unverified_state_action_pairs"] == 0,
            "navigation_complete": not frontier["pending_navigation"],
            "all_observed_states_expanded": not frontier["unexpanded_states"],
            "no_budget_or_scope_truncation": self.stop_reasons == ["SCOPED_FRONTIER_EXHAUSTED"] and not self.limit_reasons,
            "no_runtime_errors": not self.errors,
            "evidence_persisted": self.persistence_ok,
        }
        return {"target": self.target, "headless": self.config.headless, "root_discovered": self.root_discovered,
                "discovery_pages": len(self.pages), "attempted_page_surfaces": len(self.page_attempts),
                "stop_reasons": self.stop_reasons, "limit_reasons": sorted(self.limit_reasons),
                "legacy_state_reused": False, "coverage": coverage, "gates": gates,
                "regression_baseline_loaded": bool(self.config.baseline),
                "regression_baseline_role": "Explicit comparison oracle only; never used for discovery, action identity, state replay, or coverage",
                "exploration_truth_gate": all(gates.values()),
                "release_truth_gate": verdict["decision"] == "SAFE TO RELEASE",
                "release_decision": verdict,
                "claim_scope": verdict["scope"]}

    def risk_model(self):
        risks = []
        for page_url, page in self.pages.items():
            actions = [action for action in self.actions.values() if action.identity["surface"] == page_url]
            factors = []
            if any(action.operation in {"fill", "select", "check", "upload"} for action in actions):
                factors.append({"factor": "data_entry", "weight": 20})
            if any(action.control.get("sensitive") or action.control["type"] == "password" for action in actions):
                factors.append({"factor": "sensitive_input", "weight": 30})
            if any(action.blocked == "POTENTIALLY_IRREVERSIBLE_ACTION" for action in actions):
                factors.append({"factor": "irreversible_action_candidate", "weight": 40})
            if any(action.blocked == "FORM_SUBMISSION_REQUIRES_OPT_IN" for action in actions):
                factors.append({"factor": "submission_candidate", "weight": 20})
            if any(item["url"] == page["url"] and item["status"] != "PASS" for item in self.page_audit_results):
                factors.append({"factor": "observed_quality_risk", "weight": 20})
            score = min(100, 10 + sum(item["weight"] for item in factors))
            risks.append({"url": page["url"], "score": score, "priority": "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW",
                          "factors": factors, "source_state_ids": page["state_ids"], "authority": "HEURISTIC_CURRENT_RUN_DOM"})
        return {"risks": sorted(risks, key=lambda item: (-item["score"], item["url"])),
                "meaning": "Prioritization weights, not probabilities or an inferred business impact assessment",
                "business_risk_unknown": not bool((self.config.contract or {}).get("requirements"))}

    def critical_journeys(self):
        declared = [{"requirement_id": item["id"], "title": item["title"], "critical": item["critical"],
                     "test_ids": item["test_ids"], "status": item["status"], "authority": "DECLARED_REQUIREMENT"}
                    for item in self.requirement_coverage()["requirements"]]
        candidates = [{"entry_url": path["entry_url"], "state_id": state_id,
                       "action_ids": [step["action_id"] for step in path["steps"]], "authority": "OBSERVED_STATE_PATH"}
                      for state_id, path in self.paths.items() if len(path["steps"]) >= 2]
        return {"declared": sorted(declared, key=lambda item: not item["critical"]), "observed_multi_step_candidates": candidates,
                "limitation": "Observed multi-step paths are candidates; business criticality requires explicit requirements"}

    def write_director_report(self, truth):
        decision = truth["release_decision"]
        requirements = decision["requirement_coverage"]
        api = decision["api_coverage"]
        risks = self.risk_model()
        tested_endpoints = {(item["method"], item["url"]) for item in (self.config.contract or {}).get("api_tests", [])}
        untested_endpoints = [item for key, item in self.observed_api_endpoints.items() if key not in tested_endpoints]
        payload = {"version": VERSION, "run_id": self.run_id, "decision": decision["decision"], "scope": decision["scope"],
                   "scope_approved": decision["scope_approved"], "reasons": decision["reasons"],
                   "critical_requirement_gaps": [item for item in requirements["gaps"] if item["critical"]],
                   "requirement_coverage": requirements, "api_coverage": api, "exploration_coverage": truth["coverage"],
                   "untested_observed_api_endpoints": untested_endpoints, "top_risks": risks["risks"][:10],
                   "next_actions": decision["reasons"] if decision["decision"] != "SAFE TO RELEASE" else ["Review this decision within its declared scope"],
                   "source_mutations": self.source_results,
                   "source_investigation": self.source_investigation(),
                   "unknowns": ["Undeclared business rules", "Unobserved API endpoints",
                                "Source-code causality beyond supplied source references", "Full security and accessibility conformance"]}
        (self.output_dir / f"{PREFIX}_qa_director.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        report = (f"# QA Director — V{VERSION}\n\nDecision: **{decision['decision']}**\n\n"
                  f"Application: {self.target}\n\nScope: {decision['scope']}\n\n"
                  f"Scope approved: {decision['scope_approved']}. Run: {self.run_id}.\n\n"
                  "## Decision evidence\n\n" + "\n".join(f"- {reason}" for reason in decision["reasons"]) + "\n\n"
                  f"UI workflows verified twice: {decision['twice_verified_workflows']}/{decision['declared_workflows']}. "
                  f"API tests verified twice: {api['twice_verified']}/{api['declared']}.\n\n"
                  f"Mapped requirements verified: {requirements['verified']}/{requirements['declared']}. "
                  f"Discovered state/action pairs verified: {truth['coverage']['verified_state_action_pairs']}/{truth['coverage']['discovered_state_action_pairs']}.\n\n"
                  "These denominators are separate. Unknown requirements and undiscovered endpoints are not counted as passed.\n\n"
                  "## Missing requirement coverage\n\n")
        report += "\n".join(f"- {item['id']}: {item['status']}; critical={item['critical']}; {item['title']}" for item in requirements["gaps"])
        if not requirements["declared"]:
            report += "No requirement inventory was supplied; completeness of business coverage is UNKNOWN."
        elif not requirements["gaps"]:
            report += "All supplied requirement IDs have passing mapped tests."
        report += "\n\n## Risk-ranked pages\n\n"
        report += "\n".join(f"- {item['priority']} ({item['score']}/100): {item['url']} — " +
                            ", ".join(factor["factor"] for factor in item["factors"]) for item in risks["risks"][:10]) or "No DOM-derived risk model is available."
        report += (f"\n\nObserved API endpoints without explicit tests: {len(untested_endpoints)}. "
                   f"Recorded safe locator recoveries: {len(self.healing_events)}. "
                   f"DOM assertion mutants killed: {sum(item['status'] == 'KILLED' for item in self.mutation_results)}/{len(self.mutation_results)}.\n\n"
                   f"Source mutants killed: {sum(item['status'] == 'KILLED' for item in self.source_results if item.get('kind') == 'MUTANT')}/"
                   f"{sum(item.get('kind') == 'MUTANT' for item in self.source_results)}; source baselines are independently repeated.\n\n"
                   "## Evidence and next investigation\n\n"
                   f"See `{PREFIX}_root_cause_analysis.json`, `{PREFIX}_requirement_coverage.json`, `{PREFIX}_api_results.json`, "
                   f"`{PREFIX}_mutation_report.json`, `{PREFIX}_source_mutations.json`, `{PREFIX}_source_investigation.json`, "
                   f"and `{PREFIX}_evidence.jsonl` in this run directory.\n\n"
                   "Root-cause findings identify observed layers and supplied source candidates; source-code causality is never claimed without direct proof.\n")
        (self.output_dir / f"{PREFIX}_qa_director.md").write_text(report, encoding="utf-8")

    def write_reports(self):
        metadata = {"version": VERSION, "run_id": self.run_id, "current_run": True, "generated_at": now()}
        truth = self.truth()
        efficiency = {"elapsed_seconds": round(time.monotonic() - self.t0, 3), "metrics": dict(self.metrics),
                      "budgets": {name: getattr(self.config, name) for name in (
                          "max_pages", "max_seconds", "max_actions", "max_states", "max_depth", "max_actions_per_state",
                          "max_actions_per_page",
                          "max_replays", "max_replay_actions", "nav_timeout", "launch_timeout", "dom_timeout", "action_timeout", "journey_timeout",
                          "explore_frames", "max_frames", "workflow_timeout", "max_recovery_attempts", "max_audit_pages",
                          "allow_form_submission", "allow_api_writes", "max_api_requests", "max_api_response_bytes",
                          "mutation_testing", "max_mutations")},
                      "stop_reasons": self.stop_reasons, "limit_reasons": sorted(self.limit_reasons),
                      "coverage": truth["coverage"], "navigation": list(self.navigation.values()),
                      "action_attempts_by_page": dict(self.page_action_counts),
                      "guards_released_exactly_once": self.metrics["guard_acquires"] == self.metrics["guard_releases"] and not self.in_progress}
        artifacts = {"efficiency_report": efficiency,
                     "source_mutations": {"configured": bool(self.config.source_plan), "results": self.source_results},
                     "source_investigation": self.source_investigation(),
                     "risk_model": self.risk_model(),
                     "critical_journeys": self.critical_journeys(),
                     "requirement_coverage": self.requirement_coverage(),
                     "api_results": {"results": self.api_results, "summary": self.api_summary(),
                                     "observed_endpoints": list(self.observed_api_endpoints.values())},
                     "mutation_report": {"enabled": self.config.mutation_testing, "results": self.mutation_results,
                                         "eligible_assertions": sum(assertion["kind"] in {"value", "checked", "text_contains"}
                                                                    for test in (self.config.contract or {}).get("tests", []) for assertion in test["assertions"]),
                                         "killed": sum(item["status"] == "KILLED" for item in self.mutation_results),
                                         "selected_mutants": len(self.mutation_results), "maximum_mutants": self.config.max_mutations,
                                         "scope": "Selected reversible DOM postcondition mutants; application-source and API mutation coverage are UNKNOWN"},
                     "self_healing": {"events": self.healing_events, "scope": "Explicit fallback selector plus exact tag/name; assertions are never rewritten"},
                     "release_decision": truth["release_decision"],
                     "workflow_results": {"results": self.workflow_results},
                     "workflow_plan": self.config.contract or {"scope": "No supported workflow inferred", "scope_approved": False, "tests": []},
                     "page_quality_audits": {"audits": self.page_audit_results,
                                             "planned_url_viewports": self.audit_plan, "scope_truncated": self.audit_scope_truncated,
                                             "automated_accessibility_scope": "Programmatic names, labels, duplicate IDs, heading skips and landmarks; not full WCAG conformance",
                                             "performance_scope": "Observed navigation timing, buffered LCP and CLS; INP remains UNKNOWN without representative interaction latency"},
                     "regressions": {"baseline": str(self.config.baseline.resolve()) if self.config.baseline else None,
                                     "baseline_approved": self.config.baseline_approved,
                                     "results": self.regression_results,
                                     "no_baseline_meaning": "UNKNOWN, never PASS"},
                     "recovery": {"events": self.recovery_events, "maximum_attempts_per_navigation": self.config.max_recovery_attempts},
                     "root_cause_analysis": self.root_cause_analysis(),
                     "behavior_plan": self.behavior_plan(),
                     "application_understanding": {
                         "basis": "Inferred from observed DOM, not an authoritative business specification",
                         "pages": [{"url": item["url"], "title": item["title"]} for item in self.pages.values()],
                         "behavior_families": dict(Counter(action.semantic for action in self.actions.values())),
                         "known_requirements": self.config.contract,
                         "unknowns": ["Business rules not explicitly asserted", "Backend persistence", "Authorization and security guarantees"]},
                     "coverage": truth["coverage"], "frontier": self.frontier(), "state_graph": self.graph(),
                     "state_paths": {"paths": self.paths}, "application_map": {"pages": self.pages},
                     "semantic_behaviors": {"actions": [asdict(action) for action in self.actions.values()]},
                     "state_action_discovery": {"pairs": list(self.pairs.values())},
                     "navigation_surfaces": {"navigation": list(self.navigation.values())},
                     "errors": {"errors": self.errors}}
        for name, payload in artifacts.items():
            destination = self.output_dir / f"{PREFIX}_{name}.json"
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({**metadata, **payload}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(destination)
        decision = truth["release_decision"]
        self.write_director_report(truth)
        summary = (f"# V{VERSION} release assessment\n\n## {decision['decision']}\n\n"
                   f"Target: {self.target}\n\nScope: {decision['scope']}\n\n"
                   f"Approved scope: {decision['scope_approved']}\n\n"
                   f"Workflows verified twice: {decision['twice_verified_workflows']} / {decision['declared_workflows']}\n\n"
                   f"Exploration pairs verified: {truth['coverage']['verified_state_action_pairs']} / {len(self.pairs)}\n\n"
                   "Workflow coverage and exploration coverage are separate denominators. Neither measures unknown requirements.\n\n"
                   "## Findings\n\n")
        for finding in decision["findings"]:
            summary += f"- {finding['classification']}: {finding['test_id']}. {finding['why']} Evidence: {', '.join(finding['evidence_ids'])}\n"
            summary += f"  Assertion: {finding['assertion']['kind']} on {finding['assertion']['selector']}; expected {finding['assertion']['expected']!r}; observed {finding['assertion']['observed']!r}.\n"
        if not decision["findings"]:
            summary += "No repeatable declared-requirement failure was established. This is not proof of defect absence.\n"
        for regression in decision["confirmed_regressions"]:
            summary += f"- CONFIRMED_REGRESSION: {regression['url']} at {regression['viewport']}; visual difference {regression.get('visual_difference_ratio')}. Evidence: {regression['audit_evidence_id']}\n"
        summary += "\n## Test blockers and risks\n\n"
        for reason in decision["reasons"]:
            summary += f"- {reason}\n"
        for result in self.workflow_results:
            if result["status"] not in {"PASS", "ASSERTION_FAILED"}:
                summary += f"- {result['test_id']}: {result['status']}: {result.get('error', 'budget or interruption')}\n"
        summary += f"\nExploration runtime errors: {len(self.errors)}. Scope limits: {', '.join(sorted(self.limit_reasons)) or 'none recorded'}.\n\n"
        summary += f"Page audits: {len(self.page_audit_results)}. Confirmed regressions: {len(decision['confirmed_regressions'])}. Root-cause classifications: {len(self.root_cause_analysis()['findings'])}.\n\n"
        summary += "\n".join(f"- {item}" for item in decision["limitations"]) + "\n"
        (self.output_dir / f"{PREFIX}_release_assessment.md").write_text(summary, encoding="utf-8")
        report = (f"# QA Agent V{VERSION} efficiency report\n\n"
                  f"Run: {self.run_id}\nTarget: {self.target}\nElapsed: {efficiency['elapsed_seconds']} seconds\n\n"
                  f"Stop reasons: {', '.join(self.stop_reasons)}\nLimits: {', '.join(sorted(self.limit_reasons)) or 'none'}\n\n"
                  f"Unique discovered / executed actions: {truth['coverage']['unique_discovered_actions']} / {truth['coverage']['unique_executed_actions']}\n"
                  f"Discovered / executed / verified state-action pairs: {len(self.pairs)} / {self.metrics['execution_attempts']} / {truth['coverage']['verified_state_action_pairs']}\n"
                  f"Replay attempts / replay actions: {self.metrics['replay_attempts']} / {self.metrics['replay_actions']}\n"
                  f"Guard acquisitions / releases: {self.metrics['guard_acquires']} / {self.metrics['guard_releases']}\n"
                  f"Coverage reconciles: {truth['coverage']['reconciled']}\n"
                  f"Release truth gate: {'PASS' if truth['release_truth_gate'] else 'FAIL-CLOSED'}\n\n"
                  "Navigation is counted separately from semantic controls. Restoration replay never increases execution coverage. "
                  "Failed, unchanged, interrupted, blocked, and unvisited work cannot count as verified behavior. "
                  "Each run uses a fresh browser context and a unique report directory; no prior artifacts are loaded.\n")
        (self.output_dir / f"{PREFIX}_efficiency_report.md").write_text(report, encoding="utf-8")
        destination = self.output_dir / f"{PREFIX}_canonical_truth.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({**metadata, **truth}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(destination)
        self.log(f"REPORTS | {self.output_dir.resolve()}")
        self.log(f"COVERAGE | pairs={len(self.pairs)} attempted={self.metrics['execution_attempts']} "
                 f"verified={truth['coverage']['verified_state_action_pairs']} reconciled={truth['coverage']['reconciled']}")
        self.log(f"RELEASE TRUTH GATE | {'PASS' if truth['release_truth_gate'] else 'FAIL-CLOSED'}")
        self.log(f"RELEASE DECISION | {truth['release_decision']['decision']}")
        return truth

    async def cleanup(self):
        self.phase = "browser cleanup"
        cleanup_deadline = time.monotonic() + 5
        closing = set()
        if self.playwright is None and self.driver_start_task is not None:
            done, _ = await asyncio.wait({self.driver_start_task}, timeout=2)
            if done and not self.driver_start_task.cancelled() and self.driver_start_task.exception() is None:
                self.playwright = self.driver_start_task.result()
        driver = self.playwright or self.playwright_manager
        for resource in (self.context, self.browser, driver):
            if resource is None:
                continue
            if resource is self.playwright:
                shutdown = resource.stop()
            elif resource is self.playwright_manager:
                shutdown = resource.__aexit__(None, None, None)
            else:
                shutdown = resource.close()
            operation = asyncio.create_task(shutdown)
            closing.add(operation)
            done, _ = await asyncio.wait({operation}, timeout=min(1.5, max(0.05, cleanup_deadline - time.monotonic())))
            if done:
                try:
                    operation.result()
                    if resource is driver and self.driver_start_task and not self.driver_start_task.done():
                        self.driver_start_task.cancel()
                        await asyncio.gather(self.driver_start_task, return_exceptions=True)
                except Exception as error:
                    self.err("cleanup", error)
                closing.discard(operation)
        pending = closing | self.operations | self.event_tasks
        if pending:
            done, pending = await asyncio.wait(pending, timeout=max(0.05, cleanup_deadline - time.monotonic()))
            for operation in done:
                if not operation.cancelled():
                    operation.exception()
            if pending:
                self.err("cleanup", "browser operations exceeded the cleanup grace period")
                for operation in pending:
                    operation.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self.operations.clear()
        self.event_tasks.clear()

    async def run(self):
        loop = asyncio.get_running_loop()
        old_handler = signal.getsignal(signal.SIGINT)
        handler_installed = False
        heartbeat = asyncio.create_task(self.heartbeat())
        exit_code = None
        self.log(f"V{VERSION} | run={self.run_id} | target={self.target} | headless={self.config.headless}")
        self.log(f"BUDGETS | pages={self.config.max_pages} seconds={self.config.max_seconds:g} "
                 f"actions={self.config.max_actions} states={self.config.max_states} depth={self.config.max_depth}")
        self.log("CURRENT-RUN DISCOVERY | no legacy state or report inputs")
        try:
            try:
                loop.add_signal_handler(signal.SIGINT, self.interrupted.set)
                handler_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            from playwright.async_api import async_playwright
            self.playwright_manager = async_playwright()
            self.driver_start_task = asyncio.create_task(self.playwright_manager.start())
            self.playwright = await self.call(self.driver_start_task)
            if not self.config.requirements_only or (self.config.contract or {}).get("tests") or self.config.baseline:
                self.log("STARTING CHROMIUM")
                self.browser = await self.call(self.playwright.chromium.launch(headless=self.config.headless, timeout=self.config.launch_timeout))
                self.context = await self.call(self.browser.new_context(service_workers="block", accept_downloads=False))
                await self.call(self.context.route("**/*", self.route_request))
                self.log("CHROMIUM READY")
            if not self.config.requirements_only:
                try:
                    await self.pipeline()
                except RunStopped:
                    self.check()
            self.generate_workflows()
            await self.audit_pages()
            await self.run_workflows()
            await self.run_api_tests()
            await self.run_source_mutations()
            if self.config.requirements_only:
                self.stop("DECLARED_WORKFLOWS_COMPLETE")
        except RunStopped:
            pass
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.interrupted.set()
            self.stop("INTERRUPTED")
        except ImportError as error:
            self.err("setup", f"{error}; install with: python3 -m pip install playwright && python3 -m playwright install chromium")
            self.stop("SETUP_ERROR")
            exit_code = 2
        except Exception as error:
            self.err("run" if self.browser else "setup", error)
            self.stop("RUNTIME_ERROR" if self.browser else "SETUP_ERROR")
            if self.browser is None:
                exit_code = 2
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await self.cleanup()
                if not self.stop_reasons:
                    self.stop("INCOMPLETE")
                truth = self.write_reports()
                if exit_code is None:
                    exit_code = 130 if self.interrupted.is_set() else 0 if truth["release_truth_gate"] else 1
            except Exception as error:
                self.persistence_ok = False
                self.log(f"REPORT/CLEANUP ERROR | {error}; release remains fail-closed")
                exit_code = 2
            finally:
                self.evidence_file.close()
                if self.log_handle:
                    self.log_handle.close()
                if handler_installed:
                    loop.remove_signal_handler(signal.SIGINT)
                    signal.signal(signal.SIGINT, old_handler)
        return exit_code


def main(argv=None):
    config = args(argv)
    try:
        return asyncio.run(Agent(config).run())
    except KeyboardInterrupt:
        print("Interrupted; release remains fail-closed.", file=sys.stderr, flush=True)
        return 130
    except OSError as error:
        print(f"Cannot initialize reports: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
