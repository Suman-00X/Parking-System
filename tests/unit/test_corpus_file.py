"""Reading an uploaded report-corpus file, without a database."""

from __future__ import annotations

import datetime as dt

from radreport.onboarding.corpus import parse_corpus_file


def test_a_csv_corpus_reads_every_column() -> None:
    data = b"report_text,external_report_id,report_date,patient_age_years,is_deidentified\nLiver normal.,R1,2026-01-05,54,yes\nNo effusion.,R2,,,\n"
    records, problems = parse_corpus_file(data, "reports.csv")
    assert problems == []
    assert [r.external_report_id for r in records] == ["R1", "R2"]
    assert records[0].report_date == dt.date(2026, 1, 5) and records[0].patient_age_years == 54 and records[0].is_deidentified
    assert records[1].is_deidentified is False, "identified unless the file says otherwise"


def test_a_json_corpus_reads_like_the_api() -> None:
    records, problems = parse_corpus_file(b'[{"report_text": "Lungs clear.", "is_deidentified": true}]', "reports.json")
    assert problems == [] and records[0].report_text == "Lungs clear." and records[0].is_deidentified


def test_a_file_without_report_text_is_refused() -> None:
    assert parse_corpus_file(b"text,id\nx,1\n", "r.csv") == ([], ["missing required column: report_text"])


def test_bad_rows_are_skipped_with_a_reason() -> None:
    data = b"report_text,patient_age_years\nGood report.,40\n,30\nAnother.,old\n"
    records, problems = parse_corpus_file(data, "r.csv")
    assert len(records) == 1
    assert problems == ["row 3: report_text is empty", "row 4: patient_age_years or report_date is malformed"]


def test_an_unknown_column_is_not_silently_dropped() -> None:
    records, problems = parse_corpus_file(b'[{"report_text": "x", "patient_name": "Jane Doe"}]', "r.json")
    assert records == [] and "unknown column(s) patient_name" in problems[0]


def test_malformed_json_is_reported() -> None:
    assert parse_corpus_file(b"[{oops", "r.json")[1][0].startswith("not valid JSON")
