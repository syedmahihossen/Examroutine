from __future__ import annotations

import os
import tempfile
import time
import logging
from threading import Lock
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from noticeboard import NOTICEBOARD_URL, collect_notice_candidates, discover_documents
from parser import build_student_routine, parse_exam_routine, parse_seat_plan

# Keep third-party PDF warnings out of production logs.
for _name in ("pdfminer", "pdfminer.pdfpage", "pdfplumber"):
    logging.getLogger(_name).setLevel(logging.ERROR)

APP_VERSION = "8.1.0"
MAX_FILE_SIZE = 25 * 1024 * 1024

# Optional shared secret for the background refresh endpoint.
# Set REFRESH_SECRET in Render environment variables.
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "").strip()

app = FastAPI(title="Exam Routine Generator API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory result cache. Prevents students from launching expensive
# Playwright crawls. Render free tier clears it on sleep/restart — that is OK
# because the GitHub Actions refresh job will warm it again.
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = Lock()
_INFLIGHT_LOCK = Lock()
_INFLIGHT: set[tuple] = set()
CACHE_SECONDS = 1800  # 30 minutes — longer because of proactive refresh


def _write_temp(raw: bytes, file_type: str) -> str:
    if not raw or len(raw) > MAX_FILE_SIZE:
        raise ValueError("Invalid or oversized document")
    if file_type == "pdf" and not raw.startswith(b"%PDF"):
        raise ValueError("The discovered routine is not a valid PDF")
    if file_type == "xlsx" and not raw.startswith(b"PK"):
        raise ValueError("The discovered routine is not a valid XLSX file")

    suffix = ".xlsx" if file_type == "xlsx" else ".pdf"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(raw)
    return path


def _cache_key(section: str, exam_type: str, semester: str, year: Optional[int], include_seat_plan: bool):
    return (section, exam_type, semester, year, bool(include_seat_plan))


def _cache_get(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        created, value = item
        if time.monotonic() - created > CACHE_SECONDS:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key, value):
    with _CACHE_LOCK:
        if len(_CACHE) > 80:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic(), value)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "noticeboard": NOTICEBOARD_URL,
        "supported_exam_types": ["mid", "final"],
        "supported_semesters": ["spring", "summer", "fall"],
        "automatic_only": True,
    }


@app.get("/api/discovery-debug")
def discovery_debug(
    exam_type: str = Query(...),
    semester: str = Query(...),
    year: Optional[int] = Query(None),
):
    """Lightweight diagnostics: shows what the DIU Notice Board collector sees.

    This does not download or parse exam PDFs, so it is safe to use when
    troubleshooting the automatic discovery layer.
    """
    exam_type = exam_type.lower().strip()
    semester = semester.lower().strip()
    if exam_type not in ("mid", "final"):
        raise HTTPException(400, "Exam type must be Mid or Final.")
    if semester not in ("spring", "summer", "fall"):
        raise HTTPException(400, "Semester must be Spring, Summer, or Fall.")
    try:
        items = collect_notice_candidates(semester, year, exam_type)
        return {
            "ok": True,
            "count": len(items),
            "candidates": [
                {"title": x.get("title"), "url": x.get("url"), "score": x.get("score_hint", 0), "context": x.get("context", "")[:500]}
                for x in items[:20]
            ],
        }
    except Exception as exc:
        raise HTTPException(502, f"Notice Board discovery failed: {exc}") from exc


@app.get("/api/auto-analyze")
def auto_analyze(
    section: str = Query(..., min_length=2),
    exam_type: str = Query(...),
    semester: str = Query(...),
    year: Optional[int] = Query(None),
    include_seat_plan: bool = Query(True),
):
    section = section.strip().upper().replace("-", "_")
    exam_type = exam_type.lower().strip()
    semester = semester.lower().strip()

    if not section:
        raise HTTPException(400, "Section is required.")
    if exam_type not in ("mid", "final"):
        raise HTTPException(400, "Exam type must be Mid or Final.")
    if semester not in ("spring", "summer", "fall"):
        raise HTTPException(400, "Semester must be Spring, Summer, or Fall.")
    if year is not None and not 2000 <= year <= 2100:
        raise HTTPException(400, "Invalid academic year.")

    key = _cache_key(section, exam_type, semester, year, include_seat_plan)
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    # Prevent two students from launching identical Playwright crawls at once.
    # The second request waits briefly for the first request to populate cache.
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            for _ in range(90):
                time.sleep(1)
                cached = _cache_get(key)
                if cached is not None:
                    return {**cached, "cached": True}
            raise HTTPException(504, "Another identical lookup is still running. Please try again shortly.")
        _INFLIGHT.add(key)

    routine_path = None
    seat_path = None

    try:
        docs = discover_documents(
            section=section,
            exam_type=exam_type,
            semester=semester,
            year=year,
            include_seat_plan=include_seat_plan,
        )

        routine_path = _write_temp(docs["routine"]["bytes"], docs["routine"]["file_type"])
        if docs.get("seat_plan"):
            try:
                seat_path = _write_temp(docs["seat_plan"]["bytes"], docs["seat_plan"]["file_type"])
            except Exception:
                seat_path = None

        routine = parse_exam_routine(routine_path)
        if not routine.get("exams"):
            raise ValueError("The official routine was found, but no examination rows could be parsed.")

        seat_plan = None
        if seat_path:
            try:
                seat_plan = parse_seat_plan(seat_path)
            except Exception:
                seat_plan = None  # Seat plan is optional — continue with routine only.

        result = build_student_routine(routine, seat_plan, section)

        if not result["exams"]:
            raise ValueError(
                f"No examinations were found for batch {result['batch']} in the selected routine."
            )

        if seat_plan and result["matched_seat_count"] < result["exam_count"]:
            result["warnings"].append(
                f"Seat allocation matched {result['matched_seat_count']} of {result['exam_count']} examinations."
            )
        if docs.get("seat_plan") and not seat_plan:
            result["warnings"].append("Seat plan was found but could not be parsed; showing routine only.")

        result.update(
            {
                "exam_type": exam_type,
                "semester": result.get("semester") or semester.title(),
                "year": result.get("year") or year,
                "seat_plan_found": bool(seat_path),
                "seat_plan_available": result["matched_seat_count"] > 0,
                "source": {
                    "automatic": True,
                    "noticeboard": NOTICEBOARD_URL,
                    "routine_url": docs["routine"]["url"],
                    "routine_title": docs["routine"]["title"],
                    "routine_file_type": docs["routine"]["file_type"],
                    "seat_plan_url": docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
                    "seat_plan_title": docs["seat_plan"]["title"] if docs.get("seat_plan") else None,
                    "seat_plan_file_type": docs["seat_plan"]["file_type"] if docs.get("seat_plan") else None,
                },
                "cached": False,
            }
        )

        _cache_put(key, result)
        return result

    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc).strip() or "Unknown server error"
        raise HTTPException(502, f"Automatic lookup failed: {message}") from exc
    finally:
        for path in (routine_path, seat_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)


@app.post("/api/refresh")
@app.get("/api/refresh")
def refresh_documents(
    exam_type: str = Query("final"),
    semester: str = Query("summer"),
    year: Optional[int] = Query(None),
    section: str = Query("65_L"),
    secret: Optional[str] = Query(None),
):
    """Force a fresh discovery of the official routine + seat plan.

    Designed to be called by GitHub Actions (or any free cron) every 20–30 min.
    This keeps the in-memory document cache warm so students get instant results.

    Protect with REFRESH_SECRET environment variable when deployed.
    """
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        raise HTTPException(403, "Invalid or missing refresh secret.")

    exam_type = exam_type.lower().strip()
    semester = semester.lower().strip()
    section = section.strip().upper().replace("-", "_")

    if exam_type not in ("mid", "final"):
        raise HTTPException(400, "Exam type must be Mid or Final.")
    if semester not in ("spring", "summer", "fall"):
        raise HTTPException(400, "Semester must be Spring, Summer, or Fall.")

    try:
        # Force a live discovery. discover_documents already has its own
        # document-level cache; calling it here warms that cache for everyone.
        docs = discover_documents(
            section=section,
            exam_type=exam_type,
            semester=semester,
            year=year,
            include_seat_plan=True,
        )
        return {
            "ok": True,
            "message": "Documents refreshed successfully.",
            "exam_type": exam_type,
            "semester": semester,
            "year": year,
            "routine_url": docs["routine"]["url"],
            "routine_title": docs["routine"]["title"],
            "seat_plan_found": bool(docs.get("seat_plan")),
            "seat_plan_url": docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
            "from_cache": docs.get("from_cache", False),
            "version": APP_VERSION,
        }
    except Exception as exc:
        raise HTTPException(502, f"Refresh failed: {exc}") from exc


@app.get("/api/cache-status")
def cache_status():
    """Lightweight status for monitoring (used by GitHub Actions / health checks)."""
    with _CACHE_LOCK:
        result_count = len(_CACHE)
    return {
        "ok": True,
        "version": APP_VERSION,
        "result_cache_entries": result_count,
        "cache_ttl_seconds": CACHE_SECONDS,
        "noticeboard": NOTICEBOARD_URL,
    }
