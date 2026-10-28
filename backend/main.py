from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from threading import Lock
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

try:  # package import on Render
    from .noticeboard import NOTICEBOARD_URL, collect_notice_candidates, discover_documents, clear_notice_caches
    from .parser import build_student_routine, parse_exam_routine, parse_seat_plan, normalize_batch, normalize_section
except ImportError:  # direct local run from backend/
    from noticeboard import NOTICEBOARD_URL, collect_notice_candidates, discover_documents, clear_notice_caches
    from parser import build_student_routine, parse_exam_routine, parse_seat_plan, normalize_batch, normalize_section

FIREBASE_DB_URL = os.getenv(
    "FIREBASE_DB_URL",
    "https://examroutine-d5392-default-rtdb.firebaseio.com",
).rstrip("/")
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
REFRESH_SECRET = os.getenv("REFRESH_SECRET", "").strip()
APP_VERSION = "10.0.0"
MAX_FILE_SIZE = 25 * 1024 * 1024

# Keep third-party PDF warnings out of production logs.
for _name in ("pdfminer", "pdfminer.pdfpage", "pdfplumber"):
    logging.getLogger(_name).setLevel(logging.ERROR)

app = FastAPI(title="Exam Routine Generator API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory result cache. Prevents students from launching repeated
# notice-board crawls. Render free tier clears it on sleep/restart — that is OK
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


def _firebase_reference(path: str):
    """Return an Admin SDK database reference, or None if credentials are not configured."""
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, db

        try:
            firebase_admin.get_app()
        except ValueError:
            service_account = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            firebase_admin.initialize_app(
                credentials.Certificate(service_account),
                {"databaseURL": FIREBASE_DB_URL},
            )
        return db.reference(path)
    except Exception as exc:
        logging.exception("Firebase Admin initialization failed")
        raise RuntimeError(f"Firebase Admin initialization failed: {exc}") from exc


def _firebase_set(path: str, value) -> bool:
    ref = _firebase_reference(path)
    if ref is None:
        logging.warning("Firebase write skipped: FIREBASE_SERVICE_ACCOUNT_JSON is not configured")
        return False
    ref.set(value)
    return True


def _firebase_get(path: str, default=None):
    ref = _firebase_reference(path)
    if ref is None:
        return default
    value = ref.get()
    return default if value is None else value


def _save_routine(section: str, exam_type: str, semester: str, year: int, result: dict) -> None:
    """Write structured session data and legacy flat data for older clients."""
    _firebase_set(f"routines_v2/{section}/{exam_type}/{semester.lower()}/{year}", result)
    # Legacy key is retained for older deployed frontends; structured data is authoritative.
    _firebase_set(f"routines/{section}", result)


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

    # Prevent two students from launching identical crawls at once.
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
        try:
            _save_routine(section, exam_type, semester, int(result.get("year") or year or datetime.now().year), result)
        except Exception:
            logging.exception("Firebase sync failed for section %s", section)

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
def refresh_documents(
    authorization: Optional[str] = Header(None),
    semester: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    exam_type: Optional[str] = Query(None),
):
    """Discover current-year Mid/Final routines and sync each verified session.

    GitHub Actions should send Authorization: Bearer <REFRESH_SECRET>.
    Optional query parameters restrict the refresh to one session.
    """
    if not REFRESH_SECRET:
        raise HTTPException(503, "Refresh is disabled until REFRESH_SECRET is configured in Render.")
    supplied = (authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    if not supplied or supplied != REFRESH_SECRET:
        raise HTTPException(403, "Invalid or missing refresh authorization.")

    if semester and semester.lower().strip() not in ("spring", "summer", "fall"):
        raise HTTPException(400, "Semester must be Spring, Summer, or Fall.")
    if exam_type and exam_type.lower().strip() not in ("mid", "final"):
        raise HTTPException(400, "Exam type must be Mid or Final.")
    if year is not None and not 2000 <= year <= 2100:
        raise HTTPException(400, "Invalid academic year.")
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(503, "Firebase writes are not configured. Set FIREBASE_SERVICE_ACCOUNT_JSON in Render.")

    current_year = year or datetime.now().year
    semesters = [semester.lower().strip()] if semester else ["spring", "summer", "fall"]
    exam_types = [exam_type.lower().strip()] if exam_type else ["mid", "final"]
    processed = []
    failures = []

    # Clear stale discovery caches so this scheduled refresh checks the live notice board.
    clear_notice_caches()
    for sem in semesters:
        for kind in exam_types:
            routine_path = seat_path = None
            try:
                # Empty section asks discovery to validate the document at batch level;
                # the parser is then used to enumerate the actual batches/sections.
                docs = discover_documents(
                    section="", exam_type=kind, semester=sem, year=current_year,
                    include_seat_plan=True, force_refresh=True,
                )
                routine_path = _write_temp(docs["routine"]["bytes"], docs["routine"]["file_type"])
                routine_data = parse_exam_routine(routine_path)
                if not routine_data.get("exams"):
                    raise ValueError("Routine was discovered but contained no parsed exams")

                seat_data = None
                batches = sorted({normalize_batch(x.get("batch")) for x in routine_data["exams"] if normalize_batch(x.get("batch"))})
                if docs.get("seat_plan"):
                    try:
                        seat_path = _write_temp(docs["seat_plan"]["bytes"], docs["seat_plan"]["file_type"])
                        seat_data = parse_seat_plan(seat_path)
                    except Exception as seat_exc:
                        logging.warning("Seat-plan parse failed for %s %s %s: %s", sem, current_year, kind, seat_exc)
                        seat_data = None

                # Use sections present in the seat plan plus known public cache keys. If no seat plan
                # exists, known sections can still receive a batch-level routine without seat data.
                sections = set()
                for allocation in (seat_data or []):
                    sec = normalize_section(allocation.get("section"))
                    if re.fullmatch(r"\d{2,3}_[A-Z0-9]+", sec):
                        sections.add(sec)
                try:
                    existing = _firebase_get("routines", {}) or {}
                    for sec in existing:
                        normalized = normalize_section(sec)
                        if re.fullmatch(r"\d{2,3}_[A-Z0-9]+", normalized):
                            if normalize_batch(normalized) in batches:
                                sections.add(normalized)
                except Exception as firebase_exc:
                    logging.warning("Could not read existing Firebase section index: %s", firebase_exc)

                # If seat allocation is absent, create the conventional A-section seed per batch;
                # student-triggered lookups will populate additional sections when requested.
                if not sections:
                    sections.update(f"{batch}_A" for batch in batches)

                routine_url = docs["routine"]["url"]
                seat_doc = docs.get("seat_plan")
                synced_count = 0
                for sec in sorted(sections):
                    result = build_student_routine(routine_data, seat_data, sec)
                    if not result.get("exams"):
                        continue
                    result.update({
                        "exam_type": kind,
                        "semester": result.get("semester") or sem.title(),
                        "year": result.get("year") or current_year,
                        "seat_plan_found": bool(seat_data),
                        "seat_plan_available": result.get("matched_seat_count", 0) > 0,
                        "source": {
                            "automatic": True,
                            "noticeboard": NOTICEBOARD_URL,
                            "routine_url": routine_url,
                            "routine_title": docs["routine"].get("title"),
                            "routine_file_type": docs["routine"].get("file_type"),
                            "seat_plan_url": seat_doc.get("url") if seat_doc else None,
                            "seat_plan_title": seat_doc.get("title") if seat_doc else None,
                            "seat_plan_file_type": seat_doc.get("file_type") if seat_doc else None,
                        },
                        "cached": False,
                    })
                    _save_routine(sec, kind, sem, int(result.get("year") or current_year), result)
                    synced_count += 1

                session_key = f"{sem}_{current_year}_{kind}"
                _firebase_set(f"metadata/sessions/{session_key}", {
                    "routine_url": routine_url,
                    "seat_plan_url": seat_doc.get("url") if seat_doc else None,
                    "semester": sem.title(), "year": current_year, "exam_type": kind,
                    "synced_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "sections_synced": synced_count,
                })
                # Preserve legacy metadata fields for the existing frontend's source display.
                _firebase_set("metadata/routine_url", routine_url)
                _firebase_set("metadata/seat_plan_url", seat_doc.get("url") if seat_doc else None)
                processed.append({"semester": sem, "year": current_year, "exam_type": kind,
                                  "routine_url": routine_url, "sections_synced": synced_count})
            except Exception as exc:
                logging.exception("Refresh failed for %s %s %s", sem, current_year, kind)
                failures.append({"semester": sem, "year": current_year, "exam_type": kind, "error": str(exc)[:500]})
            finally:
                for path in (routine_path, seat_path):
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass

    if not processed and failures:
        raise HTTPException(502, {"message": "No exam routines were synced.", "failures": failures})
    return {"ok": True, "version": APP_VERSION, "processed": processed, "failures": failures}


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
