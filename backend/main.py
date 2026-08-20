from __future__ import annotations
"""
ExamRoutine backend — stable strategy

1. PRIMARY cache:  /routines/{SECTION}.json     (flat, proven, frontend reads this)
2. OPTIONAL v2:    /routines_v2/{SECTION}/{exam}/{semester}/{year}.json
3. METADATA:       /metadata.json  (routine_url, seat_plan_url, section_count)
4. REFRESH:        rebuilds ALL sections; auto-forces if cache looks empty/thin
5. AUTO-ANALYZE:   serves one student; upserts only that section (never deletes others)
"""
import requests
import re
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

for _name in ("pdfminer", "pdfminer.pdfpage", "pdfplumber"):
    logging.getLogger(_name).setLevel(logging.ERROR)

APP_VERSION = "8.2.0"

FIREBASE_BASE_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines"
FIREBASE_V2_BASE = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines_v2"
FIREBASE_META_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/metadata.json"
FIREBASE_SHALLOW_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines.json?shallow=true"
FIREBASE_SECRET = os.environ.get(
    "FIREBASE_SECRET", "TLC3hRT91gy6h78O2EwQ2NLxbNwRTTM4IWrlzd5C"
).strip()

MAX_FILE_SIZE = 25 * 1024 * 1024
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "").strip()

# If fewer sections than this exist in Firebase, refresh auto-forces a full rebuild
MIN_HEALTHY_SECTION_COUNT = 30

# Reject clearly fake / typo sections from being cached (e.g. 65_Z from bad searches)
# Real DIU sections use letters A–Z but Z alone as a single-letter section is rare;
# we still allow any A-Z pattern that appears in the official PDF. The guard below
# only blocks writes when the section was NOT discovered from the PDF (auto-analyze
# of a typo that still returned batch rows).
SECTION_RE = re.compile(r"^\d{2,3}_[A-Z0-9]+$")

app = FastAPI(title="Exam Routine Generator API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = Lock()
_INFLIGHT_LOCK = Lock()
_INFLIGHT: set[tuple] = set()
CACHE_SECONDS = 1800


def _auth_url(url: str) -> str:
    if not FIREBASE_SECRET:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}auth={FIREBASE_SECRET}"


def firebase_get(url: str, timeout: int = 8):
    try:
        r = requests.get(_auth_url(url) if "auth=" not in url else url, timeout=timeout)
        if not r.ok:
            return None
        data = r.json()
        return data
    except Exception:
        return None


def firebase_put(url: str, payload: dict, timeout: int = 10) -> bool:
    try:
        r = requests.put(_auth_url(url), json=payload, timeout=timeout)
        return r.ok
    except Exception as e:
        print(f"Firebase PUT failed {url}: {e}")
        return False


def count_cached_sections() -> int:
    data = firebase_get(FIREBASE_SHALLOW_URL)
    if not isinstance(data, dict):
        return 0
    return sum(1 for k in data if SECTION_RE.match(str(k).upper()))


def write_section_cache(
    section: str,
    result: dict,
    exam_type: str,
    semester: str,
    year,
) -> None:
    """Upsert one section. Never deletes other sections. Never nests under legacy key."""
    section = section.strip().upper().replace("-", "_")
    exam_type = (exam_type or "final").lower().strip()
    semester = (semester or "summer").lower().strip()
    year_s = str(year or result.get("year") or 2026)

    legacy_url = f"{FIREBASE_BASE_URL.rstrip('/')}/{section}.json"
    v2_url = f"{FIREBASE_V2_BASE.rstrip('/')}/{section}/{exam_type}/{semester}/{year_s}.json"

    ok1 = firebase_put(legacy_url, result)
    ok2 = firebase_put(v2_url, result)
    print(f"Firebase upsert {section}: legacy={'ok' if ok1 else 'fail'} v2={'ok' if ok2 else 'fail'}")


def write_metadata(
    routine_url: Optional[str],
    seat_plan_url: Optional[str],
    exam_type: str = "",
    semester: str = "",
    year=None,
    section_count: Optional[int] = None,
) -> None:
    payload = {
        "routine_url": routine_url or "",
        "seat_plan_url": seat_plan_url or "",
        "exam_type": exam_type or "",
        "semester": semester or "",
        "year": year or "",
        "updated_at": int(time.time()),
    }
    if section_count is not None:
        payload["section_count"] = section_count
    firebase_put(FIREBASE_META_URL, payload)


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


def _cache_key(section, exam_type, semester, year, include_seat_plan):
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


def detect_term():
    """Best-effort semester/exam/year from the notice board HTML."""
    try:
        resp = requests.get(NOTICEBOARD_URL, timeout=10)
        match = re.search(r">([^<]*CSE[^<]*Routine[^<]*)<", resp.text, re.IGNORECASE)
        if match:
            title = match.group(1).lower()
            exam_type = "mid" if "mid" in title else "final"
            if "spring" in title:
                semester = "spring"
            elif "fall" in title:
                semester = "fall"
            else:
                semester = "summer"
            year_match = re.search(r"\b(20\d{2})\b", title)
            year = int(year_match.group(1)) if year_match else 2026
            return exam_type, semester, year
    except Exception as e:
        print(f"detect_term failed: {e}")
    return "final", "summer", 2026


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "noticeboard": NOTICEBOARD_URL,
        "supported_exam_types": ["mid", "final"],
        "supported_semesters": ["spring", "summer", "fall"],
        "automatic_only": True,
        "strategy": "legacy-primary + optional-v2 + self-heal-refresh",
    }


@app.get("/api/discovery-debug")
def discovery_debug(
    exam_type: str = Query(...),
    semester: str = Query(...),
    year: Optional[int] = Query(None),
):
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
                {
                    "title": x.get("title"),
                    "url": x.get("url"),
                    "score": x.get("score_hint", 0),
                    "context": x.get("context", "")[:500],
                }
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

    if not SECTION_RE.match(section):
        raise HTTPException(400, "Enter a valid section such as 65_L or 65_N.")
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

    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            for _ in range(90):
                time.sleep(1)
                cached = _cache_get(key)
                if cached is not None:
                    return {**cached, "cached": True}
            raise HTTPException(
                504, "Another identical lookup is still running. Please try again shortly."
            )
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
                seat_plan = None

        result = build_student_routine(routine, seat_plan, section)

        if not result["exams"]:
            raise ValueError(
                f"No examinations were found for batch {result['batch']} in the selected routine."
            )

        # Do not cache typo sections that only got generic batch rows with zero seat matches
        # unless the section string appears in the official documents.
        combined = str(routine) + " " + str(seat_plan)
        section_in_docs = bool(re.search(re.escape(section).replace("_", "[-_]"), combined, re.I))
        if (not section_in_docs) and result.get("matched_seat_count", 0) == 0:
            # Still return data to the student, but do NOT pollute Firebase
            result.update(
                {
                    "exam_type": exam_type,
                    "semester": result.get("semester") or semester.title(),
                    "year": result.get("year") or year,
                    "seat_plan_found": bool(seat_path),
                    "seat_plan_available": False,
                    "source": {
                        "automatic": True,
                        "noticeboard": NOTICEBOARD_URL,
                        "routine_url": docs["routine"]["url"],
                        "routine_title": docs["routine"]["title"],
                        "routine_file_type": docs["routine"]["file_type"],
                        "seat_plan_url": docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
                        "seat_plan_title": docs["seat_plan"]["title"] if docs.get("seat_plan") else None,
                        "seat_plan_file_type": docs["seat_plan"]["file_type"]
                        if docs.get("seat_plan")
                        else None,
                    },
                    "cached": False,
                    "warnings": list(result.get("warnings") or [])
                    + [
                        f"Section {section} was not found in the official documents; result not saved to cache."
                    ],
                }
            )
            _cache_put(key, result)
            return result

        if seat_plan and result["matched_seat_count"] < result["exam_count"]:
            result["warnings"].append(
                f"Seat allocation matched {result['matched_seat_count']} of {result['exam_count']} examinations."
            )
        if docs.get("seat_plan") and not seat_plan:
            result["warnings"].append(
                "Seat plan was found but could not be parsed; showing routine only."
            )

        result_year = result.get("year") or year or 2026
        result.update(
            {
                "exam_type": exam_type,
                "semester": result.get("semester") or semester.title(),
                "year": result_year,
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
                    "seat_plan_file_type": docs["seat_plan"]["file_type"]
                    if docs.get("seat_plan")
                    else None,
                },
                "cached": False,
            }
        )

        # Upsert this section only — never replaces the whole /routines tree
        try:
            write_section_cache(section, result, exam_type, semester, result_year)
            # Keep metadata PDF links fresh when students hit live lookup
            write_metadata(
                docs["routine"]["url"],
                docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
                exam_type,
                semester,
                result_year,
            )
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
def refresh_documents(
    secret: Optional[str] = Query(None),
    force: bool = Query(
        False,
        description="Rebuild all sections even if routine PDF URL is unchanged.",
    ),
):
    """Rebuild Firebase cache for every section found in the official PDFs.

    Strategy:
    - If force=true → always rebuild
    - If cached section count < MIN_HEALTHY_SECTION_COUNT → auto-force (self-heal)
    - Else if PDF URL unchanged → skip (save Render minutes)
    - Writes are per-section upserts only (never delete the whole tree)
    """
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        raise HTTPException(403, "Invalid or missing refresh secret.")

    exam_type, semester, year = detect_term()
    print(f"Refresh term: {semester} {year} {exam_type}")

    existing_count = count_cached_sections()
    auto_force = existing_count < MIN_HEALTHY_SECTION_COUNT
    if auto_force:
        print(
            f"Cache thin ({existing_count} < {MIN_HEALTHY_SECTION_COUNT}) — auto-forcing full rebuild"
        )
        force = True

    last_meta = firebase_get(FIREBASE_META_URL) or {}
    if not isinstance(last_meta, dict):
        last_meta = {}
    last_routine_url = last_meta.get("routine_url") or ""

    routine_path = None
    seat_path = None

    try:
        docs = discover_documents(
            section="65_L",
            exam_type=exam_type,
            semester=semester,
            year=year,
            include_seat_plan=True,
        )
        current_routine_url = docs["routine"]["url"]

        if (not force) and current_routine_url and current_routine_url == last_routine_url:
            return {
                "ok": True,
                "message": "Latest routine is already synced. Doing nothing.",
                "routine_url": current_routine_url,
                "sections_cached": existing_count,
                "hint": "Pass force=true to rebuild, or wait until cache drops below threshold.",
            }

        print(f"Processing routine: {current_routine_url} force={force}")
        routine_path = _write_temp(docs["routine"]["bytes"], docs["routine"]["file_type"])
        if docs.get("seat_plan"):
            try:
                seat_path = _write_temp(docs["seat_plan"]["bytes"], docs["seat_plan"]["file_type"])
            except Exception:
                seat_path = None

        routine_data = parse_exam_routine(routine_path)
        seat_plan_data = parse_seat_plan(seat_path) if seat_path else None

        combined_data = str(routine_data) + " " + str(seat_plan_data)
        raw_sections = re.findall(r"\d{2,3}[-_][A-Z0-9]+", combined_data, flags=re.I)
        found_sections = {sec.replace("-", "_").upper() for sec in raw_sections}
        target_sections = sorted(s for s in found_sections if SECTION_RE.match(s))
        print(f"Discovered {len(target_sections)} sections from PDFs")

        synced = 0
        for sec in target_sections:
            result = build_student_routine(routine_data, seat_plan_data, sec)
            if not result.get("exams"):
                continue

            result_year = result.get("year") or year
            result.update(
                {
                    "exam_type": exam_type,
                    "semester": result.get("semester") or semester.title(),
                    "year": result_year,
                    "seat_plan_found": bool(seat_path),
                    "seat_plan_available": result.get("matched_seat_count", 0) > 0,
                    "source": {
                        "automatic": True,
                        "noticeboard": NOTICEBOARD_URL,
                        "routine_url": docs["routine"]["url"],
                        "routine_title": docs["routine"]["title"],
                        "seat_plan_url": docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
                        "seat_plan_title": docs["seat_plan"]["title"] if docs.get("seat_plan") else None,
                    },
                    "cached": True,
                }
            )
            write_section_cache(sec, result, exam_type, semester, result_year)
            synced += 1

        write_metadata(
            current_routine_url,
            docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
            exam_type,
            semester,
            year,
            section_count=synced,
        )

        return {
            "ok": True,
            "message": f"Success! Synced {synced}/{len(target_sections)} sections.",
            "routine_url": current_routine_url,
            "sections_discovered": len(target_sections),
            "sections_synced": synced,
            "sections_cached_before": existing_count,
            "force": force,
            "auto_force": auto_force,
        }

    except Exception as exc:
        raise HTTPException(502, f"Refresh failed: {exc}") from exc
    finally:
        for path in (routine_path, seat_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


@app.get("/api/cache-status")
def cache_status():
    with _CACHE_LOCK:
        result_count = len(_CACHE)
    fb_count = count_cached_sections()
    meta = firebase_get(FIREBASE_META_URL) or {}
    return {
        "ok": True,
        "version": APP_VERSION,
        "result_cache_entries": result_count,
        "cache_ttl_seconds": CACHE_SECONDS,
        "firebase_section_count": fb_count,
        "firebase_healthy": fb_count >= MIN_HEALTHY_SECTION_COUNT,
        "metadata": meta if isinstance(meta, dict) else {},
        "noticeboard": NOTICEBOARD_URL,
    }
