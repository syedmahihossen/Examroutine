from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import tempfile, os

from parser import parse_exam_routine, parse_seat_plan, build_student_routine

app = FastAPI(title="Exam Routine Generator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health():
    return {"ok": True}

MAX_FILE_SIZE = 25 * 1024 * 1024

async def save_upload(upload: UploadFile) -> str:
    filename = upload.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, f"{filename or 'Uploaded file'} is not a PDF.")
    if upload.content_type and upload.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(400, "Only PDF files are accepted.")

    suffix = ".pdf"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        size = 0
        with open(path, "wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(413, "PDF is too large. Maximum size is 25 MB.")
                f.write(chunk)
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

@app.post("/api/analyze")
async def analyze(
    routine_pdf: UploadFile = File(...),
    seat_plan_pdf: Optional[UploadFile] = File(None),
    section: str = Form(...),
):
    if not section.strip():
        raise HTTPException(400, "Section is required.")

    routine_path = await save_upload(routine_pdf)
    seat_path = None

    try:
        routine = parse_exam_routine(routine_path)

        seat_plan = None
        if seat_plan_pdf and seat_plan_pdf.filename:
            seat_path = await save_upload(seat_plan_pdf)
            seat_plan = parse_seat_plan(seat_path)

        result = build_student_routine(
            routine=routine,
            seat_plan=seat_plan,
            section=section.strip(),
        )

        return result

    except Exception as exc:
        raise HTTPException(422, f"Could not process PDFs: {exc}")

    finally:
        for p in [routine_path, seat_path]:
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
