from __future__ import annotations
"""
ExamRoutine backend — stable strategy

1. PRIMARY cache:  /routines/{SECTION}.json     (flat, proven, frontend reads this)
2. OPTIONAL v2:    /routines_v2/{SECTION}/{exam}/{semester}/{year}.json
3. METADATA:       /metadata.json  (routine_url, routine_hash, seat_plan_url, section_count)
4. REFRESH:        rebuilds ALL sections ONLY when content hash changes; auto-forces if thin
5. AUTO-ANALYZE:   serves one student; upserts only that section (never deletes others)
"""
import hashlib
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

APP_VERSION = "8.4.1"

FIREBASE_BASE_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines"
FIREBASE_V2_BASE = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines_v2"
FIREBASE_META_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/metadata.json"
FIREBASE_SHALLOW_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/routines.json?shallow=true"
# Global catalogue of every exam term ever synced (never overwritten as a whole).
FIREBASE_TERMS_URL = "https://examroutine-d5392-default-rtdb.firebaseio.com/terms.json"
# NEVER hardcode the secret. Set FIREBASE_SECRET in Render environment variables only.
FIREBASE_SECRET = os.environ.get("FIREBASE_SECRET", "").strip()

MAX_FILE_SIZE = 25 * 1024 * 1024
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "").strip()

# If fewer sections than this exist in Firebase, refresh auto-forces a full rebuild
MIN_HEALTHY_SECTION_COUNT = 30

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
        # Prefer authenticated read when secret is set (works even if public read is off).
        target = _auth_url(url) if "auth=" not in url else url
        r = requests.get(target, timeout=timeout)
        if not r.ok:
            # Fallback: public read (rules may allow unauthenticated GET on some paths)
            if FIREBASE_SECRET and "auth=" in target:
                r2 = requests.get(url, timeout=timeout)
                if r2.ok:
                    return r2.json()
            return None
        data = r.json()
        return data
    except Exception:
        return None


def firebase_put(url: str, payload: dict, timeout: int = 10) -> bool:
    if not FIREBASE_SECRET:
        print(
            "Firebase PUT skipped: FIREBASE_SECRET is empty. "
            "Set it in Render Environment Variables (Realtime Database secret)."
        )
        return False
    try:
        r = requests.put(_auth_url(url), json=payload, timeout=timeout)
        if not r.ok:
            print(f"Firebase PUT failed {url}: HTTP {r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"Firebase PUT failed {url}: {e}")
        return False


def count_cached_sections() -> int:
    data = firebase_get(FIREBASE_SHALLOW_URL)
    if not isinstance(data, dict):
        return 0
    return sum(1 for k in data if SECTION_RE.match(str(k).upper()))


def write_term_index(
    exam_type: str,
    semester: str,
    year,
    routine_url: Optional[str] = None,
    routine_title: Optional[str] = None,
    section_count: Optional[int] = None,
) -> None:
    """Register this exam term in the permanent history catalogue.

    Path: /terms/{exam_type}/{semester}/{year}
    Old terms are never deleted — only upserted when re-synced.
    """
    exam_type = (exam_type or "final").lower().strip()
    semester = (semester or "summer").lower().strip()
    year_s = str(year or 2026)
    term_url = (
        "https://examroutine-d5392-default-rtdb.firebaseio.com/terms"
        f"/{exam_type}/{semester}/{year_s}.json"
    )
    payload = {
        "exam_type": exam_type,
        "semester": semester,
        "year": int(year_s) if str(year_s).isdigit() else year_s,
        "routine_url": routine_url or "",
        "routine_title": routine_title or "",
        "updated_at": int(time.time()),
    }
    if section_count is not None:
        payload["section_count"] = section_count
    ok = firebase_put(term_url, payload)
    print(f"Term index upsert {exam_type}/{semester}/{year_s}: {'ok' if ok else 'fail'}")


def write_section_cache(
    section: str,
    result: dict,
    exam_type: str,
    semester: str,
    year,
) -> None:
    """Write per-section data.

    - Legacy flat key is always the *latest* view of that section (overwritten).
    - v2 key is permanent history: /routines_v2/{section}/{exam}/{semester}/{year}
      Different terms never overwrite each other.
    """
    section = section.strip().upper().replace("-", "_")
    exam_type = (exam_type or "final").lower().strip()
    semester = (semester or "summer").lower().strip()
    year_s = str(year or result.get("year") or 2026)

    legacy_url = f"{FIREBASE_BASE_URL.rstrip('/')}/{section}.json"
    v2_url = f"{FIREBASE_V2_BASE.rstrip('/')}/{section}/{exam_type}/{semester}/{year_s}.json"

    ok1 = firebase_put(legacy_url, result)
    ok2 = firebase_put(v2_url, result)
    print(f"Firebase upsert {section}: legacy={'ok' if ok1 else 'fail'} v2={'ok' if ok2 else 'fail'}")


def content_fingerprint(raw: bytes) -> str:
    """Cheap, stable content hash so we skip re-parse when the PDF is unchanged."""
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()


def write_metadata(
    routine_url: Optional[str],
    seat_plan_url: Optional[str],
    exam_type: str = "",
    semester: str = "",
    year=None,
    section_count: Optional[int] = None,
    routine_hash: Optional[str] = None,
    routine_size: Optional[int] = None,
    routine_title: Optional[str] = None,
    seat_plan_hash: Optional[str] = None,
    seat_plan_size: Optional[int] = None,
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
    if routine_hash is not None:
        payload["routine_hash"] = routine_hash
    if routine_size is not None:
        payload["routine_size"] = routine_size
    if routine_title is not None:
        payload["routine_title"] = routine_title
    if seat_plan_hash is not None:
        payload["seat_plan_hash"] = seat_plan_hash
    if seat_plan_size is not None:
        payload["seat_plan_size"] = seat_plan_size
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
    """Pick the newest CSE *exam* routine (mid/final), not class routine.

    Scans main notice board + CSE department list. Prefers titles that
    contain Mid/Final + Examination/Exam Routine over Class Routine.
    """
    urls = [
        NOTICEBOARD_URL,
        "https://webbackend.daffodilvarsity.edu.bd/noticeboard",
        "https://webbackend.daffodilvarsity.edu.bd/department-notice/cse",
    ]
    candidates = []
    title_re = re.compile(
        r"(CSE[^<]{0,120}(?:Mid(?:term)?|Final)[^<]{0,80}(?:Examination\s*)?Routine[^<]{0,40})"
        r"|(?:Mid(?:term)?|Final)\s*Examination\s*Routine[^<]{0,40}CSE[^<]{0,40}"
        r"|(Updated\s+CSE[^<]{0,100}(?:Mid|Final)[^<]{0,60}Routine[^<]{0,40})",
        re.I,
    )
    for url in urls:
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if not resp.ok:
                continue
            text = resp.text
            for m in title_re.finditer(text):
                title = re.sub(r"\s+", " ", m.group(0)).strip().lower()
                if "class routine" in title and "exam" not in title:
                    continue
                if "improvement" in title and "mid" not in title and "final" not in title:
                    continue
                exam_type = "mid" if re.search(r"\bmid(?:term)?\b", title) else "final"
                if "spring" in title:
                    semester = "spring"
                elif "fall" in title or "autumn" in title:
                    semester = "fall"
                else:
                    semester = "summer"
                year_match = re.search(r"\b(20\d{2})\b", title)
                year = int(year_match.group(1)) if year_match else 2026
                # Score: prefer current academic cycle (Fall when published),
                # exam routines over class routines, updated only within same term.
                score = 0
                score += year * 1000
                # Fall is current in Oct–Dec cycle; strongly prefer newer semester
                score += {"fall": 300, "summer": 200, "spring": 100}.get(semester, 0)
                if "exam" in title or "examination" in title:
                    score += 500
                if "class routine" in title:
                    score -= 2000
                if "improvement" in title:
                    score -= 400  # improvement is not the main mid/final routine
                if "updated" in title:
                    score += 50  # small boost within same term only
                candidates.append((score, exam_type, semester, year, title[:120]))
        except Exception as e:
            print(f"detect_term fetch failed {url}: {e}")

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0]
        print(f"detect_term chose: {best[1]} {best[2]} {best[3]} score={best[0]} ({best[4]})")
        return best[1], best[2], best[3]

    print("detect_term: no CSE exam routine title found, defaulting fall mid 2026")
    return "mid", "fall", 2026


@app.get("/")
def root():
    """Render / uptime pings often hit /. Avoid noisy 404s."""
    return {
        "ok": True,
        "service": "ExamRoutine API",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/api/health",
        "cache_status": "/api/cache-status",
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "noticeboard": NOTICEBOARD_URL,
        "supported_exam_types": ["mid", "final"],
        "supported_semesters": ["spring", "summer", "fall"],
        "automatic_only": True,
        "strategy": "legacy-primary + optional-v2 + content-hash-skip + strict-cse",
        "firebase_secret_configured": bool(FIREBASE_SECRET),
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

        try:
            write_section_cache(section, result, exam_type, semester, result_year)
            r_bytes = docs["routine"].get("bytes") or b""
            s_bytes = (docs.get("seat_plan") or {}).get("bytes") or b""
            write_metadata(
                docs["routine"]["url"],
                docs["seat_plan"]["url"] if docs.get("seat_plan") else None,
                exam_type,
                semester,
                result_year,
                routine_hash=content_fingerprint(r_bytes) if r_bytes else None,
                routine_size=len(r_bytes) if r_bytes else None,
                routine_title=docs["routine"].get("title"),
                seat_plan_hash=content_fingerprint(s_bytes) if s_bytes else None,
                seat_plan_size=len(s_bytes) if s_bytes else None,
            )
            write_term_index(
                exam_type,
                semester,
                result_year,
                routine_url=docs["routine"]["url"],
                routine_title=docs["routine"].get("title"),
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
        description="Rebuild all sections even if routine content hash is unchanged.",
    ),
    exam_type: Optional[str] = Query(
        None, description="Optional override: mid or final (skips auto detect)."
    ),
    semester: Optional[str] = Query(
        None, description="Optional override: spring, summer, or fall."
    ),
    year: Optional[int] = Query(None, description="Optional academic year override."),
):
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        raise HTTPException(403, "Invalid or missing refresh secret.")

    if exam_type and semester and year:
        exam_type = exam_type.lower().strip()
        semester = semester.lower().strip()
        if exam_type not in ("mid", "final") or semester not in ("spring", "summer", "fall"):
            raise HTTPException(400, "Invalid exam_type or semester override.")
        print(f"Refresh term (manual override): {semester} {year} {exam_type}")
    else:
        exam_type, semester, year = detect_term()
        print(f"Refresh term (auto): {semester} {year} {exam_type}")

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
    last_routine_hash = last_meta.get("routine_hash") or ""
    last_routine_size = last_meta.get("routine_size")

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
        routine_bytes = docs["routine"]["bytes"]
        current_hash = content_fingerprint(routine_bytes)
        current_size = len(routine_bytes) if routine_bytes else 0
        routine_title = docs["routine"].get("title") or ""

        seat_bytes = None
        seat_hash = ""
        seat_size = 0
        if docs.get("seat_plan") and docs["seat_plan"].get("bytes"):
            seat_bytes = docs["seat_plan"]["bytes"]
            seat_hash = content_fingerprint(seat_bytes)
            seat_size = len(seat_bytes)

        # Skip when content is identical (hash + size), even if URL text differs.
        same_content = (
            bool(current_hash)
            and bool(last_routine_hash)
            and current_hash == last_routine_hash
            and (last_routine_size is None or int(last_routine_size) == current_size)
        )
        same_url = bool(current_routine_url) and current_routine_url == last_routine_url

        if (not force) and (same_content or (same_url and not last_routine_hash)):
            print(
                f"Skip refresh: same_content={same_content} same_url={same_url} "
                f"hash={current_hash[:12]}… size={current_size}"
            )
            return {
                "ok": True,
                "message": "No new routine published. Content hash matches — skipped download/parse.",
                "routine_url": current_routine_url,
                "routine_hash": current_hash,
                "routine_size": current_size,
                "sections_cached": existing_count,
                "skipped": True,
                "hint": "Pass force=true to rebuild anyway.",
            }

        print(
            f"Processing NEW/CHANGED routine: {current_routine_url} "
            f"hash={current_hash[:12]}… size={current_size} force={force}"
        )
        routine_path = _write_temp(routine_bytes, docs["routine"]["file_type"])
        if docs.get("seat_plan") and seat_bytes:
            try:
                seat_path = _write_temp(seat_bytes, docs["seat_plan"]["file_type"])
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
            routine_hash=current_hash,
            routine_size=current_size,
            routine_title=routine_title,
            seat_plan_hash=seat_hash or None,
            seat_plan_size=seat_size or None,
        )
        write_term_index(
            exam_type,
            semester,
            year,
            routine_url=current_routine_url,
            routine_title=routine_title,
            section_count=synced,
        )

        return {
            "ok": True,
            "message": f"Success! Synced {synced}/{len(target_sections)} sections.",
            "routine_url": current_routine_url,
            "routine_hash": current_hash,
            "routine_size": current_size,
            "sections_discovered": len(target_sections),
            "sections_synced": synced,
            "sections_cached_before": existing_count,
            "force": force,
            "auto_force": auto_force,
            "skipped": False,
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


def _flatten_terms(node, exam_type="", semester="", year="", out=None):
    """Walk /terms tree → list of {exam_type, semester, year, ...}."""
    if out is None:
        out = []
    if not isinstance(node, dict):
        return out
    # Leaf term object has exam_type + semester + year keys
    if "exam_type" in node and "semester" in node and "year" in node:
        out.append(
            {
                "exam_type": str(node.get("exam_type") or exam_type).lower(),
                "semester": str(node.get("semester") or semester).lower(),
                "year": str(node.get("year") or year),
                "routine_url": node.get("routine_url") or "",
                "routine_title": node.get("routine_title") or "",
                "section_count": node.get("section_count"),
                "updated_at": node.get("updated_at"),
            }
        )
        return out
    for key, child in node.items():
        k = str(key).lower()
        if k in ("mid", "final"):
            _flatten_terms(child, exam_type=k, semester=semester, year=year, out=out)
        elif k in ("spring", "summer", "fall"):
            _flatten_terms(child, exam_type=exam_type, semester=k, year=year, out=out)
        elif re.fullmatch(r"20\d{2}", k):
            _flatten_terms(child, exam_type=exam_type, semester=semester, year=k, out=out)
        else:
            _flatten_terms(child, exam_type=exam_type, semester=semester, year=year, out=out)
    return out


@app.get("/api/history")
def history(
    section: Optional[str] = Query(
        None, description="If set, only terms that have data for this section."
    ),
):
    """List every exam term ever cached (permanent history).

    - Without section: returns the global /terms catalogue.
    - With section: also checks /routines_v2/{section} for term keys that
      may exist even if the global index is incomplete.
    """
    terms = []
    seen = set()

    global_tree = firebase_get(FIREBASE_TERMS_URL) or {}
    if isinstance(global_tree, dict):
        for t in _flatten_terms(global_tree):
            key = (t["exam_type"], t["semester"], t["year"])
            if key not in seen:
                seen.add(key)
                terms.append(t)

    if section:
        section = section.strip().upper().replace("-", "_")
        if not SECTION_RE.match(section):
            raise HTTPException(400, "Enter a valid section such as 65_L or 65_N.")
        # Walk routines_v2/{section}/{exam}/{semester}/{year}
        v2_root = firebase_get(f"{FIREBASE_V2_BASE.rstrip('/')}/{section}.json") or {}
        if isinstance(v2_root, dict):
            for exam_type, sem_map in v2_root.items():
                if not isinstance(sem_map, dict):
                    continue
                for semester, year_map in sem_map.items():
                    if not isinstance(year_map, dict):
                        continue
                    for year_s, payload in year_map.items():
                        if not isinstance(payload, dict):
                            continue
                        if not payload.get("exams"):
                            continue
                        key = (
                            str(exam_type).lower(),
                            str(semester).lower(),
                            str(year_s),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        terms.append(
                            {
                                "exam_type": key[0],
                                "semester": key[1],
                                "year": key[2],
                                "routine_url": (payload.get("source") or {}).get(
                                    "routine_url", ""
                                ),
                                "routine_title": (payload.get("source") or {}).get(
                                    "routine_title", ""
                                ),
                                "section_count": payload.get("exam_count"),
                                "updated_at": None,
                                "has_section_data": True,
                            }
                        )
                        # Mark existing ones that also have section data
                        for t in terms:
                            if (
                                t["exam_type"] == key[0]
                                and t["semester"] == key[1]
                                and str(t["year"]) == key[2]
                            ):
                                t["has_section_data"] = True

    def sort_key(t):
        try:
            y = int(t.get("year") or 0)
        except (TypeError, ValueError):
            y = 0
        sem_order = {"fall": 3, "summer": 2, "spring": 1}.get(
            str(t.get("semester") or "").lower(), 0
        )
        exam_order = 1 if str(t.get("exam_type") or "").lower() == "final" else 0
        return (y, sem_order, exam_order)

    terms.sort(key=sort_key, reverse=True)

    return {
        "ok": True,
        "count": len(terms),
        "section": section,
        "terms": terms,
    }


@app.get("/api/routine-archive")
def routine_archive(
    section: str = Query(..., min_length=2),
    exam_type: str = Query(...),
    semester: str = Query(...),
    year: int = Query(...),
):
    """Fetch a specific historical routine for a section from v2 cache only.

    Does not scrape the notice board — archive lookup only.
    """
    section = section.strip().upper().replace("-", "_")
    exam_type = exam_type.lower().strip()
    semester = semester.lower().strip()

    if not SECTION_RE.match(section):
        raise HTTPException(400, "Enter a valid section such as 65_L or 65_N.")
    if exam_type not in ("mid", "final"):
        raise HTTPException(400, "Exam type must be Mid or Final.")
    if semester not in ("spring", "summer", "fall"):
        raise HTTPException(400, "Semester must be Spring, Summer, or Fall.")
    if not 2000 <= year <= 2100:
        raise HTTPException(400, "Invalid academic year.")

    v2_url = (
        f"{FIREBASE_V2_BASE.rstrip('/')}/{section}/{exam_type}/{semester}/{year}.json"
    )
    data = firebase_get(v2_url)
    if not data or not isinstance(data, dict) or not data.get("exams"):
        raise HTTPException(
            404,
            f"No archived routine for {section} · {semester.title()} {year} {exam_type.title()}.",
        )

    data = dict(data)
    data["cached"] = True
    data["from_archive"] = True
    return data
