import os
from parser import parse_exam_routine, parse_seat_plan, build_student_routine

ROUTINE = os.environ.get("EXAM_ROUTINE_PDF", "/mnt/data/examroutine_test/updated-cse-exam-routine-final-semester-summer-2026-9f64c0100d.pdf")
SEAT = os.environ.get("SEAT_PLAN_PDF", "/mnt/data/examroutine_test/final-exam-seat-details-summer-2026-2d4b722cea.pdf")


def main():
    routine = parse_exam_routine(ROUTINE)
    seat = parse_seat_plan(SEAT)
    assert routine["semester"] == "Summer"
    assert routine["year"] == 2026
    assert len(routine["exams"]) == 32

    for section in ("65_L", "65_N", "66_A"):
        result = build_student_routine(routine, seat, section)
        assert result["exam_count"] == 4
        assert result["matched_seat_count"] == 4
        assert all(x["seat_match"] for x in result["exams"])

        fallback = build_student_routine(routine, None, section)
        assert fallback["exam_count"] == 4
        assert fallback["matched_seat_count"] == 0

    # Section-dependent courses: the batch routine contains alternatives,
    # while the seat plan identifies the actual course for each section.
    expected = {
        "64_A": {"2026-08-23": "CSE431", "2026-08-25": "CSE432"},
        "64_K": {"2026-08-23": "CSE441", "2026-08-25": "CSE442"},
        "64_N": {"2026-08-23": "CSE453", "2026-08-25": "CSE454"},
        "64_P": {"2026-08-23": "CSE471", "2026-08-25": "CSE472"},
    }
    expected_matches = {"64_A": 3, "64_K": 3, "64_N": 3, "64_P": 2}
    for section, by_date in expected.items():
        result = build_student_routine(routine, seat, section)
        assert result["exam_count"] == 3
        assert result["matched_seat_count"] == expected_matches[section]
        got = {x["date"]: x["course_code"] for x in result["exams"]}
        for date, code in by_date.items():
            assert got[date] == code, (section, date, got[date], code)

    print("ALL CORE TESTS PASSED")


if __name__ == "__main__":
    main()
