import re
from datetime import datetime
from collections import defaultdict
from typing import Any, Optional
import pdfplumber


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_session(text: str) -> dict:
    s = norm(text)
    m = re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\s*[- ]?\s*(20\d{2})\b", s, re.I)
    if not m:
        return {"semester": "", "year": None}
    return {"semester": m.group(1).title(), "year": int(m.group(2))}


def clean_course_code(value: str) -> str:
    m = re.search(r"\b([A-Z]{2,8}\d{3,4})\b", value.upper())
    return m.group(1) if m else ""


def normalize_batch(value: str) -> str:
    s = norm(value).upper()
    m = re.search(r"\d+", s)
    return m.group(0) if m else ""


def normalize_section(value: str) -> str:
    s = norm(value).upper().replace(" ", "").replace("-", "_")
    if s.startswith("BATCH"):
        s = s[5:]
    return s


def parse_date(value: str) -> Optional[str]:
    s = norm(value)
    for pattern in (
        r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b",
        r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b",
    ):
        m = re.search(pattern, s)
        if not m:
            continue
        parts = list(map(int, m.groups()))
        if parts[0] >= 2000:
            y, mo, d = parts
        else:
            d, mo, y = parts
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def weekday(date_iso: Optional[str]) -> str:
    if not date_iso:
        return ""
    return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%A")


def slot_time(slot: str) -> str:
    return {
        "A": "09:00 AM - 11:00 AM",
        "B": "12:00 PM - 02:00 PM",
        "C": "03:00 PM - 05:00 PM",
    }.get(slot, "")


def page_date_slot(page):
    text = page.extract_text() or ""
    date = parse_date(text)
    m = re.search(r"\bSlot\s*:\s*([ABC])\b", text, re.I)
    return date, (m.group(1).upper() if m else "")


# ============================================================
# EXAM ROUTINE
# ============================================================

def parse_course_cell(cell: str):
    text = norm(cell)
    if not text:
        return None
    text = re.sub(r"\[\s*\d+\s*\]", "", text)
    m = re.match(r"^([A-Z]{2,8}\d{3,4})\s*:?(.*)$", text, re.I)
    if not m:
        return None
    code = m.group(1).upper()
    name = norm(m.group(2)).lstrip(": ")
    if not name:
        return None
    return code, name


def _table_header_row(row):
    text = " ".join(norm(x or "") for x in row)
    return bool(re.search(r"Slot\s*A", text, re.I)) and bool(re.search(r"Slot\s*B", text, re.I))


def _parse_table_exam_rows(table, page_no, page_first_date=None, pending=None):
    """
    Parse one routine table. A repeated blue Slot A/B/C row starts a NEW date
    block. This matters because the official PDF can put the last course of
    one block at the bottom of a page and put its date on the next page.
    """
    slot_columns = [(1, 2, "A"), (3, 4, "B"), (5, 6, "C")]
    found = []
    current_date = None

    for raw in table:
        row = list(raw) + [None] * max(0, 7 - len(raw))
        row = row[:7]

        if _table_header_row(row):
            current_date = None
            continue

        d = parse_date(row[0] or "")
        if d:
            current_date = d

        for course_idx, batch_idx, slot in slot_columns:
            course = parse_course_cell(row[course_idx] or "")
            batch = normalize_batch(row[batch_idx] or "")
            if not course or not batch:
                continue

            course_codes = list(dict.fromkeys(
                m.group(1).upper()
                for m in re.finditer(r"\b([A-Z]{2,8}\d{3,4})\b", row[course_idx] or "", re.I)
            ))
            item = {
                "date": current_date,
                "day": weekday(current_date),
                "slot": slot,
                "time": slot_time(slot),
                "course_code": course[0],
                "course_codes": course_codes or [course[0]],
                "course_name": course[1],
                "batch": batch,
                "source_page": page_no,
            }

            if current_date:
                found.append(item)
            else:
                # Date is expected on the next page. Keep this item pending.
                pending.append(item)

    return found


def parse_exam_routine(path: str) -> dict:
    exams = []
    pending = []
    metadata = {"semester": "", "year": None}

    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text() or ""
            if not metadata["semester"]:
                metadata = extract_session(page_text)

            page_dates = re.findall(r"\b\d{1,2}[/-]\d{1,2}[/-]20\d{2}\b", page_text)
            next_page_date = parse_date(page_dates[0]) if page_dates else None

            if pending and next_page_date:
                for item in pending:
                    item["date"] = next_page_date
                    item["day"] = weekday(next_page_date)
                    exams.append(item)
                pending.clear()

            for table in page.extract_tables():
                exams.extend(_parse_table_exam_rows(
                    table,
                    page_no,
                    page_first_date=next_page_date,
                    pending=pending,
                ))

    unique = {}
    for e in exams:
        if not e.get("date"):
            continue
        unique[(e["date"], e["slot"], e["course_code"], e["batch"])] = e

    return {
        "semester": metadata["semester"],
        "year": metadata["year"],
        "exams": sorted(
            unique.values(),
            key=lambda x: (x["date"], x["slot"], x["course_code"]),
        ),
    }


# ============================================================
# SEAT PLAN
# ============================================================

def parse_seat_plan(path: str) -> list[dict]:
    allocations = []
    current = None

    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            page_date, page_slot = page_date_slot(page)
            for table in page.extract_tables():
                for raw in table:
                    row = list(raw) + [None] * max(0, 8 - len(raw))
                    row = row[:8]

                    course_code = clean_course_code(row[1] or "")
                    section_cell = norm(row[4] or "")
                    room_cell = norm(row[5] or "")
                    seats_cell = norm(row[6] or "")
                    total_cell = norm(row[7] or "")

                    if section_cell and re.match(r"^\d{2,4}[_-][A-Z0-9]+$", section_cell, re.I):
                        # The official seat plan can split one course across PDF
                        # pages. A continuation page may omit Dept/ID/Course Title
                        # while still starting a new section row. Preserve the
                        # previous course code when the new row has no ID and is
                        # on the same date/slot.
                        inherited_course = ""
                        if (
                            not course_code
                            and current
                            and current.get("date") == page_date
                            and current.get("slot") == page_slot
                        ):
                            inherited_course = current.get("course_code", "")

                        current = {
                            "date": page_date,
                            "slot": page_slot,
                            "section": normalize_section(section_cell),
                            "course_code": course_code or inherited_course,
                            "rooms": [],
                            "total": int(total_cell) if total_cell.isdigit() else None,
                            "source_page": page_no,
                        }
                        allocations.append(current)

                    if current and room_cell and seats_cell.isdigit():
                        current["rooms"].append({
                            "room": room_cell,
                            "seats": int(seats_cell),
                        })

    for a in allocations:
        seen = set()
        rooms = []
        for r in a["rooms"]:
            key = (r["room"], r["seats"])
            if key not in seen:
                seen.add(key)
                rooms.append(r)
        a["rooms"] = rooms
        calculated = sum(r["seats"] for r in rooms)
        if a["total"] is None:
            a["total"] = calculated

    return allocations


# ============================================================
# MATCHING
# ============================================================

def build_student_routine(routine: dict, seat_plan: Optional[list[dict]], section: str) -> dict:
    wanted_section = normalize_section(section)
    wanted_batch = normalize_batch(section)

    routine_exams = routine.get("exams", [])
    batch_exams = [
        e for e in routine_exams
        if normalize_batch(e["batch"]) == wanted_batch
    ]

    section_allocations = []
    if seat_plan:
        section_allocations = [
            s for s in seat_plan
            if normalize_section(s["section"]) == wanted_section
        ]

    # Strong key: section + date + slot + course code.
    # Fall back to section + date + slot only when the seat-plan row has no
    # course code. This prevents accidentally assigning another course's room.
    seat_index = defaultdict(list)
    for s in section_allocations:
        code = clean_course_code(s.get("course_code", ""))
        seat_index[(s["date"], s["slot"], code)].append(s)
        if not code:
            seat_index[(s["date"], s["slot"], "")].append(s)

    result = []
    for exam in batch_exams:
        routine_codes = exam.get("course_codes") or [exam["course_code"]]
        candidates = []
        for code in routine_codes:
            candidates.extend(seat_index.get((exam["date"], exam["slot"], code), []))
        if not candidates:
            candidates = seat_index.get((exam["date"], exam["slot"], ""), [])
        # Deduplicate candidates while preserving order.
        seen = set()
        candidates = [c for c in candidates if not ((c["source_page"], c["section"]) in seen or seen.add((c["source_page"], c["section"]))) ]
        chosen = candidates[0] if len(candidates) == 1 else None

        item = {
            **exam,
            "section": wanted_section,
            "rooms": chosen["rooms"] if chosen else [],
            "total_students": chosen["total"] if chosen else None,
            "seat_match": bool(chosen),
            "seat_source_page": chosen["source_page"] if chosen else None,
        }
        result.append(item)

    warnings = []
    if not batch_exams:
        warnings.append(f"No routine entries found for batch {wanted_batch}.")
    if seat_plan and not section_allocations:
        warnings.append(f"Section {wanted_section} was not found in the seat-plan PDF.")
    for x in result:
        if seat_plan and not x["seat_match"]:
            warnings.append(f"{x['date']} {x['course_code']}: seat allocation not matched.")
        if x["seat_match"]:
            calculated = sum(r["seats"] for r in x["rooms"])
            if calculated != x["total_students"]:
                warnings.append(
                    f"{x['date']} {x['course_code']}: room seats total "
                    f"{calculated}, PDF total says {x['total_students']}."
                )

    return {
        "semester": routine.get("semester", ""),
        "year": routine.get("year"),
        "section": wanted_section,
        "batch": wanted_batch,
        "exam_count": len(result),
        "seat_plan_uploaded": bool(seat_plan),
        "matched_seat_count": sum(1 for x in result if x["seat_match"]),
        "exams": result,
        "warnings": warnings,
    }

