"""OK-1A — 🔴 N+1 YOK: `GET /approvals`in sorgu sayısı SATIR SAYISINDAN BAĞIMSIZ.

İddia tahmine değil ÖLÇÜME dayanır: sürücüye giden HER ifade sayılır
(`tests/subcontractor_progress_payments/test_work_category.py:24-37` deseni).

Bir satır ile ON satır AYNI ifade sayısını üretmelidir. Eşitlik iddiası mutlak
bir tavandan daha güçlüdür: tavan, dilim büyüdükçe sessizce gevşetilebilir;
eşitlik ise satır başına ek sorgu eklendiği anda kırılır.

🔴 T4 UYARLAMASI — ÖLÇÜM ARTIK GERÇEK EVRAKLA YAPILIR. Satır artık üç evrak
ailesinden zenginleştiriliyor (başlık · alt başlık · brüt/net) ve uydurma bir
`document_id` görünürlük süzgecine takılıp kutuya HİÇ girmiyor. Eski kurulum bu
turdan sonra "sıfır satırın sorgu sayısı sabittir" gibi BOŞ bir şey ölçerdi —
yani tam da bekçilemesi gereken N+1'i göremez hâle gelirdi.

Sayfadaki AİLE KARIŞIMI iki ölçümde de AYNIDIR ve bu kasıtlıdır: aile başına
sabit sayıda sorgu KABUL EDİLİR (satır başına DEĞİL), dolayısıyla eşitlik
iddiası ancak aynı aile karışımı üzerinde anlamlıdır.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import event

from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from tests.conftest import test_engine
from tests.modules.approvals.conftest import proje_rolu_ver

_TASERON = ApprovalDocumentType.subcontractor_progress_payment


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def _zincirler(seeded_db, evrak_fabrikasi, yaratanlar, adet: int) -> None:
    """Her zincirin YARATANI ve PROJESİ FARKLIDIR.

    İki ayrı N+1 tuzağı aynı anda kurulur: `created_by` adını satır başına ayrı
    bir sorguyla çözen bir uygulama da, evrağın projesini/sözleşmesini satır
    başına çeken bir uygulama da tam burada patlar.
    """
    for sira in range(adet):
        yaratan = yaratanlar[sira % len(yaratanlar)]
        document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
        await service.create_chain(
            seeded_db,
            document_type=_TASERON,
            document_id=document_id,
            amount=Decimal("100.00"),
            created_by_user_id=yaratan.id,
        )


async def test_onay_kutusu_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratanlar = [
        await aktor_fabrikasi(f"n1-yaratan-{sira}@ok1a.co", full_name=f"Yaratan {sira}")
        for sira in range(5)
    ]
    await aktor_fabrikasi(
        "n1-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("n1-sef@ok1a.co")

    await _zincirler(seeded_db, evrak_fabrikasi, yaratanlar, 1)
    with _sorgu_sayaci() as tek_satir:
        yanit = await client.get("/approvals", headers=basliklar)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 1

    await _zincirler(seeded_db, evrak_fabrikasi, yaratanlar, 9)
    with _sorgu_sayaci() as on_satir:
        yanit = await client.get("/approvals", headers=basliklar)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 10

    assert len(on_satir) == len(tek_satir), (
        f"satır sayısı 1→10 olunca sorgu sayısı {len(tek_satir)}→{len(on_satir)} oldu — N+1"
    )


async def test_UC_AILE_birlikteyken_de_sorgu_sayisi_SABIT_kalir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Aile başına sabit ⇒ ÜÇ ailelik bir sayfada da satır sayısı sorgu
    sayısını oynatmamalı. Aile karışımı iki ölçümde de aynıdır; değişen tek şey
    aile BAŞINA düşen satır sayısıdır (1 → 3)."""
    yaratan = await aktor_fabrikasi("n1-uclu-yaratan@ok1a.co")
    # IZN-B3b: tek kişi tek projede tek rol → her ailenin projesinde O AİLENİN ilk adım rolüyle üye.
    aktor = await aktor_fabrikasi("n1-uclu@ok1a.co", role_key="hr_manager", tum_projeler=False)
    basliklar = await giris("n1-uclu@ok1a.co")
    tipler = (
        _TASERON,
        ApprovalDocumentType.progress_payment,
        ApprovalDocumentType.purchase_request,
    )
    ilk_adim_rolu = {
        _TASERON: ApprovalRole.site_chief,
        ApprovalDocumentType.progress_payment: ApprovalRole.accounting,
        ApprovalDocumentType.purchase_request: ApprovalRole.procurement,
    }

    async def _tur(tekrar: int) -> None:
        for _ in range(tekrar):
            for tip in tipler:
                document_id, proje = await evrak_fabrikasi(tip, creator=yaratan)
                await proje_rolu_ver(seeded_db, aktor, proje, ilk_adim_rolu[tip].value)
                await service.create_chain(
                    seeded_db,
                    document_type=tip,
                    document_id=document_id,
                    amount=Decimal("100.00"),
                    created_by_user_id=yaratan.id,
                )

    await _tur(1)
    with _sorgu_sayaci() as uc_satir:
        yanit = await client.get("/approvals", headers=basliklar)
    assert yanit.json()["total"] == 3, yanit.text

    await _tur(2)
    with _sorgu_sayaci() as dokuz_satir:
        yanit = await client.get("/approvals", headers=basliklar)
    assert yanit.json()["total"] == 9, yanit.text

    assert len(dokuz_satir) == len(uc_satir), (
        f"3→9 satırda sorgu sayısı {len(uc_satir)}→{len(dokuz_satir)} oldu — N+1"
    )
