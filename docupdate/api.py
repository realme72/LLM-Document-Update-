"""FastAPI wrapper.  uvicorn docupdate.api:app --reload

POST /update        JSON {"sourceDocument": str, "changes": [str]} -> {"updatedDocument", "report"}
POST /update/pdf    multipart: file=<pdf>, changes=<JSON list>    -> same
"""
from __future__ import annotations

import json
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .llm import LLM
from .pdf_io import pdf_to_markdown
from .pipeline import update_document

load_dotenv()
app = FastAPI(title="LLM-driven document updates")


class UpdateRequest(BaseModel):
    sourceDocument: str
    changes: list[str] = Field(min_length=1)
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
    return items


@app.post("/update/pdf")
async def update_pdf(file: UploadFile = File(...), changes: str = Form(...), verify: bool = Form(True)):
    change_list = parse_changes(changes)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp.flush()
        source = pdf_to_markdown(tmp.name)
    return await update_document(source, change_list, LLM(), verify=verify)
