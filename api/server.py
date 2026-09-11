from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = Path(os.getenv("QA_REPORT_ROOT", ROOT / "reports")).resolve()
REPORT_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("QA_MAX_UPLOAD_BYTES", "10485760"))
API_TOKEN = os.getenv("QA_API_TOKEN")
JOBS: dict[str, dict] = {}
ALLOWED_SUFFIXES = {".txt", ".md", ".json", ".pdf", ".doc", ".docx"}

app = FastAPI(title="Autonomous Quality Engineering API", version="11.0.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorize(value: str | None) -> None:
    if API_TOKEN and value != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid API token")


async def validate_public_url(value: str) -> str:
    if any(marker in value for marker in ("[", "](", ")")):
        raise HTTPException(status_code=400, detail="Use a plain URL, not Markdown syntax")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="A public HTTP or HTTPS URL is required")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443,
                                            type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise HTTPException(status_code=400, detail=f"Unable to resolve target host: {error}") from error
    for address in {item[4][0] for item in addresses}:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="Private, local, reserved, and link-local targets are blocked")
    return value.strip()


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    return "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))


def extract_document(path: Path) -> str:
    if path.suffix in {".txt", ".md", ".json"}:
        return path.read_text(encoding="utf-8")
    if path.suffix == ".docx":
        return extract_docx(path)
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if path.suffix == ".doc":
        completed = subprocess.run(["antiword", str(path)], capture_output=True, text=True,
                                   timeout=30, check=False)
        if completed.returncode:
            raise ValueError("Unable to extract legacy DOC; convert it to DOCX or PDF")
        return completed.stdout
    raise ValueError("Supported requirement formats: TXT, Markdown, JSON, PDF, DOC, and DOCX")


async def save_upload(upload: UploadFile, directory: Path) -> Path:
    suffix = Path(upload.filename or "requirements.txt").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Supported files: TXT, Markdown, JSON, PDF, DOC, and DOCX")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Requirements file is too large")
    destination = directory / f"requirements{suffix}"
    destination.write_bytes(data)
    return destination


def requirement_argument(path: Path, directory: Path) -> tuple[str, Path]:
    text = extract_document(path).strip()
    if not text:
        raise ValueError("Requirements document contains no extractable text")
    extracted = directory / "requirements-extracted.txt"
    extracted.write_text(text, encoding="utf-8")
    if path.suffix == ".json":
        try:
            document = json.loads(text)
            if isinstance(document, dict) and document.get("scope") and document.get("scope_approved") is True:
                return "--requirements", path
        except json.JSONDecodeError:
            pass
    return "--product-brief", extracted


async def execute_job(job_id: str, target_url: str, document: Path | None, headed: bool,
                      allow_form_submission: bool) -> None:
    job = JOBS[job_id]
    directory = Path(job["directory"])
    command = [sys.executable, "-u", str(ROOT / "qa11.py"), target_url,
               "--headed" if headed else "--headless", "--max-pages", "10", "--max-seconds", "300",
               "--max-actions", "80", "--output-dir", str(directory / "run"),
               "--log-file", str(directory / "execution.log")]
    try:
        if document:
            flag, path = requirement_argument(document, directory)
            command += [flag, str(path)]
            if flag == "--requirements":
                command.append("--requirements-only")
        if allow_form_submission:
            command.append("--allow-form-submission")
        job.update(status="RUNNING", started_at=now())
        process = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
                                                       stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
        lines = []
        while line := await process.stdout.readline():
            lines.append(line.decode(errors="replace"))
            job["recent_log"] = "".join(lines[-40:])
        exit_code = await process.wait()
        organization_files = list((directory / "run").rglob("qa_v11_organization.json"))
        if not organization_files:
            raise RuntimeError(f"QA engine produced no organization report (exit {exit_code})")
        report = json.loads(organization_files[-1].read_text(encoding="utf-8"))
        job.update(status="COMPLETE", completed_at=now(), exit_code=exit_code,
                   decision=report.get("decision"), summary=report.get("summary"),
                   report_directory=str(organization_files[-1].parent))
    except Exception as error:
        job.update(status="FAILED", completed_at=now(), error=str(error)[:2000])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "11.0.0"}


@app.post("/api/jobs", status_code=202)
async def create_job(target_url: str = Form(...), requirements: UploadFile | None = File(None),
                     headed: bool = Form(False), allow_form_submission: bool = Form(False),
                     authorization: str | None = Header(None)):
    authorize(authorization)
    target = await validate_public_url(target_url)
    job_id = uuid.uuid4().hex
    directory = REPORT_ROOT / job_id
    directory.mkdir(parents=True, exist_ok=False)
    document = await save_upload(requirements, directory) if requirements else None
    JOBS[job_id] = {"id": job_id, "status": "QUEUED", "target": target, "created_at": now(),
                    "directory": str(directory), "recent_log": ""}
    asyncio.create_task(execute_job(job_id, target, document, headed, allow_form_submission))
    return {"job_id": job_id, "status": "QUEUED"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, authorization: str | None = Header(None)):
    authorize(authorization)
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return {key: value for key, value in JOBS[job_id].items() if key != "directory"}


@app.get("/api/jobs/{job_id}/report")
async def get_report(job_id: str, authorization: str | None = Header(None)):
    authorize(authorization)
    job = JOBS.get(job_id)
    if not job or job.get("status") != "COMPLETE":
        raise HTTPException(status_code=404, detail="Completed report not found")
    path = Path(job["report_directory"]) / "qa_v11_qa_director.md"
    return FileResponse(path, media_type="text/markdown", filename=f"qa-v11-{job_id}.md")
