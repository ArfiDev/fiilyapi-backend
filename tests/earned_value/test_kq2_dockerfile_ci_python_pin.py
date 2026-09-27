"""KATALOG-UQ-2 (3, DUSUK) — Dockerfile/CI Python surumu bekcisi.

`app.modules.earned_value.labels.normalize_label` (v2) ve `3102e435238c` migration'in
dondurulmus kopyalari Python 3.12'nin (Unicode 15.0.0) unicodedata veritabaniyla
hesaplandi (bkz. `labels.py`nin SURUM RISKI notu, `test_KQ2_unidata_version_is_pinned_
15_0_0`). O bekci yalniz KOSAN sureci sinar — Dockerfile'da/CI'da Python surumu 3.13'e
(farkli Unicode surumu) YUKSELTILIRSE CI YINE de 3.12 ile kosuyor gorunebilir (bekci
"gecer") ama CANLI konteyner FARKLI surumle davranir. Bu dosya bunu ONLEMEK icin
Dockerfile'daki `FROM python:X.Y` VE `.github/workflows/ci.yml`deki HER
`python-version`i PARSE EDER (sabit "3.12" metnine GUVENMEZ, GERCEK dosyayi okur) — ikisi
de beklenen surumle (3.12) eslesmeli.

Onceki hal: bekci Dockerfile'a HIC BAGLI degildi (sabit varsayim). Bu test o bosluk icin
eklendi (opus curutmesi madde 3).
"""

from __future__ import annotations

import re
from pathlib import Path

#: tests/earned_value/<bu dosya>.py → parents[2] == repo koku (BACKEND_DIR).
BACKEND_DIR = Path(__file__).resolve().parents[2]
DOCKERFILE = BACKEND_DIR / "Dockerfile"
CI_WORKFLOW = BACKEND_DIR / ".github" / "workflows" / "ci.yml"

#: `labels.py`/migration'in dondurulmus kopyalarinin hesaplandigi surum (Unicode 15.0.0).
EXPECTED_MINOR = "3.12"

MESSAGE = "donmuş normalize Unicode sürümü; KATALOG-UQ yeniden hesaplama migration'ı gerekir"

_DOCKERFILE_FROM_RE = re.compile(r"^FROM\s+python:(\d+\.\d+)", re.MULTILINE)
_CI_PYTHON_VERSION_RE = re.compile(r'python-version:\s*"?(\d+\.\d+)"?')


def _dockerfile_python_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = _DOCKERFILE_FROM_RE.search(text)
    assert match, f"Dockerfile'da 'FROM python:X.Y' bulunamadi: {path}"
    return match.group(1)


def _ci_python_versions(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    versions = set(_CI_PYTHON_VERSION_RE.findall(text))
    assert versions, f"ci.yml'de 'python-version' bulunamadi: {path}"
    return versions


def test_KQ2_dockerfile_python_version_matches_frozen_unicode_pin(
    dockerfile: Path = DOCKERFILE,
) -> None:
    """`dockerfile` parametresi MUTASYON testi icindir: gecici bir sahte kopya verilip
    fonksiyon o yola cagrilabilir — gercek Dockerfile'a DOKUNULMAZ."""
    version = _dockerfile_python_version(dockerfile)
    assert version == EXPECTED_MINOR, f"{MESSAGE} (Dockerfile: {version} != {EXPECTED_MINOR})"


def test_KQ2_ci_python_version_matches_frozen_unicode_pin(
    ci_workflow: Path = CI_WORKFLOW,
) -> None:
    """`ci_workflow` parametresi MUTASYON testi icindir (yukaridaki ile ayni desen)."""
    versions = _ci_python_versions(ci_workflow)
    assert versions == {EXPECTED_MINOR}, f"{MESSAGE} (ci.yml: {versions} != {{EXPECTED_MINOR}})"
