"""IZN-B4b onarımı — bordro kaynaklı fiş (`payroll_period`) `maas_kisisel` ile de gizlenir.

Yalın yol: kaynak tipi yanıta taşınır (`source_type`) ve `KATEGORI_COZ` satır başına ek kategori
ekler. Elle fiş yalnız muhasebe kategorileriyle gizlenir (pozitif kontrol).
"""

from __future__ import annotations

import uuid
from datetime import date

from app.core.sayfalar import HiddenCategory
from app.modules.accounting.models import ChartAccountType, JournalSourceType
from tests._hassas_alan import rol_gizli
from tests.modules.accounting.conftest import _auth

H = HiddenCategory
_PAROLA = "parola1234"


async def _kurulum(client, seeded_db, user_factory, hesap_fabrikasi, fis_fabrikasi, gizli):
    gider = await hesap_fabrikasi(
        "770", name="Genel Yönetim", account_type=ChartAccountType.expense
    )
    kasa = await hesap_fabrikasi("100", name="Kasa", account_type=ChartAccountType.asset)
    bordro = await fis_fabrikasi(
        [(gider, "5000.00", "0"), (kasa, "0", "5000.00")],
        entry_date=date(2026, 7, 5),
        description="Bordro fişi",
    )
    bordro.source_type = JournalSourceType.payroll_period
    bordro.source_id = uuid.uuid4()
    elle = await fis_fabrikasi(
        [(gider, "700.00", "0"), (kasa, "0", "700.00")],
        entry_date=date(2026, 7, 6),
        description="Elle fiş",
    )
    await seeded_db.flush()
    await seeded_db.refresh(bordro)  # `updated_at` (onupdate) süresi dolar
    rol = await rol_gizli(seeded_db, "bordro_gizli", gizli)
    await user_factory(email="bordro@b4b.co", password=_PAROLA, role_key=rol.key)
    yanit = await client.post("/auth/login", json={"email": "bordro@b4b.co", "password": _PAROLA})
    return _auth(yanit.json()["access_token"]), bordro, elle


async def test_fis_ve_defter_bordro_satiri_maas_kisisel_gizliyken_NULL(
    client, seeded_db, user_factory, hesap_fabrikasi, fis_fabrikasi
):
    basliklar, bordro, elle = await _kurulum(
        client, seeded_db, user_factory, hesap_fabrikasi, fis_fabrikasi, {H.maas_kisisel}
    )

    detay_bordro = (await client.get(f"/journal-entries/{bordro.id}", headers=basliklar)).json()
    detay_elle = (await client.get(f"/journal-entries/{elle.id}", headers=basliklar)).json()
    defter = (await client.get("/journal?year=2026&month=7", headers=basliklar)).json()

    assert detay_bordro["source_type"] == "payroll_period"
    assert detay_bordro["total_debit"] is None and detay_bordro["total_credit"] is None
    assert all(s["debit"] is None and s["credit"] is None for s in detay_bordro["lines"])
    # POZİTİF KONTROL: elle fişte `maas_kisisel` etkisiz.
    assert detay_elle["total_debit"] == "700.00"
    assert detay_elle["lines"][0]["debit"] == "700.00"
    satirlar = {s["entry_id"]: s for s in defter["items"]}
    assert satirlar[str(bordro.id)]["debit"] is None or satirlar[str(bordro.id)]["credit"] is None
    assert satirlar[str(bordro.id)]["running_balance"] is None
    assert any(s["debit"] == "700.00" for s in defter["items"])


async def test_bayraksiz_rolde_bordro_fisi_ACIK(
    client, seeded_db, user_factory, hesap_fabrikasi, fis_fabrikasi
):
    """İkiz: hiçbir kategori gizli değilse bordro fişi de AÇIK (ek kategori yalnız bayrak varsa)."""
    basliklar, bordro, _ = await _kurulum(
        client, seeded_db, user_factory, hesap_fabrikasi, fis_fabrikasi, set()
    )

    detay = (await client.get(f"/journal-entries/{bordro.id}", headers=basliklar)).json()

    assert detay["total_debit"] == "5000.00"
