"""OKT-B1 — RET KAYDI (zincir silinmez) + `GET /approvals/history`.

Kullanıcı kararı (KARARLAR 62ae58a): Onay Kutusu'nda "Onay Verildi / Reddedildi /
Tümü" sekmeleri çalışır; reddedilen zincir artık SİLİNMEZ (eski K2'nin üstüne
yazar). Bu dosyanın üç ağır sorumluluğu:

1. (a) ret sonrası zincir + adımlar DB'de durur; reddeden/zaman/gerekçe dolu.
2. (b) aynı evrak yeniden onaya gönderilebilir ve YENİ zincir açılır — kısmi
   unique indeks (`WHERE rejected_at IS NULL`) reddedilmiş kayıtları dışlar.
   ⚠️ Satınalma talebinde ret TERMİNALDİR (`rejected` → hiçbir geçiş yok), yeniden
   gönderim yalnız iki hakediş ailesinde uçtan uca vardır; satınalmada motor
   düzeyinde (aynı `document_id` için yeni zincir) ölçülür.
3. (c) reddedilmiş zincir bekleyen kutusunda / açık zincir okumalarında / ikame
   kapısında SAYILMAZ.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError
from app.modules.approvals import guards, repository, service
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_ISVEREN = ApprovalDocumentType.progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request

_YOL = {
    _TASERON: "/subcontractor-progress-payments",
    _ISVEREN: "/progress-payments",
}
_GEREKCE = "Metraj sayfası eksik"
_TUTAR = Decimal("100.00")


async def _zincirler(session, tip, document_id) -> list[ApprovalChain]:
    rows = await session.execute(
        select(ApprovalChain)
        .where(ApprovalChain.document_type == tip, ApprovalChain.document_id == document_id)
        .order_by(ApprovalChain.created_at, ApprovalChain.id)
    )
    return list(rows.scalars())


async def _kur(seeded_db, tip, document_id, yaratan):
    return await service.create_chain(
        seeded_db,
        document_type=tip,
        document_id=document_id,
        amount=_TUTAR,
        created_by_user_id=yaratan.id,
    )


async def _reddet(seeded_db, tip, document_id, reddeden, gerekce=_GEREKCE):
    return await service.reject_chain(
        seeded_db, actor=reddeden, document_type=tip, document_id=document_id, reason=gerekce
    )


# --------------------------------------------------------------------------- #
# (a) Ret zinciri SİLMEZ
# --------------------------------------------------------------------------- #


async def test_RET_sonrasi_zincir_ve_adimlar_DB_de_DURUR_reddeden_zaman_gerekce_dolu(
    seeded_db, aktor_fabrikasi
):
    yaratan = await aktor_fabrikasi("okt-a-yaratan@okt.co")
    sef = await aktor_fabrikasi(
        "okt-a-sef@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    pm = await aktor_fabrikasi(
        "okt-a-pm@okt.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
        full_name="Pınar Müdür",
    )
    document_id = uuid.uuid4()
    zincir = await _kur(seeded_db, _TASERON, document_id, yaratan)
    await service.approve_next_step(
        seeded_db, actor=sef, document_type=_TASERON, document_id=document_id
    )

    await _reddet(seeded_db, _TASERON, document_id, pm)

    kayitlar = await _zincirler(seeded_db, _TASERON, document_id)
    assert [k.id for k in kayitlar] == [zincir.id], "ret zinciri SİLMEMELİ"
    assert kayitlar[0].rejected_by_user_id == pm.id
    assert kayitlar[0].rejected_at is not None
    assert kayitlar[0].rejection_reason == _GEREKCE
    adimlar = list(
        (
            await seeded_db.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id == zincir.id)
                .order_by(ApprovalStep.step_no)
            )
        ).scalars()
    )
    assert len(adimlar) == 3
    # 1. adımın ONAYI durur; reddedilen 2. adım karara bağlanmamış kalır.
    assert [a.decided_at is not None for a in adimlar] == [True, False, False]
    assert adimlar[0].decided_by_user_id == sef.id


async def test_ret_damgasi_GEREKCESIZ_yazilamaz_DB_kisiti(seeded_db, aktor_fabrikasi):
    """`ck_approval_chains_rejection_pair`: `rejected_at` dolu ⇒ gerekçe dolu."""
    yaratan = await aktor_fabrikasi("okt-ck-yaratan@okt.co")
    zincir = await _kur(seeded_db, _TASERON, uuid.uuid4(), yaratan)

    with pytest.raises(IntegrityError):
        await seeded_db.execute(
            text("UPDATE approval_chains SET rejected_at = now() WHERE id = :id"),
            {"id": zincir.id},
        )
    await seeded_db.rollback()


# --------------------------------------------------------------------------- #
# (b) Yeniden gönderim YENİ zincir açar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tip", [_TASERON, _ISVEREN])
async def test_HAKEDIS_reddedilip_yeniden_gonderilince_YENI_zincir_acilir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris, tip
):
    """Uçtan uca: reject → draft → submit. Eski reddedilmiş kayıt DURUR, yenisi açılır."""
    etiket = tip.value[:4]
    yaratan = await aktor_fabrikasi(f"okt-b-{etiket}-yaratan@okt.co")
    await aktor_fabrikasi(f"okt-b-{etiket}-admin@okt.co", role_key="system_admin")
    rol = ApprovalRole.site_chief if tip is _TASERON else ApprovalRole.accounting
    await aktor_fabrikasi(
        f"okt-b-{etiket}-onay@okt.co",
        role_key="site_chief" if tip is _TASERON else "accounting",
        approval_roles=[rol],
    )
    onay = await giris(f"okt-b-{etiket}-onay@okt.co")
    admin = await giris(f"okt-b-{etiket}-admin@okt.co")
    document_id, _ = await evrak_fabrikasi(tip, creator=yaratan, period=(2026, 7))
    ilk = await _kur(seeded_db, tip, document_id, yaratan)

    ret = await client.post(
        f"{_YOL[tip]}/{document_id}/reject", json={"reason": _GEREKCE}, headers=onay
    )
    assert ret.status_code == 200, ret.text
    assert ret.json()["status"] == "draft"

    yeniden = await client.post(f"{_YOL[tip]}/{document_id}/submit", headers=admin)
    assert yeniden.status_code == 200, yeniden.text
    assert yeniden.json()["status"] == "pending_approval"

    kayitlar = await _zincirler(seeded_db, tip, document_id)
    assert len(kayitlar) == 2
    eski, yeni = kayitlar
    assert eski.id == ilk.id and eski.rejected_at is not None
    assert yeni.id != ilk.id and yeni.rejected_at is None
    acik = await service.open_chain(seeded_db, tip, document_id)
    assert acik is not None and acik.id == yeni.id


async def test_SATINALMA_reddedilmis_evrak_icin_motor_YENI_zincir_acar(seeded_db, aktor_fabrikasi):
    """Satınalmada ret terminaldir (talep yeniden gönderilemez); yine de motor
    düzeyinde "ret kaydı açık zincir sayılmaz" kuralı üç aile için AYNI olmalı."""
    yaratan = await aktor_fabrikasi("okt-b-sat-yaratan@okt.co")
    sat = await aktor_fabrikasi(
        "okt-b-sat-onay@okt.co",
        role_key="procurement",
        approval_roles=[ApprovalRole.procurement],
    )
    document_id = uuid.uuid4()
    ilk = await _kur(seeded_db, _SATINALMA, document_id, yaratan)
    await _reddet(seeded_db, _SATINALMA, document_id, sat)

    yeni = await _kur(seeded_db, _SATINALMA, document_id, yaratan)

    assert yeni.id != ilk.id
    assert len(await _zincirler(seeded_db, _SATINALMA, document_id)) == 2


async def test_UQ_acik_zincir_TEK_reddedilmisler_BIRIKEBILIR(seeded_db, aktor_fabrikasi):
    """Kısmi unique indeks: iki AÇIK zincir DB'de reddedilir; reddedilmişler birikir."""
    yaratan = await aktor_fabrikasi("okt-uq-yaratan@okt.co")
    sef = await aktor_fabrikasi(
        "okt-uq-sef@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = uuid.uuid4()
    for _ in range(2):  # iki tur ret → iki reddedilmiş kayıt
        await _kur(seeded_db, _TASERON, document_id, yaratan)
        await _reddet(seeded_db, _TASERON, document_id, sef)
    await _kur(seeded_db, _TASERON, document_id, yaratan)
    assert len(await _zincirler(seeded_db, _TASERON, document_id)) == 3

    # Servis korkuluğunu atlayıp DB kısıtını doğrudan sına: ikinci AÇIK zincir.
    seeded_db.add(
        ApprovalChain(
            document_type=_TASERON,
            document_id=document_id,
            threshold_snapshot=Decimal("500000.00"),
            amount_snapshot=_TUTAR,
        )
    )
    with pytest.raises(IntegrityError):
        await seeded_db.flush()
    await seeded_db.rollback()


# --------------------------------------------------------------------------- #
# (c) Reddedilmiş zincir bekleyen / açık sayılmaz
# --------------------------------------------------------------------------- #


async def test_reddedilmis_zincir_BEKLEYEN_kutusunda_GORUNMEZ_sayac_dahil(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("okt-c-yaratan@okt.co")
    await aktor_fabrikasi(
        "okt-c-sef@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    reddeden = await aktor_fabrikasi(
        "okt-c-reddeden@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("okt-c-sef@okt.co")
    reddedilen_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    bekleyen_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _kur(seeded_db, _TASERON, reddedilen_id, yaratan)
    await _kur(seeded_db, _TASERON, bekleyen_id, yaratan)
    onceki = await client.get("/approvals", headers=basliklar)
    assert onceki.json()["total"] == 2

    await _reddet(seeded_db, _TASERON, reddedilen_id, reddeden)

    yanit = await client.get("/approvals", headers=basliklar)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 1
    assert [i["document_id"] for i in yanit.json()["items"]] == [str(bekleyen_id)]


async def test_reddedilmis_zincir_ACIK_zincir_okumalarinda_ve_kapida_SAYILMAZ(
    seeded_db, aktor_fabrikasi
):
    yaratan = await aktor_fabrikasi("okt-c2-yaratan@okt.co")
    sef = await aktor_fabrikasi(
        "okt-c2-sef@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    adayi = await aktor_fabrikasi(
        "okt-c2-aday@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    document_id = uuid.uuid4()
    await _kur(seeded_db, _TASERON, document_id, yaratan)
    await _reddet(seeded_db, _TASERON, document_id, sef)

    assert await service.open_chain(seeded_db, _TASERON, document_id) is None
    # `repository.get_chain_for_update` yolu: zorunlu zincir yok → 409, esnek yol → None.
    with pytest.raises(ConflictError) as hata:
        await service.approve_next_step(
            seeded_db, actor=adayi, document_type=_TASERON, document_id=document_id
        )
    assert str(hata.value) == guards.NO_OPEN_CHAIN
    assert (
        await service.approve_next_step(
            seeded_db,
            actor=adayi,
            document_type=_TASERON,
            document_id=document_id,
            require_chain=False,
        )
        is None
    )
    # İkame kapısı: reddedilmiş zincirin karara bağlanmamış adımı kapı AÇMAZ.
    assert not (
        await repository.chain_gate_facts(
            seeded_db, actor_id=adayi.id, document_type=_TASERON, document_id=document_id
        )
    ).holds_next_step_role


async def test_ret_GERI_SARMAYI_etkilemez_acik_zincir_yoksa_rewind_None(seeded_db, aktor_fabrikasi):
    yaratan = await aktor_fabrikasi("okt-rw-yaratan@okt.co")
    sef = await aktor_fabrikasi(
        "okt-rw-sef@okt.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    pm = await aktor_fabrikasi(
        "okt-rw-pm@okt.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    document_id = uuid.uuid4()
    await _kur(seeded_db, _TASERON, document_id, yaratan)
    await service.approve_next_step(
        seeded_db, actor=sef, document_type=_TASERON, document_id=document_id
    )
    await _reddet(seeded_db, _TASERON, document_id, pm)

    # Reddedilmiş zincirin 1. imzası geri SARILMAZ (zincir açık değil).
    assert (
        await service.rewind_last_step(seeded_db, document_type=_TASERON, document_id=document_id)
        is None
    )
