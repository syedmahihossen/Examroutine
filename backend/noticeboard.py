"""DIU CSE Notice Board discovery (v10) - requests only, no browser.

Why this was rewritten
----------------------
daffodilvarsity.edu.bd/noticeboard is now a client-rendered Next.js app, so the
old Playwright crawl saw no notices and newly published routines were missed.
The same notices are served as plain server-rendered HTML by the notice
backend, per department:

    https://webbackend.daffodilvarsity.edu.bd/department-notice/cse?page=N

Each list row links to /noticed/<id>; the detail page links its PDFs as
/noticeFile/<slug>-<hash>.pdf. Notice ids increase over time, so "newest id"
means "latest notice" and an "Updated ..." notice automatically supersedes the
one it replaces.

Real titles handled (note: the newest one has no "CSE" in the title because the
list is already CSE-only, and it bundles the routine and the seat plan):
    CSE Midterm Examination Routine, Fall-2026
    Updated Midterm Examination Routine and Seat Plan [Fall-2026]
    Updated CSE Final Examination Routine-Summer 2026
    Final Examination Seat Plan (Summer-2026)
Excluded: "Final Improvement Examination Routine ...", class routines, etc.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

for _name in ("pdfminer", "pdfminer.pdfpage", "pdfplumber"):
    logging.getLogger(_name).setLevel(logging.ERROR)

try:
    from .parser import (
        clean_course_code, normalize_batch, normalize_section,
        parse_exam_routine, parse_seat_plan,
    )
except ImportError:  # direct run from backend/
    from parser import (
        clean_course_code, normalize_batch, normalize_section,
        parse_exam_routine, parse_seat_plan,
    )

log = logging.getLogger("noticeboard")

# Shown to users / stored in Firebase metadata.
NOTICEBOARD_URL = "https://daffodilvarsity.edu.bd/noticeboard"

# Mirrors of the same CSE notice list. The first one that answers wins.
NOTICE_HOSTS = [
    "https://webbackend.daffodilvarsity.edu.bd",
    "https://daffodilvarsity.edu.bd",
    "https://old.daffodilvarsity.edu.bd",
]
LIST_PATH = "/department-notice/cse"
MAX_LIST_PAGES = 6
MAX_DOCUMENT_BYTES = 30 * 1024 * 1024
HTTP_TIMEOUT = (8, 25)
LIST_CACHE_SECONDS = 600
DOC_CACHE_SECONDS = 1800

SEMESTERS = ("spring", "summer", "fall")
EXAM_TYPES = ("mid", "final")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/pdf,*/*;q=0.8",
}

_LIST_CACHE: dict[str, tuple[float, list]] = {}
_DOC_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_INFLIGHT: dict[tuple, threading.Lock] = {}


# --------------------------------------------------------------------------
# small helpers (kept because main.py / older code import some of them)
# --------------------------------------------------------------------------
def norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def normalize_semester(v: str) -> str:
    s = norm(v).lower()
    return next((x for x in SEMESTERS if re.search(rf"\b{x}\b", s)), "")


def normalize_exam_type(v: str) -> str:
    s = norm(v).lower().replace("–", "-").replace("—", "-")
    if re.search(r"\bmid(?:[\s-]?term|[\s-]?semester)?\b", s):
        return "mid"
    if re.search(r"\bfinal\b", s):
        return "final"
    return ""


@dataclass
class Notice:
    id: int
    title: str
    url: str
    date_text: str = ""
    semester: str = ""
    year: Optional[int] = None
    exam_type: str = ""
    kind: str = ""          # routine | seat | routine+seat | ""
    updated: bool = False

    def as_debug(self) -> dict:
        return {
            "id": self.id, "title": self.title, "url": self.url, "date": self.date_text,
            "semester": self.semester, "year": self.year, "exam_type": self.exam_type,
            "kind": self.kind, "updated": self.updated,
        }


_JUNK = (
    "class routine", "registration", "registrar", "retake", "fees", "fee ",
    "improvement", "makeup", "make-up", "make up", "advisor", "convocation",
    "admission", "clearance", "scholarship", "orientation",
)


def classify_title(title: str) -> Notice:
    """Extract (semester, year, exam type, kind) from a notice title."""
    t = re.sub(r"[\[\]\(\)_,:;]", " ", norm(title).lower())
    t = re.sub(r"\s+", " ", t).strip()
    n = Notice(id=0, title=norm(title), url="")

    is_exam = bool(re.search(r"\bexam|\bmid[\s-]?term|\bmid\b|seat\s*(plan|details)", t))
    if not is_exam or any(j in t for j in _JUNK):
        return n

    m = re.search(r"\b(spring|summer|fall)\s*[-–]?\s*(?:semester\s*)?'?(20\d{2}|\d{2})\b", t)
    if m:
        n.semester = m.group(1)
        y = int(m.group(2))
        n.year = y if y > 99 else 2000 + y

    n.exam_type = normalize_exam_type(t)

    has_routine = bool(re.search(r"\broutine\b|\bschedule\b", t))
    has_seat = bool(re.search(r"seat[\s-]*(plan|details)|sitting\s*plan", t))
    if has_routine and has_seat:
        n.kind = "routine+seat"
    elif has_seat:
        n.kind = "seat"
    elif has_routine:
        n.kind = "routine"
    n.updated = bool(re.search(r"\b(updated|revised|rescheduled|corrected|final\s+update)\b", t))
    return n


# --------------------------------------------------------------------------
# HTTP + HTML parsing
# --------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get_text(session: requests.Session, path_or_url: str) -> tuple[str, str]:
    """GET HTML, trying every mirror for relative paths. Returns (html, url)."""
    urls = [path_or_url] if path_or_url.startswith("http") else [h + path_or_url for h in NOTICE_HOSTS]
    errors = []
    for u in urls:
        try:
            r = session.get(u, timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and len(r.text) > 1500:
                return r.text, r.url
            errors.append(f"{u}: HTTP {r.status_code}, {len(r.text)} bytes")
        except requests.RequestException as exc:
            errors.append(f"{u}: {exc.__class__.__name__}")
    raise RuntimeError("DIU notice board unreachable (" + "; ".join(errors) + ")")


_DATE_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)\s+[A-Za-z]+\s+20\d{2}")


def parse_list(html: str, base: str) -> list[Notice]:
    """Parse a /department-notice/cse page into classified notices (newest first)."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[int, Notice] = {}
    for a in soup.find_all("a", href=re.compile(r"/noticed/\d+")):
        nid = int(re.search(r"/noticed/(\d+)", a["href"]).group(1))
        title = norm(a.get_text(" ", strip=True))
        if not title or nid in found:
            continue
        n = classify_title(title)
        n.id, n.title, n.url = nid, title, urljoin(base, a["href"])
        card = a.find_parent(["div", "li", "article", "tr"]) or a.parent
        d = _DATE_RE.search(card.get_text(" ", strip=True)) if card else None
        n.date_text = d.group(0) if d else ""
        found[nid] = n
    return sorted(found.values(), key=lambda x: x.id, reverse=True)


def parse_attachments(html: str, base: str) -> list[tuple[str, str]]:
    """[(label, absolute_url)] for every file linked from a notice page."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.search(r"/noticeFile/|/download-file/|\.(pdf|xlsx|xlsm)(\?|$)", href, re.I):
            url = urljoin(base, href)
            if url not in seen:
                seen.add(url)
                out.append((norm(a.get_text(" ", strip=True)), url))
    return out


def attachment_kind(label: str, url: str) -> str:
    blob = f"{label} {urlparse(url).path}".lower()
    if re.search(r"seat|sitting", blob):
        return "seat"
    return "routine"


def _list_notices(session: requests.Session, force: bool = False) -> list[Notice]:
    """First MAX_LIST_PAGES pages of the CSE notice list, newest first (cached)."""
    key = "cse"
    with _LOCK:
        hit = _LIST_CACHE.get(key)
    if hit and not force and time.monotonic() - hit[0] < LIST_CACHE_SECONDS:
        return list(hit[1])

    notices: dict[int, Notice] = {}
    last_error: Optional[Exception] = None
    for page in range(1, MAX_LIST_PAGES + 1):
        try:
            html, base = _get_text(session, f"{LIST_PATH}?page={page}")
        except Exception as exc:  # keep what we have if a later page fails
            last_error = exc
            if page == 1:
                raise
            break
        rows = parse_list(html, base)
        if not rows:
            break
        for n in rows:
            notices.setdefault(n.id, n)
    if not notices and last_error:
        raise last_error
    result = sorted(notices.values(), key=lambda x: x.id, reverse=True)
    with _LOCK:
        _LIST_CACHE[key] = (time.monotonic(), list(result))
    return result


def _download(session: requests.Session, url: str) -> Optional[dict]:
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        raw = r.content
        if r.status_code != 200 or not raw or len(raw) > MAX_DOCUMENT_BYTES:
            return None
        if raw.startswith(b"%PDF"):
            return {"url": url, "final_url": r.url, "bytes": raw, "file_type": "pdf"}
        if raw[:2] == b"PK":
            return {"url": url, "final_url": r.url, "bytes": raw, "file_type": "xlsx"}
    except requests.RequestException:
        pass
    return None


def _save_temp(raw: bytes, ft: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".xlsx" if ft == "xlsx" else ".pdf")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(raw)
    return path


# --------------------------------------------------------------------------
# Verification of downloaded documents
# --------------------------------------------------------------------------
def _verify_routine(doc: dict, section: str, semester: str, year: Optional[int]) -> Optional[dict]:
    """Parse the routine; require right session (when printed) and the section's batch."""
    path = _save_temp(doc["bytes"], doc["file_type"])
    try:
        parsed = parse_exam_routine(path)
    except Exception as exc:
        log.warning("Routine parse failed for %s: %s", doc["url"], exc)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    exams = parsed.get("exams") or []
    batch = normalize_batch(section)
    if batch:
        exams = [e for e in exams if normalize_batch(e.get("batch")) == batch]
    if not exams:
        return None
    if parsed.get("semester") and normalize_semester(parsed["semester"]) != semester:
        return None
    if year and parsed.get("year") and int(parsed["year"]) != int(year):
        return None
    return parsed


def _verify_seat(doc: dict, section: str, routine_keys: set) -> Optional[list]:
    if doc.get("file_type") != "pdf":
        return None
    path = _save_temp(doc["bytes"], "pdf")
    try:
        alloc = parse_seat_plan(path)
    except Exception as exc:
        log.warning("Seat plan parse failed for %s: %s", doc["url"], exc)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    wanted = normalize_section(section)
    matches = [x for x in alloc if x.get("rooms") and (not wanted or normalize_section(x.get("section")) == wanted)]
    if not matches:
        return None
    if routine_keys:
        overlap = sum(
            1 for x in matches
            if (x.get("date"), x.get("slot"), clean_course_code(x.get("course_code", ""))) in routine_keys
            or (x.get("date"), x.get("slot")) in {(k[0], k[1]) for k in routine_keys}
        )
        if overlap <= 0:
            return None
    return alloc


# --------------------------------------------------------------------------
# Public API (names used by main.py)
# --------------------------------------------------------------------------
def collect_notice_candidates(semester: str, year: Optional[int], exam_type: str, force_refresh: bool = False) -> list[dict]:
    """Lightweight listing used by /api/discovery-debug (no PDFs downloaded)."""
    semester, exam_type = normalize_semester(semester), normalize_exam_type(exam_type)
    notices = _list_notices(_session(), force=force_refresh)
    out = []
    for n in notices:
        match = bool(n.kind and n.semester == semester and n.exam_type == exam_type and (not year or n.year == int(year)))
        if match or n.kind:
            out.append({
                "title": n.title, "url": n.url, "id": n.id, "kind": n.kind,
                "semester": n.semester, "year": n.year, "exam_type": n.exam_type,
                "updated": n.updated, "date": n.date_text,
                "score_hint": (2000 if match else 100) + n.id // 10,
                "context": f"{n.date_text} | kind={n.kind} | match={match}",
            })
    out.sort(key=lambda x: (x["score_hint"], x["id"]), reverse=True)
    return out


def clear_notice_caches() -> None:
    with _LOCK:
        _LIST_CACHE.clear()
        _DOC_CACHE.clear()


def _discover(section: str, exam_type: str, semester: str, year: Optional[int], include_seat_plan: bool, force: bool) -> dict:
    session = _session()
    notices = _list_notices(session, force=force)
    wanted = [
        n for n in notices
        if n.kind and n.exam_type == exam_type and n.semester == semester and (year is None or n.year == year)
    ]
    wanted.sort(key=lambda n: n.id, reverse=True)  # newest first; "Updated" wins by id
    if not wanted:
        raise RuntimeError(
            f"No {semester.title()} {year or ''} CSE {exam_type.title()} routine notice was found on the DIU "
            f"notice board yet. It may not be published - try again later."
        )

    routine_hit = None   # (notice, doc, parsed)
    seat_hit = None      # (notice, doc, alloc)
    seat_docs: list[tuple[Notice, dict]] = []  # fallback pool for seat plans

    for n in wanted:
        if routine_hit and seat_hit:
            break
        try:
            html, base = _get_text(session, n.url)
        except Exception as exc:
            log.warning("Could not open notice %s: %s", n.url, exc)
            continue
        atts = parse_attachments(html, base)
        routines = [(l, u) for l, u in atts if attachment_kind(l, u) == "routine"]
        seats = [(l, u) for l, u in atts if attachment_kind(l, u) == "seat"]

        if not routine_hit and n.kind in ("routine", "routine+seat"):
            for label, url in routines:
                doc = _download(session, url)
                parsed = _verify_routine(doc, section, semester, year) if doc else None
                if parsed:
                    routine_hit = (n, doc, parsed)
                    break

        if include_seat_plan and not seat_hit and seats:
            for label, url in seats:
                doc = _download(session, url)
                if doc:
                    seat_docs.append((n, doc))
        # Seat docs are verified against the chosen routine below.

        # Seat-only notices still need a routine from some other notice: keep looping.

    if not routine_hit:
        raise RuntimeError(
            f"A {semester.title()} {year or ''} CSE {exam_type.title()} notice exists, but no routine "
            f"containing batch {normalize_batch(section) or '(any)'} could be verified from its PDF."
        )

    if include_seat_plan and seat_docs:
        routine_n, _, parsed = routine_hit
        batch = normalize_batch(section)
        keys = {
            (e["date"], e["slot"], clean_course_code(e.get("course_code", "")))
            for e in parsed["exams"]
            if not batch or normalize_batch(e.get("batch")) == batch
        }
        # Prefer the seat plan bundled with the routine notice, then newest.
        seat_docs.sort(key=lambda x: (x[0].id == routine_n.id, x[0].id), reverse=True)
        for n, doc in seat_docs:
            alloc = _verify_seat(doc, section, keys)
            if alloc:
                seat_hit = (n, doc, alloc)
                break

    rn, rdoc, _ = routine_hit
    result = {
        "routine": {
            "url": rdoc["final_url"], "title": rn.title, "bytes": rdoc["bytes"],
            "file_type": rdoc["file_type"], "exam_type": exam_type,
            "semester": semester.title(), "year": year or rn.year,
            "notice_url": rn.url, "notice_id": rn.id,
        },
        "seat_plan": None,
        "source": NOTICEBOARD_URL,
    }
    if seat_hit:
        sn, sdoc, _ = seat_hit
        result["seat_plan"] = {
            "url": sdoc["final_url"], "title": sn.title, "bytes": sdoc["bytes"],
            "file_type": sdoc["file_type"], "notice_url": sn.url, "notice_id": sn.id,
        }
    return result


def discover_documents(section: str, exam_type: str, semester: str, year: Optional[int] = None,
                       include_seat_plan: bool = True, force_refresh: bool = False) -> dict:
    section = normalize_section(section)
    exam_type = normalize_exam_type(exam_type) or (exam_type or "").lower().strip()
    semester = normalize_semester(semester) or (semester or "").lower().strip()
    if exam_type not in EXAM_TYPES:
        raise ValueError("Exam type must be Mid or Final.")
    if semester not in SEMESTERS:
        raise ValueError("Semester must be Spring, Summer, or Fall.")
    if year is not None and not 2000 <= int(year) <= 2100:
        raise ValueError("Invalid academic year.")
    year_i = int(year) if year is not None else None

    # The routine/seat PDFs are shared by every section of a session, so cache
    # per session. A section is only used for verification, which is cheap.
    doc_key = (exam_type, semester, year_i, bool(include_seat_plan), section if section else "")
    with _LOCK:
        hit = _DOC_CACHE.get(doc_key)
        gate = _INFLIGHT.setdefault(doc_key, threading.Lock())
    if hit and not force_refresh and time.monotonic() - hit[0] < DOC_CACHE_SECONDS:
        return {**hit[1], "from_cache": True}

    with gate:  # coalesce concurrent identical lookups
        with _LOCK:
            hit = _DOC_CACHE.get(doc_key)
        if hit and not force_refresh and time.monotonic() - hit[0] < DOC_CACHE_SECONDS:
            return {**hit[1], "from_cache": True}
        result = _discover(section, exam_type, semester, year_i, include_seat_plan, force_refresh)
        with _LOCK:
            _DOC_CACHE[doc_key] = (time.monotonic(), result)
            if len(_DOC_CACHE) > 24:
                oldest = min(_DOC_CACHE.items(), key=lambda kv: kv[1][0])[0]
                _DOC_CACHE.pop(oldest, None)
        return result
