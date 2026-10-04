"""SIL-B1 — etiket + mali sınıf bekçisi: motorun ulaştığı HER tablonun Türkçe adı olmalı.

Önizleme kullanıcıya tabloların adını gösterir. Etiketsiz tablo teknik adıyla (ör.
`ev_day_cells`) görünürdü. Bu test her kayıtlı türün kapsadığı tabloları FK grafiği + kancalar
üzerinden METADATA'dan hesaplar ve eksik etiketi KIRMIZI yapar: yeni bir FK ya da yeni bir aile
eklenince etiket yazmak unutulamaz.
"""

import app.main  # noqa: F401  (tüm modeller `Base.metadata`ya kaydolsun)
from app.core.db import Base
from app.core.silme.etiketler import MALI_TABLOLAR_DIGER, TABLOLAR, TUM_TABLOLAR
from app.core.silme.graf import tum_kenarlar
from app.core.silme.turler import kayitli_turler
from app.core.silme.yurutucu import silme_sirasi
from app.modules.silme import kayitlar  # noqa: F401  (tür + kanca kaydı)


def _ulasilan_tablolar(kok: str) -> tuple[set[str], set[str]]:
    """(silinecek tablolar, bağı kopacak tablolar) — çözücünün tablo düzeyindeki aynası."""
    kenarlar = tum_kenarlar(Base.metadata)
    silinecek = {kok}
    kopacak: set[str] = set()
    sinir = [kok]
    while sinir:
        ust = sinir.pop()
        for k in kenarlar:
            if k.ust != ust:
                continue
            if k.iliski == "detach":
                kopacak.add(k.alt)
            elif k.alt not in silinecek:
                silinecek.add(k.alt)
                sinir.append(k.alt)
    return silinecek, kopacak


def test_kayitli_her_turun_ulastigi_her_tablonun_turkce_adi_var() -> None:
    turler = kayitli_turler()
    assert set(turler) >= {"site", "section", "block", "unit"}
    eksik: dict[str, set[str]] = {}
    for anahtar, tur in turler.items():
        silinecek, kopacak = _ulasilan_tablolar(tur.tablo)
        # bağı kopacak tabloların alt-ağaçları ÖNİZLENMEZ: yalnız kendi adları gerekir
        for tablo in silinecek | kopacak:
            if tablo not in TUM_TABLOLAR or not TUM_TABLOLAR[tablo].etiket:
                eksik.setdefault(anahtar, set()).add(tablo)
    assert eksik == {}


def test_etiketler_gercek_tablolara_bakar_yazim_hatasi_yok() -> None:
    bilinmeyen = set(TUM_TABLOLAR) - set(Base.metadata.tables)
    assert bilinmeyen == set()


def test_etiketli_kolonlar_gercekten_var() -> None:
    kotu = {
        (tablo, kolon)
        for tablo, bilgi in TUM_TABLOLAR.items()
        for kolon in bilgi.ornek
        if kolon not in Base.metadata.tables[tablo].c
    }
    assert kotu == set()


def test_etiket_sayisi_ve_kapsam_kilitli() -> None:
    """Şantiye ailesi: 52 tablo FK ile + 4 tablo kancayla silinir, 11 tablonun yalnız bağı kopar."""
    silinecek: set[str] = set()
    kopacak: set[str] = set()
    for tur in kayitli_turler().values():
        s, k = _ulasilan_tablolar(tur.tablo)
        silinecek |= s
        kopacak |= k
    assert len(silinecek) == 56
    assert len(kopacak - silinecek) == 11
    assert len(TABLOLAR) == 67  # 56 + 11
    assert set(MALI_TABLOLAR_DIGER) == {
        "progress_payments",
        "financial_instruments",
        "payroll_periods",
        "payroll_lines",
    }


def test_mali_sinifi_tam_liste_kilitli() -> None:
    """`is_financial` kapsamı (CEO eki). Değişirse silme kapısı değişir: bilerek güncelle."""
    kosulsuz = {t for t, b in TUM_TABLOLAR.items() if b.mali is True}
    kosullu = {t for t, b in TUM_TABLOLAR.items() if callable(b.mali)}
    assert kosulsuz == {
        "invoices",
        "invoice_lines",
        "payments",
        "journal_entries",
        "journal_lines",
        "financial_instruments",
        "payroll_periods",
        "payroll_lines",
    }
    assert kosullu == {
        "progress_payments",
        "progress_payment_lines",
        "subcontractor_progress_payments",
        "subcontractor_progress_payment_lines",
        "unit_sales",
        "sale_installments",
        "timesheet_entries",
    }


def test_silme_sirasi_her_turun_agacinda_kurulabilir_tablo_dongusu_yok() -> None:
    for tur in kayitli_turler().values():
        silinecek, _ = _ulasilan_tablolar(tur.tablo)
        sira = silme_sirasi(Base.metadata, silinecek)
        assert set(sira) == silinecek
        assert sira[-1] == tur.tablo  # kök EN SON silinir


def test_kancalar_kayitli_ve_hedef_tablolar_gercek() -> None:
    from app.core.silme.graf import kayitli_kancalar  # noqa: PLC0415

    adlar = {k.ad for k in kayitli_kancalar()}
    assert "journal_entries.source:invoice" in adlar
    assert "approval_chains.document:subcontractor_progress_payment" in adlar
    for k in kayitli_kancalar():
        assert k.ust_tablo in Base.metadata.tables, k.ad
        assert k.alt_tablo in Base.metadata.tables, k.ad
