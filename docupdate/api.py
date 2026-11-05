"""FastAPI wrapper.  uvicorn docupdate.api:app --reload

POST /update        JSON {"sourceDocument": str, "changes": [str]} -> {"updatedDocument", "report"}
POST /update/pdf    multipart: file=<pdf>, changes=<JSON list>    -> same

Input limits (env): DOCUPDATE_MAX_PDF_MB (default 20), DOCUPDATE_MAX_CHARS (default 500000, ~80k words),
DOCUPDATE_MAX_CHANGES (default 50).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .llm import LLM
from .pdf_io import pdf_to_markdown
from .pipeline import update_document

load_dotenv()
MAX_PDF_MB = float(os.getenv("DOCUPDATE_MAX_PDF_MB", "20"))
MAX_PDF_BYTES = int(MAX_PDF_MB * 1024 * 1024)
MAX_CHARS = int(os.getenv("DOCUPDATE_MAX_CHARS", "500000"))
MAX_CHANGES = int(os.getenv("DOCUPDATE_MAX_CHANGES", "50"))

app = FastAPI(title="LLM-driven document updates")


class UpdateRequest(BaseModel):
    sourceDocument: str = Field(min_length=1, max_length=MAX_CHARS)
    changes: list[str] = Field(min_length=1, max_length=MAX_CHANGES)
    verify: bool = True


@app.post("/update")
async def update(req: UpdateRequest):
    return await update_document(req.sourceDocument, req.changes, LLM(), verify=req.verify)


def parse_changes(raw: str) -> list[str]:
    """Form fields are text. Accept a JSON list (curly quotes from copy-paste are normalized),
    or fall back to one change per line."""
    text = raw.strip().translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(422, f"changes looks like a JSON list but is invalid ({e}). "
                                     'Use ["change one", "change two"] with straight double quotes.')
    else:
        items = text.splitlines()
    items = [str(c).strip() for c in items if str(c).strip()]
    if not items:
        raise HTTPException(422, 'changes is empty: send ["change one", "change two"] or one change per line')
    if len(items) > MAX_CHANGES:
        raise HTTPException(422, f"too many changes ({len(items)}); the limit is {MAX_CHANGES}")
    return items


@app.post("/update/pdf")
async def update_pdf(file: UploadFile = File(...), changes: str = Form(...), verify: bool = Form(True)):
    change_list = parse_changes(changes)
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF is larger than {MAX_PDF_MB:g} MB")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            # parsing is CPU-bound and synchronous: run it off the event loop so other requests keep moving
            source = await asyncio.to_thread(pdf_to_markdown, tmp.name)
        except Exception as e:
            raise HTTPException(422, f"could not read the PDF ({type(e).__name__})")
    if len(source) > MAX_CHARS:
        raise HTTPException(413, f"document text is {len(source)} characters; the limit is {MAX_CHARS}")
    return await update_document(source, change_list, LLM(), verify=verify)
