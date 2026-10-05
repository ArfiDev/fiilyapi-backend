"""OKT-B1 — `GET /approvals/history` (Onay Kutusu "Onay Verildi / Reddedildi / Tümü").

Karar ZİNCİR düzeyindedir (CEO kararı): `approved` = zincirin tüm adımları imzalı,
`rejected` = zincir reddedildi, `all` = görünür TÜM zincirler (süren zincirler
kartta `pending`). Görünürlük = proje kapsamı (bekleyen kutusuyla ORTAK IDOR
yardımcısı) + onay rollerimden biri zincirin HERHANGİ bir adımında.

Sıralama: karar zamanı azalan; süren zincirler (karar zamanı NULL) SONDA; sonra
`created_at` azalan, sonra `id`.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, update

from app.modules.approvals import service
from app.modules.approvals.models import ApprovalChain, ApprovalDocumentType, ApprovalRole
from tests.conftest import test_engine

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_GEREKCE = "Fiyat farkı hesabı hatalı"
_ZINCIR_ROLLERI = (
    ApprovalRole.site_chief,
    ApprovalRole.project_manager,
    ApprovalRole.accounting,
)
_SISTEM_ROLU = {
    ApprovalRole.site_chief: "site_chief",
    ApprovalRole.project_manager: "project_manager",
    ApprovalRole.accounting: "accounting",
}


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


async def _kur(seeded_db, document_id, yaratan):
    return await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )


async def _onayci(aktor_fabrikasi, etiket, rol, ad=None):
    return await aktor_fabrikasi(
        f"{etiket}-{rol.value}@okt-g.co",
        role_key=_SISTEM_ROLU[rol],
        approval_roles=[rol],
        full_name=ad or f"{etiket} {rol.value}",
    )


async def _ilerlet(seeded_db, aktor_fabrikasi, document_id, adet, etiket):
    """İlk `adet` adımı imzalar; SON imzayı atanı döner."""
    son = None
    for rol in _ZINCIR_ROLLERI[:adet]:
        son = await _onayci(aktor_fabrikasi, etiket, rol, ad=f"İmzacı {rol.value}")
        await service.approve_next_step(
            seeded_db, actor=son, document_type=_TASERON, document_id=document_id
        )
    return son


async def _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan, projeler=(None,) * 3):
    """Bir ONAYLANMIŞ · bir REDDEDİLMİŞ · bir SÜREN zincir kurar (bu sırayla, zaman artan).

    Taşeron fabrikası projeye TEK sözleşme no açar: aynı projede iki evrak çakışır,
    bu yüzden her evrak kendi projesine (`projeler`) düşer."""
    onayli, _ = await evrak_fabrikasi(_TASERON, creator=yaratan, project=projeler[0])
    reddedilen, _ = await evrak_fabrikasi(_TASERON, creator=yaratan, project=projeler[1])
    suren, _ = await evrak_fabrikasi(_TASERON, creator=yaratan, project=projeler[2])
    for belge in (onayli, reddedilen, suren):
        await _kur(seeded_db, belge, yaratan)
    await _ilerlet(seeded_db, aktor_fabrikasi, onayli, 3, f"on{uuid.uuid4().hex[:4]}")
    reddeden = await _onayci(
        aktor_fabrikasi, f"rd{uuid.uuid4().hex[:4]}", ApprovalRole.site_chief, ad="Reddeden Şef"
    )
    await service.reject_chain(
        seeded_db,
        actor=reddeden,
        document_type=_TASERON,
        document_id=reddedilen,
        reason=_GEREKCE,
    )
    await _ilerlet(seeded_db, aktor_fabrikasi, suren, 1, f"su{uuid.uuid4().hex[:4]}")
    return onayli, reddedilen, suren


async def _izleyici(aktor_fabrikasi, giris, email="okt-g-izleyici@okt-g.co", **kw):
    await aktor_fabrikasi(
        email,
        role_key=kw.pop("role_key", "accounting"),
        approval_roles=kw.pop("approval_roles", [ApprovalRole.accounting]),
        **kw,
    )
    return await giris(email)


def _karar_haritasi(govde):
    return {item["document_id"]: item for item in govde["items"]}


async def test_ALL_gorunur_TUM_zincirleri_son_durumlariyla_dondurur(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g1-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    onayli, reddedilen, suren = await _uc_durum(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan
    )

    yanit = await client.get("/approvals/history", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["total"] == 3
    assert "my_approval_roles" not in govde  # IZN-B3b: roller satırda (`can_decide`)
    harita = _karar_haritasi(govde)

    onay = harita[str(onayli)]
    assert onay["decision"] == "approved"
    assert onay["decided_by"] == "İmzacı accounting"  # SON imzayı atan
    assert onay["decided_at"] is not None
    assert onay["reason"] is None
    assert onay["current_step_no"] == 3
    assert onay["can_decide"] is False  # bitmiş zincir
    assert [a["decided_at"] is not None for a in onay["steps"]] == [True, True, True]

    red = harita[str(reddedilen)]
    assert red["decision"] == "rejected"
    assert red["decided_by"] == "Reddeden Şef"
    assert red["decided_at"] is not None
    assert red["reason"] == _GEREKCE
    assert red["current_step_no"] == 1  # reddedilen adım
    assert red["can_decide"] is False  # terminal zincir

    bekleyen = harita[str(suren)]
    assert bekleyen["decision"] == "pending"
    assert bekleyen["decided_by"] is None
    assert bekleyen["decided_at"] is None
    assert bekleyen["reason"] is None
    assert bekleyen["current_step_no"] == 2  # sıradaki adım
    assert bekleyen["can_decide"] is False  # sıradaki adım PM'in; izleyici muhasebe
    # Bekleyen kutusuyla AYNI kart alanları da gelir.
    for alan in ("chain_id", "document_type", "title", "gross_amount", "threshold_snapshot"):
        assert alan in bekleyen


async def test_decision_filtresi_SON_DURUMA_gore_suzer_total_suzulmus_gelir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g2-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    onayli, reddedilen, _ = await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)

    onaylanan = (await client.get("/approvals/history?decision=approved", headers=basliklar)).json()
    reddedilenler = (
        await client.get("/approvals/history?decision=rejected", headers=basliklar)
    ).json()

    assert [i["document_id"] for i in onaylanan["items"]] == [str(onayli)]
    assert onaylanan["total"] == 1
    assert [i["document_id"] for i in reddedilenler["items"]] == [str(reddedilen)]
    assert reddedilenler["total"] == 1


async def test_GECERSIZ_decision_422(client, aktor_fabrikasi, giris):
    basliklar = await _izleyici(aktor_fabrikasi, giris)

    for deger in ("pending", "bogus", ""):
        yanit = await client.get(f"/approvals/history?decision={deger}", headers=basliklar)
        assert yanit.status_code == 422, (deger, yanit.text)


async def test_SIRALAMA_karar_zamani_azalan_suren_SONDA_sonra_olusturulma_azalan(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g3-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    onayli, reddedilen, suren = await _uc_durum(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan
    )
    daha_eski, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    daha_yeni, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    for belge, gun in ((daha_eski, 30), (daha_yeni, 1)):
        zincir = await _kur(seeded_db, belge, yaratan)
        await seeded_db.execute(
            update(ApprovalChain)
            .where(ApprovalChain.id == zincir.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=gun))
        )
    await seeded_db.flush()

    sira = [
        (i["document_id"], i["decision"])
        for i in (await client.get("/approvals/history", headers=basliklar)).json()["items"]
    ]

    # `_uc_durum` önce onaylı zincirin imzalarını, SONRA reddi atar → ret daha yeni:
    # önce ret, sonra onaylı; sonra süren zincirler created_at azalan:
    # `suren` (şimdi) > daha_yeni (-1g) > daha_eski (-30g).
    assert sira == [
        (str(reddedilen), "rejected"),
        (str(onayli), "approved"),
        (str(suren), "pending"),
        (str(daha_yeni), "pending"),
        (str(daha_eski), "pending"),
    ]


async def test_sayfalama_total_sabit_kalir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g4-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)

    sayfa = (await client.get("/approvals/history?limit=1&offset=1", headers=basliklar)).json()

    assert sayfa["total"] == 3
    assert len(sayfa["items"]) == 1
    assert sayfa["items"][0]["decision"] == "approved"  # sıra: RET · onaylı · süren
    assert (sayfa["limit"], sayfa["offset"]) == (1, 1)


async def test_IDOR_proje_kapsami_disindaki_zincirler_ne_items_ne_total_olarak_gorunur(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris, project_factory
):
    yaratan = await aktor_fabrikasi("okt-g5-yaratan@okt-g.co")
    gorunen = [await project_factory(code=f"OKTG-A{n}", name=f"Görünen {n}") for n in range(3)]
    gizli = [await project_factory(code=f"OKTG-B{n}", name=f"Gizli {n}") for n in range(3)]
    basliklar = await _izleyici(
        aktor_fabrikasi, giris, email="okt-g5-kapsamli@okt-g.co", projeler=gorunen
    )
    gorunen_onayli, gorunen_ret, _ = await _uc_durum(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan, projeler=gorunen
    )
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan, projeler=gizli)

    for karar, beklenen in (("all", 3), ("approved", 1), ("rejected", 1)):
        govde = (await client.get(f"/approvals/history?decision={karar}", headers=basliklar)).json()
        assert govde["total"] == beklenen, karar  # total de süzgeçte (BOR-TEMİZ)
        assert len(govde["items"]) == beklenen, karar
    govde = (await client.get("/approvals/history", headers=basliklar)).json()
    assert {i["document_id"] for i in govde["items"]} >= {str(gorunen_onayli), str(gorunen_ret)}


async def test_proje_erisimi_HIC_yoksa_bos_doner(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g6-yaratan@okt-g.co")
    basliklar = await _izleyici(
        aktor_fabrikasi, giris, email="okt-g6-kapsamsiz@okt-g.co", tum_projeler=False
    )
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)

    govde = (await client.get("/approvals/history", headers=basliklar)).json()

    assert govde["items"] == [] and govde["total"] == 0


async def test_ROL_gorunurlugu_zincirin_HICBIR_adiminda_rolum_yoksa_gorunmez(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g7-yaratan@okt-g.co")
    # Taşeron zinciri: şef · PM · muhasebe. `procurement` rolü HİÇBİR adımda yok.
    satinalmaci = await _izleyici(
        aktor_fabrikasi,
        giris,
        email="okt-g7-satinalma@okt-g.co",
        role_key="procurement",
        approval_roles=[ApprovalRole.procurement],
    )
    # Hiçbir projede adım rolü taşımayan aktör (ana rol `hr_manager` bir adım rolü DEĞİL):
    # sorgu bile açılmaz.
    rolsuz = await _izleyici(
        aktor_fabrikasi,
        giris,
        email="okt-g7-rolsuz@okt-g.co",
        role_key="hr_manager",
        approval_roles=[],
    )
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)

    for basliklar in (satinalmaci, rolsuz):
        govde = (await client.get("/approvals/history", headers=basliklar)).json()
        assert govde["items"] == [] and govde["total"] == 0


async def test_reddeden_kullanici_silinmisse_decided_by_null_kayit_durur(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-g8-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    _, reddedilen, _ = await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)
    # `ON DELETE SET NULL` sonucu: kullanıcı silinince alan NULL olur.
    await seeded_db.execute(
        update(ApprovalChain)
        .where(ApprovalChain.document_id == reddedilen)
        .values(rejected_by_user_id=None)
    )
    await seeded_db.flush()

    govde = (await client.get("/approvals/history?decision=rejected", headers=basliklar)).json()

    assert govde["total"] == 1
    assert govde["items"][0]["decided_by"] is None
    assert govde["items"][0]["reason"] == _GEREKCE


async def test_gecmis_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """`test_ok1a_query_count.py` deseni: 3 → 9 satırda ifade sayısı AYNI kalmalı."""
    yaratan = await aktor_fabrikasi("okt-g9-yaratan@okt-g.co")
    basliklar = await _izleyici(aktor_fabrikasi, giris)
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)
    with _sorgu_sayaci() as uc_satir:
        yanit = await client.get("/approvals/history", headers=basliklar)
    assert yanit.json()["total"] == 3, yanit.text

    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)
    await _uc_durum(seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan)
    with _sorgu_sayaci() as dokuz_satir:
        yanit = await client.get("/approvals/history", headers=basliklar)
    assert yanit.json()["total"] == 9, yanit.text

    assert len(dokuz_satir) == len(uc_satir), (
        f"3→9 satırda sorgu sayısı {len(uc_satir)}→{len(dokuz_satir)} oldu — N+1"
    )


async def test_CAN_DECIDE_yalniz_SIRADAKI_adimin_sahibi_ve_acik_zincirde_true(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """IZN-B3b: `can_decide` satır başına OLGUDUR. Süren zincirin sıradaki adımı (PM) kimindeyse
    onda `true`; onaylanmış / reddedilmiş zincirde ve başkasının adımında `false`.

    İki karşıt izleyici AYNI testtedir: muhasebe (adım 3'te rolü var, sıra gelmedi) ve PM
    (sıradaki adım onun). Sabit `true` ya da sabit `false` yazan bir uygulama birini kırar.
    """
    yaratan = await aktor_fabrikasi("okt-g10-yaratan@okt-g.co")
    muhasebe = await _izleyici(aktor_fabrikasi, giris, email="okt-g10-muh@okt-g.co")
    pm = await _izleyici(
        aktor_fabrikasi,
        giris,
        email="okt-g10-pm@okt-g.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    onayli, reddedilen, suren = await _uc_durum(
        seeded_db, aktor_fabrikasi, evrak_fabrikasi, yaratan
    )

    muh_harita = _karar_haritasi((await client.get("/approvals/history", headers=muhasebe)).json())
    pm_harita = _karar_haritasi((await client.get("/approvals/history", headers=pm)).json())

    assert {k: v["can_decide"] for k, v in muh_harita.items()} == {
        str(onayli): False,
        str(reddedilen): False,
        str(suren): False,
    }
    assert {k: v["can_decide"] for k, v in pm_harita.items()} == {
        str(onayli): False,
        str(reddedilen): False,
        str(suren): True,
    }
    # Kutu ile TUTARLI: PM'in kutusunda tam bu satır var, muhasebenin kutusunda yok.
    pm_kutu = (await client.get("/approvals", headers=pm)).json()
    assert [i["document_id"] for i in pm_kutu["items"]] == [str(suren)]
    assert pm_kutu["items"][0]["can_decide"] is True
    assert (await client.get("/approvals", headers=muhasebe)).json()["items"] == []
