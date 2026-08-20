from __future__ import annotations
import requests
import re
FIREBASE_BASE_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines"
FIREBASE_SECRET = "TLC3hRT91gy6h78O2EwQ2NLxbNwRTTM4IWrlzd5C"
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
        try:
            section_db_url = f"{FIREBASE_BASE_URL}/{section}.json"
            requests.put(section_db_url, json=result, timeout=5)
            print(f"Successfully synced section {section} to Firebase!")
        except Exception as e:
            print(f"Firebase sync failed for {section}: {e}")
        
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
def refresh_documents(secret: Optional[str] = Query(None)):
    """Check for new routines autonomously, dynamically find ALL sections, and sync to Firebase."""
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        raise HTTPException(403, "Invalid or missing refresh secret.")

    # --- THE AUTONOMOUS BRAIN: Automatically detect the current semester ---
    try:
        resp = requests.get(NOTICEBOARD_URL, timeout=10)
        # Find the first notice title containing both "CSE" and "Routine"
        match = re.search(r'>([^<]*CSE[^<]*Routine[^<]*)<', resp.text, re.IGNORECASE)
        
        if match:
            title = match.group(1).lower()
            exam_type = "mid" if "mid" in title else "final"
            
            if "spring" in title: semester = "spring"
            elif "fall" in title: semester = "fall"
            else: semester = "summer"
            
            year_match = re.search(r'\b(20\d{2})\b', title)
            year = int(year_match.group(1)) if year_match else 2026
            
            print(f"🤖 Auto-detected: {semester.title()} {year} {exam_type.title()} Exams")
        else:
            exam_type, semester, year = "final", "summer", 2026
    except Exception:
        print("Could not auto-detect semester. Using defaults.")
        exam_type, semester, year = "final", "summer", 2026
    # ---------------------------------------------------------------------

    # 1. Fetch our bookmark to see what we last processed (Added security key here!)
    metadata_url = f"https://examroutine-d5392-default-rtdb.firebaseio.com/metadata.json?auth={FIREBASE_SECRET}"
    try:
        meta_resp = requests.get(metadata_url, timeout=5)
        last_metadata = meta_resp.json() or {}
        last_routine_url = last_metadata.get("routine_url", "")
    except Exception:
        last_routine_url = ""

    try:
        # 2. Check the Notice Board for the latest document using our Auto-Detected variables
        docs = discover_documents(
            section="65_L", # Dummy section just to trigger discovery
            exam_type=exam_type,
            semester=semester,
            year=year,
            include_seat_plan=True,
        )
        
        current_routine_url = docs["routine"]["url"]

        # 3. THE CATCH: Stop if the routine hasn't changed
        if current_routine_url == last_routine_url:
            print("No new routine found. Doing nothing.")
            return {
                "ok": True, 
                "message": "Latest routine is already synced. Doing nothing.",
                "routine_url": current_routine_url
            }

        # 4. Parse the new documents
        print(f"New routine detected: {current_routine_url}. Processing...")
        routine_path = _write_temp(docs["routine"]["bytes"], docs["routine"]["file_type"])
        seat_path = None
        if docs.get("seat_plan"):
            try:
                seat_path = _write_temp(docs["seat_plan"]["bytes"], docs["seat_plan"]["file_type"])
            except Exception:
                pass

        routine_data = parse_exam_routine(routine_path)
        seat_plan_data = parse_seat_plan(seat_path) if seat_path else None

        # --- THE MAGIC: DYNAMICALLY FIND EVERY SECTION ---
        combined_data = str(routine_data) + " " + str(seat_plan_data)
        raw_sections = re.findall(r'\d{2,3}[-_][A-Z0-9]+', combined_data)
        found_sections = {sec.replace('-', '_') for sec in raw_sections}
        target_sections = sorted(list(found_sections))
        print(f"Discovered {len(target_sections)} unique sections in the documents.")
        # -------------------------------------------------

        # 5. Build and push the routine for every single discovered section
        for sec in target_sections:
            result = build_student_routine(routine_data, seat_plan_data, sec)
            
            if result.get("exams") and len(result["exams"]) > 0:
                result.update({
                    "exam_type": exam_type,
                    "semester": result.get("semester") or semester.title(),
                    "year": result.get("year") or year,
                    "seat_plan_found": bool(seat_path),
                    "seat_plan_available": result.get("matched_seat_count", 0) > 0,
                    "cached": True, 
                })
                
                section_db_url = f"{FIREBASE_BASE_URL}/{sec}.json?auth={FIREBASE_SECRET}"
                requests.put(section_db_url, json=result, timeout=5)
                print(f"Background Sync: Updated {sec}")

        # 6. Update our bookmark in Firebase
        new_metadata = {
            "routine_url": current_routine_url,
            "seat_plan_url": docs["seat_plan"]["url"] if docs.get("seat_plan") else None
        }
        requests.put(metadata_url, json=new_metadata, timeout=5)

        return {
            "ok": True, 
            "message": f"Success! Synced {len(target_sections)} sections to Firebase.",
            "routine_url": current_routine_url
        }

    except Exception as exc:
        raise HTTPException(502, f"Refresh failed: {exc}") from exc
    finally:
        for path in (locals().get('routine_path'), locals().get('seat_path')):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
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
