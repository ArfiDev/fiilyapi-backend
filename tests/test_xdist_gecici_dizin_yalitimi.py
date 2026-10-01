"""TB-TMPDIR bekçisi: xdist işçisinin geçici dizini KENDİNE ait olmalı.

Kanon: `--dist loadfile` DOSYA bütünlüğünü korur, SÜREÇ bütünlüğünü korumaz.
`tests/conftest.py`teki işçi başına `TMPDIR` yaması kaldırılırsa bu test
`-n` ile koşulan turda KIRMIZI olur (seri koşuda anlamsız → atlanır).
"""

import os
import tempfile

import pytest

from tests.conftest import _gecici_kok_hesapla, _gecici_kok_kur

XDIST_ISCI = os.environ.get("PYTEST_XDIST_WORKER")

requires_xdist = pytest.mark.skipif(
    not XDIST_ISCI, reason="Seri koşuda işçi yalıtımı diye bir şey yoktur"
)


def test_ayni_isci_adli_iki_oturum_birbirinin_dosyasini_silmez(tmp_path):
    """Paralel iki pytest koşusunun gw0'ı aynı adı taşısa da dizinleri ayrı olmalı."""
    isci = "gw0"
    kok_a = _gecici_kok_hesapla(str(tmp_path), isci, "1111")
    kok_b = _gecici_kok_hesapla(str(tmp_path), isci, "2222")

    _gecici_kok_kur(kok_a, isci, "1111")
    _gecici_kok_kur(kok_b, isci, "2222")
    dosya_b = os.path.join(kok_b, "openpyxl.ornek")
    with open(dosya_b, "w") as f:
        f.write("b")

    _gecici_kok_kur(kok_a, isci, "1111")  # A yeniden kurulur (rmtree)

    assert os.path.exists(dosya_b)


@requires_xdist
def test_gettempdir_isci_son_ekini_tasir():
    assert os.path.basename(tempfile.gettempdir()) == f"fiil-erp-test-{XDIST_ISCI}-{os.getpid()}"


@requires_xdist
def test_tmpdir_ortam_degiskeni_de_yamalanir():
    """Alt süreçler `tempfile.tempdir`i DEĞİL `TMPDIR`i okur; ikisi de gerekir."""
    assert os.environ.get("TMPDIR") == tempfile.gettempdir()


@requires_xdist
def test_uretilen_gecici_dosya_isci_dizinine_duser():
    with tempfile.NamedTemporaryFile(prefix="tbtmpdir.") as f:
        assert os.path.dirname(f.name) == tempfile.gettempdir()
