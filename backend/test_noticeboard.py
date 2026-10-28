"""Offline tests for the notice collector (no network needed)."""
from pathlib import Path

import pytest

import noticeboard as nb

DATA = Path(__file__).resolve().parent / "testdata"
ROUTINE = (DATA / "updated-cse-exam-routine-final-semester-summer-2026-9f64c0100d.pdf").read_bytes()
SEAT = (DATA / "final-exam-seat-details-summer-2026-2d4b722cea.pdf").read_bytes()

BASE = "https://webbackend.daffodilvarsity.edu.bd"


def card(nid, title, date):
    return (f'<div class="card"><a href="{BASE}/noticed/{nid}"><b>{title}</b></a>'
            f"<p>Computer Science and Engineering</p><p>Examination</p><p>{date}</p></div>")


LIST_HTML = "<html><body>" + "x" * 1600 + "".join([
    card(4029, "Updated Midterm Examination Routine and Seat Plan [Fall-2026]", "27th October 2026"),
    card(4023, "CSE Midterm Examination Routine, Fall-2026", "24th October 2026"),
    card(4004, "CSE Class Routine Version 4.1 - Fall 2026", "02nd October 2026"),
    card(3999, "Final Improvement Examination Routine for Summer 2026", "27th September 2026"),
    card(3799, "Final Examination Seat Plan (Summer-2026)", "18th August 2026"),
    card(3798, "Updated CSE Final Examination Routine-Summer 2026", "15th August 2026"),
    card(3789, "CSE Final Examination Routine, Summer-2026", "12th August 2026"),
]) + "</body></html>"


def detail(files):
    links = "".join(f'<a href="{BASE}/noticeFile/{f}">{label}</a>' for label, f in files)
    return "<html><body>" + "x" * 1600 + links + "</body></html>"


PAGES = {
    f"{BASE}/noticed/4029": detail([("Updated CSE Exam Routine Mid S...", "updated-cse-exam-routine-mid-semester-fall-2026-aa.pdf"),
                                    ("Seat Plan", "seat-plan-bb.pdf")]),
    f"{BASE}/noticed/4023": detail([("CSE Exam Routine", "cse-exam-routine-mid-semester-fall-2026-cc.pdf")]),
    f"{BASE}/noticed/3799": detail([("Seat details", "final-exam-seat-details-summer-2026-2d4b722cea.pdf")]),
    f"{BASE}/noticed/3798": detail([("Updated CSE Exam Routine", "updated-cse-exam-routine-final-semester-summer-2026-9f64c0100d.pdf")]),
    f"{BASE}/noticed/3789": detail([("CSE Exam Routine", "old-routine.pdf")]),
}


@pytest.fixture(autouse=True)
def fake_site(monkeypatch):
    nb.clear_notice_caches()

    def get_text(session, path_or_url):
        if path_or_url.startswith("http"):
            return PAGES[path_or_url], path_or_url
        if "page=1" in path_or_url:
            return LIST_HTML, f"{BASE}{path_or_url}"
        return "<html></html>" , path_or_url  # later pages empty -> stop

    def download(session, url):
        if "summer-2026-9f64c0100d" in url or "old-routine" in url:
            return {"url": url, "final_url": url, "bytes": ROUTINE, "file_type": "pdf"}
        if "seat-details-summer" in url:
            return {"url": url, "final_url": url, "bytes": SEAT, "file_type": "pdf"}
        return None  # Fall 2026 files are not available offline

    monkeypatch.setattr(nb, "_get_text", get_text)
    monkeypatch.setattr(nb, "_download", download)


def test_title_classification():
    c = nb.classify_title
    n = c("Updated Midterm Examination Routine and Seat Plan [Fall-2026]")
    assert (n.semester, n.year, n.exam_type, n.kind, n.updated) == ("fall", 2026, "mid", "routine+seat", True)
    n = c("CSE Midterm Examination Routine, Fall-2026")
    assert (n.semester, n.year, n.exam_type, n.kind) == ("fall", 2026, "mid", "routine")
    n = c("Final Examination Seat Plan (Summer-2026)")
    assert (n.semester, n.year, n.exam_type, n.kind) == ("summer", 2026, "final", "seat")
    n = c("Updated CSE Final Examination Routine-Summer 2026")
    assert (n.semester, n.year, n.exam_type, n.kind) == ("summer", 2026, "final", "routine")
    for junk in ("Final Improvement Examination Routine for Summer 2026",
                 "CSE Class Routine Version 4.1 - Fall 2026",
                 "Notice for Course Registration of Fall-2026 Semester"):
        assert c(junk).kind == ""


def test_candidates_include_newest_fall_mid_notice():
    items = nb.collect_notice_candidates("fall", 2026, "mid")
    assert items[0]["id"] == 4029          # the newest "Updated ... and Seat Plan" notice, no "CSE" in title
    assert {x["id"] for x in items[:2]} == {4029, 4023}
    assert all(x["id"] != 3999 for x in items)  # improvement exam excluded


def test_summer_final_end_to_end():
    docs = nb.discover_documents("65_L", "final", "summer", 2026)
    assert docs["routine"]["notice_id"] == 3798          # newest valid routine ("Updated ...")
    assert docs["seat_plan"] and docs["seat_plan"]["notice_id"] == 3799
    assert docs["routine"]["file_type"] == "pdf"


def test_refresh_style_empty_section():
    docs = nb.discover_documents("", "final", "summer", 2026, force_refresh=True)
    assert docs["routine"] and docs["seat_plan"]


def test_unpublished_session_has_clear_error():
    with pytest.raises(RuntimeError, match="No Spring 2027"):
        nb.discover_documents("65_L", "mid", "spring", 2027)


def test_wrong_session_pdf_is_rejected():
    # Fall 2026 mid files can't be downloaded offline -> must not fall back to the Summer PDF.
    with pytest.raises(RuntimeError):
        nb.discover_documents("65_L", "mid", "fall", 2026)
