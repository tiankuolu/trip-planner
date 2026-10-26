import zipfile
from pathlib import Path

import pytest

from evaluations.report import rows
from scripts.complete_evaluation import complete


def test_resume_preserves_records_when_source_version_differs(tmp_path):
    archive = Path(__file__).resolve().parents[1] / "evaluations" / "reports" / "2026-10-23" / "evidence.zip"
    with zipfile.ZipFile(archive) as evidence:
        evidence.extractall(tmp_path)
    recorded = rows(tmp_path)
    assert len(recorded) == 96
    with pytest.raises(ValueError, match="源码与实验记录不同"):
        complete(tmp_path)
    assert rows(tmp_path) == recorded
    assert list(tmp_path.glob("resume-*.json")) == []
