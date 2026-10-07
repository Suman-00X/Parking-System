"""The demo seed's fixed data stays consistent with what the pipeline and the corpus mapper need."""

from __future__ import annotations

from radreport.devtools import demo_lab
from radreport.pipeline.stages.study_code import CARRIER_PATTERNS


def test_every_dictation_names_its_study_and_a_known_code() -> None:
    spoken = {words for _code, words in demo_lab.STUDY_CODES.values()}
    for _modality, _part, _title, words in demo_lab.DICTATIONS:
        assert words.startswith("study type "), "study-code detection needs its carrier phrase"
        assert any(words.removeprefix("study type ").startswith(code) for code in spoken)
    assert CARRIER_PATTERNS[0] == r"study\s*type"


def test_credentials_are_read_from_the_local_file(tmp_path) -> None:
    sheet = tmp_path / "creds.md"
    sheet.write_text("| product_admin | Product Admin | `admin@x.local` | `pw-1` |\n| radiologist | Dr A | `r1@x.local` | `pw-2` |\n| radiologist | Dr B | `r2@x.local` | `pw-3` |\n")
    assert demo_lab._credentials(sheet) == {"product_admin": [("admin@x.local", "pw-1")], "radiologist": [("r1@x.local", "pw-2"), ("r2@x.local", "pw-3")]}
