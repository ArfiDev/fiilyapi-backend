"""SIL-B1 — `/admin/silme/{kind}/{id}`: önizleme, silme, belirteç, kapı, denetim."""

import pytest
from sqlalchemy import select, text

from app.core.db import Base
from app.modules.approvals.models import ApprovalChain, ApprovalStep
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.contracts.models import SubcontractorContract
from app.modules.personnel.models import Personnel
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import SubcontractorPaymentStatus
from app.modules.units.models import Block, Unit
from tests._silme_yardimci import onizle, rol_girisi, sil_aile, sil_genel, sisyon_girisi
from tests.modules.silme import _dunya as d

PREVIEW_STALE = "Silinecek kayıtlar değişti; önizlemeyi yenileyin"
PREVIEW_REQUIRED = "Silmeden önce önizleme alınmalı; önizlemeyi açıp onaylayın"
FINANCIAL_PENDING = (
    "Bu kaydın bağlı mali kayıtları var; mali kayıt silme bir sonraki sürümde açılacak"
)
SYSTEM_ADMIN_ONLY = "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"


async def _tablo_sayimlari(session) -> dict[str, int]:
    """TÜM tabloların satır sayısı: "başka hiçbir şeye dokunulmadı" kanıtının zemini.

    `audit_log` HARİÇ: giriş (login) denetim satırı yazar; silme denetimi ayrıca sayılır
    (`_silme_denetim_satiri`).
    """
    sayilar: dict[str, int] = {}
    for tablo in Base.metadata.tables:
        if tablo == "audit_log":
            continue
        sayilar[tablo] = int(
            (await session.execute(text(f'SELECT count(*) FROM "{tablo}"'))).scalar_one()
        )
    return sayilar


async def _silme_denetim_satiri(session) -> int:
    sorgu = select(AuditLog).where(AuditLog.action == AuditAction.delete)
    return len((await session.execute(sorgu)).scalars().all())


async def _hicbir_sey_degismedi(session, once: dict[str, int]) -> None:
    assert _fark(once, await _tablo_sayimlari(session)) == {}
    assert await _silme_denetim_satiri(session) == 0  # engellenen silme denetime yazmaz


def _fark(once: dict[str, int], sonra: dict[str, int]) -> dict[str, int]:
    return {t: once[t] - sonra[t] for t in once if once[t] != sonra[t]}


@pytest.fixture
async def sistem(client, user_factory):
    return await sisyon_girisi(client, user_factory)


@pytest.fixture
async def olusturan(user_factory):
    return await user_factory(
        email="olusturan@silme.co", password="parola1234", role_key="site_chief"
    )


@pytest.fixture
async def dolu_santiye(db_session, project_factory, olusturan):
    """Şantiyeyi her bağ türüyle dolduran dünya (mali kayıt YOK)."""
    proje = await project_factory("SIL-1")
    snt = await d.site(db_session, proje)
    bolum = await d.section(db_session, snt)
    await d.section(db_session, snt, "Ince Isler")
    await d.boq(db_session, snt)
    blok = await d.block(db_session, proje, snt)
    await d.unit(db_session, proje, blok, "1")
    await d.unit(db_session, proje, blok, "2")
    kisi = await d.personel(db_session)
    kisi.assigned_section_id = bolum.id  # SET NULL bacağı: silinmez, bağı kopar
    await d.puantaj(db_session, proje, snt, kisi, olusturan)
    await d.gunluk(db_session, proje, snt, olusturan)
    await d.belge(db_session, proje, snt)
    await d.klasor(db_session, proje, snt)
    await d.plan(db_session, proje, snt)
    await d.hakedis_satiri(db_session, proje, snt, olusturan)  # taslak: mali DEĞİL
    odeme = await d.tasaron_hakedisi(db_session, proje, snt, olusturan)  # taslak
    await d.onay_zinciri(db_session, odeme, olusturan)  # FK dışı bağ (kanca)
    await db_session.flush()
    return {"proje": proje, "site": snt, "section": bolum, "kisi": kisi, "odeme": odeme}


# --- Kapı ---


@pytest.mark.parametrize("rol", ["project_manager", "site_chief", "patron"])
async def test_sistem_yoneticisi_disinda_herkes_403_ve_hicbir_sey_silinmez(
    client, user_factory, db_session, dolu_santiye, rol
) -> None:
    baslik = await rol_girisi(client, user_factory, rol)
    snt = dolu_santiye["site"]
    once = await _tablo_sayimlari(db_session)

    onizleme = await onizle(client, baslik, "site", snt.id)
    genel = await client.delete(
        f"/admin/silme/site/{snt.id}", params={"preview_token": "x"}, headers=baslik
    )
    aile = await client.delete(f"/sites/{snt.id}", params={"preview_token": "x"}, headers=baslik)

    for yanit in (onizleme, genel, aile):
        assert yanit.status_code == 403
        assert yanit.json() == {"detail": SYSTEM_ADMIN_ONLY}
    await _hicbir_sey_degismedi(db_session, once)


# --- Önizleme = silme ---


async def test_onizleme_sayilari_gercek_silinenle_birebir_ayni(
    client, db_session, sistem, dolu_santiye
) -> None:
    snt = dolu_santiye["site"]
    once = await _tablo_sayimlari(db_session)

    yanit = await onizle(client, sistem, "site", snt.id)
    assert yanit.status_code == 200
    onizleme = yanit.json()
    gruplar = {g["table"]: g for g in onizleme["groups"]}
    # Her bağ türü önizlemede görünür
    for tablo in (
        "sections", "boq_items", "blocks", "units", "timesheet_entries", "site_diary_entries",
        "documents", "document_folders", "site_plan_rows", "progress_payment_lines",
        "subcontractor_contracts", "subcontractor_progress_payments", "approval_chains",
        "approval_steps",
    ):  # fmt: skip
        assert tablo in gruplar, tablo
    assert gruplar["approval_chains"]["relation"] == "linked"  # FK dışı kanca
    assert gruplar["units"]["relation"] == "restrict"  # normalde engelleyen bağ
    assert gruplar["sections"]["relation"] == "cascade"
    assert gruplar["units"]["samples"] == ["1", "2"]
    assert gruplar["sections"]["samples"] == ["Ince Isler", "Kaba İnşaat"]  # ada göre
    assert all(g["is_financial"] is False for g in gruplar.values())
    assert [g["table"] for g in onizleme["detached"]] == ["personnel"]
    assert onizleme["label"] == snt.name
    assert onizleme["kind_label"] == "Şantiye"
    assert onizleme["dependent_count"] == sum(g["count"] for g in onizleme["groups"])

    silme = await client.delete(
        f"/admin/silme/site/{snt.id}",
        params={"preview_token": onizleme["preview_token"]},
        headers=sistem,
    )

    assert silme.status_code == 204
    beklenen = {t: g["count"] for t, g in gruplar.items()}
    beklenen["sites"] = 1
    # Silinen = önizlenen: ne eksik ne fazla (CASCADE'in sessizce sildiği satır YOK)
    assert _fark(once, await _tablo_sayimlari(db_session)) == beklenen
    # SET NULL bacağı: personel durur, bölüm bağı kopar
    db_session.expunge_all()
    kisi = await db_session.get(Personnel, dolu_santiye["kisi"].id)
    assert kisi is not None and kisi.assigned_section_id is None


async def test_ornek_adlar_ada_gore_ilk_bes_dogal_sira(
    client, db_session, sistem, project_factory
) -> None:
    """Örnekler `ORDER BY ad LIMIT 5` ile seçilir (eskiden 1000 kimlik UUID'ye göre seçilip sonra
    sıralanırdı): ekleme sırası ne olursa olsun ilk beş AD önizlemede görünür."""
    proje = await project_factory("SIL-ORNEK")
    snt = await d.site(db_session, proje)
    for sira in (7, 3, 9, 1, 0, 8, 5, 2, 6, 4):  # karışık ekleme sırası
        await d.section(db_session, snt, f"Bolum {sira}")

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    bolumler = next(g for g in onizleme["groups"] if g["table"] == "sections")

    assert bolumler["count"] == 10
    assert bolumler["samples"] == [f"Bolum {n}" for n in range(5)]


async def test_aile_ucu_genel_uclada_ayni_agaci_siler(
    client, db_session, sistem, dolu_santiye
) -> None:
    snt = dolu_santiye["site"]

    yanit = await sil_aile(client, sistem, "site", snt.id)

    assert yanit.status_code == 204
    assert await d.sayim(db_session, Site, Site.id == snt.id) == 0
    assert await d.sayim(db_session, Block) == 0


async def test_denetim_tek_satir_kim_ne_kac_bagli_kayit(
    client, db_session, sistem, dolu_santiye
) -> None:
    snt = dolu_santiye["site"]
    onizleme = (await onizle(client, sistem, "site", snt.id)).json()

    await sil_genel(client, sistem, "site", snt.id)

    satirlar = (
        (await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.delete)))
        .scalars()
        .all()
    )
    assert len(satirlar) == 1
    detay = satirlar[0].detail
    assert detay.startswith(f"Şantiye silindi: {dolu_santiye['proje'].name} · {snt.name} · ")
    assert f"{onizleme['dependent_count']} bağlı kayıtla birlikte silindi" in detay
    assert satirlar[0].actor_user_id is not None
    # TAM "tür → sayı" dökümü (plan §4): önizlemedeki HER grup, kesilmeden, aynı sırayla
    dokum = ", ".join(f"{g['label']} {g['count']}" for g in onizleme["groups"])
    assert f"({dokum})" in detay
    assert len(onizleme["groups"]) > 5  # eski "ilk 5 + …" sınırından UZUN döküm
    assert "…" not in detay
    # bağı kopan (silinmeyen) kayıtlar AYRI parça olarak yazılır
    assert detay.endswith(" · bağı kopan (silinmedi): Personel 1")


async def test_bagli_kaydi_olmayan_silmenin_denetim_metni_eskisiyle_ayni(
    client, db_session, sistem, project_factory
) -> None:
    proje = await project_factory("SIL-BOS")
    snt = await d.site(db_session, proje)

    yanit = await sil_genel(client, sistem, "site", snt.id)

    assert yanit.status_code == 204
    satir = (await db_session.execute(select(AuditLog))).scalars().all()[-1]
    assert satir.detail == f"Şantiye silindi: {proje.name} · {snt.name}"


# --- Belirteç ---


async def test_belirtecsiz_silme_428_ve_hicbir_sey_silinmez(
    client, db_session, sistem, dolu_santiye
) -> None:
    snt = dolu_santiye["site"]
    once = await _tablo_sayimlari(db_session)

    genel = await client.delete(f"/admin/silme/site/{snt.id}", headers=sistem)
    aile = await client.delete(f"/sites/{snt.id}", headers=sistem)

    for yanit in (genel, aile):
        assert yanit.status_code == 428
        assert yanit.json() == {"detail": PREVIEW_REQUIRED, "code": "preview_required"}
    await _hicbir_sey_degismedi(db_session, once)


async def test_onizlemeden_sonra_baglanti_degisirse_409_stale_ve_hicbir_sey_silinmez(
    client, db_session, sistem, dolu_santiye
) -> None:
    snt = dolu_santiye["site"]
    eski = (await onizle(client, sistem, "site", snt.id)).json()["preview_token"]
    await d.section(db_session, snt, "Sonradan Eklenen")  # ağaç değişti
    once = await _tablo_sayimlari(db_session)

    yanit = await client.delete(
        f"/admin/silme/site/{snt.id}", params={"preview_token": eski}, headers=sistem
    )

    assert yanit.status_code == 409
    assert yanit.json() == {"detail": PREVIEW_STALE, "code": "preview_stale"}
    await _hicbir_sey_degismedi(db_session, once)
    # önizleme yenilenince silme geçer
    assert (await sil_genel(client, sistem, "site", snt.id)).status_code == 204


async def test_bagi_kopacak_kayit_degisirse_de_belirtec_bayatlar(
    client, db_session, sistem, dolu_santiye
) -> None:
    """SET NULL satırları silinmez ama kullanıcının gördüğü listenin parçasıdır."""
    bolum = dolu_santiye["section"]
    eski = (await onizle(client, sistem, "section", bolum.id)).json()["preview_token"]
    yeni_kisi = await d.personel(db_session, "Yeni Personel")
    yeni_kisi.assigned_section_id = bolum.id
    await db_session.flush()

    yanit = await client.delete(
        f"/admin/silme/section/{bolum.id}", params={"preview_token": eski}, headers=sistem
    )

    assert yanit.status_code == 409
    assert yanit.json()["code"] == "preview_stale"


# --- Mali kayıt kapısı (CEO eki: SIL-B2'ye kadar) ---


async def _mali_santiye(db_session, project_factory, olusturan, durum):
    proje = await project_factory("SIL-MALI")
    snt = await d.site(db_session, proje)
    await d.section(db_session, snt)
    await d.hakedis_satiri(db_session, proje, snt, olusturan, durum=durum)
    return snt


@pytest.mark.parametrize("durum", [ProgressPaymentStatus.approved, ProgressPaymentStatus.paid])
async def test_mali_bagli_santiye_409_financial_pending_ve_db_degismez(
    client, db_session, sistem, project_factory, olusturan, durum
) -> None:
    snt = await _mali_santiye(db_session, project_factory, olusturan, durum)
    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    gruplar = {g["table"]: g for g in onizleme["groups"]}
    assert gruplar["progress_payment_lines"]["is_financial"] is True  # önizleme TAM görünür
    assert gruplar["sections"]["is_financial"] is False
    once = await _tablo_sayimlari(db_session)

    for yol in (f"/admin/silme/site/{snt.id}", f"/sites/{snt.id}"):
        yanit = await client.delete(
            yol, params={"preview_token": onizleme["preview_token"]}, headers=sistem
        )
        assert yanit.status_code == 409
        assert yanit.json() == {"detail": FINANCIAL_PENDING, "code": "financial_pending"}
        await _hicbir_sey_degismedi(db_session, once)


async def test_oncelik_belirtec_yoksa_once_428_mali_olsa_bile(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt = await _mali_santiye(
        db_session, project_factory, olusturan, ProgressPaymentStatus.approved
    )

    yanit = await client.delete(f"/admin/silme/site/{snt.id}", headers=sistem)

    assert yanit.status_code == 428
    assert yanit.json()["code"] == "preview_required"


async def test_oncelik_belirtec_bayatsa_mali_olsa_bile_409_preview_stale(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    snt = await _mali_santiye(
        db_session, project_factory, olusturan, ProgressPaymentStatus.approved
    )
    eski = (await onizle(client, sistem, "site", snt.id)).json()["preview_token"]
    await d.section(db_session, snt, "Yeni")

    yanit = await client.delete(
        f"/admin/silme/site/{snt.id}", params={"preview_token": eski}, headers=sistem
    )

    assert yanit.status_code == 409
    assert yanit.json()["code"] == "preview_stale"


async def test_taslak_hakedis_mali_sayilmaz_dolu_santiye_silinir(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """Taslak hakedişin fişi/faturası/ödemesi yoktur: silmek hiçbir mali kaydı yetim bırakmaz."""
    snt = await _mali_santiye(db_session, project_factory, olusturan, ProgressPaymentStatus.draft)

    yanit = await sil_genel(client, sistem, "site", snt.id)

    assert yanit.status_code == 204


async def test_hakedis_baslik_bayat_kalir_SIL_B2(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """MEVCUT DAVRANIŞI BELGELER — SIL-B2 bu testi DEĞİŞTİRMEK ZORUNDA kalır.

    İşveren hakedişi proje düzeyindedir; satırı şantiyeye bağlıdır (`site_id` RESTRICT). Şantiye
    silinince şantiyeye bağlı SATIRLAR gider ama hakediş BAŞLIĞI kalır ve toplamı (satırlardan
    türer) sessizce küçülür/sıfırlanır: başlık bayat kalır. SIL-B2 (mali aile) bunu çözmeden
    önce bu test kasıtlı olarak kırmızıya döner.
    """
    proje = await project_factory("SIL-BAYAT")
    snt_a = await d.site(db_session, proje, "SNT-A", "A Şantiyesi")
    snt_b = await d.site(db_session, proje, "SNT-B", "B Şantiyesi")
    odeme = await d.hakedis_satiri(db_session, proje, snt_a, olusturan)
    ikinci_satir = ProgressPaymentLine(
        payment_id=odeme.id,
        contract_item_id=odeme.lines[0].contract_item_id,
        site_id=snt_b.id,
        code="11.099",
        description="B kalemi",
        unit="m³",
        contract_unit_price=odeme.lines[0].contract_unit_price,
        coefficient=odeme.lines[0].coefficient,
        quantity=odeme.lines[0].quantity,
    )
    db_session.add(ikinci_satir)
    await db_session.flush()
    odeme_id = odeme.id

    assert (await sil_genel(client, sistem, "site", snt_a.id)).status_code == 204

    db_session.expunge_all()
    kalan_baslik = await db_session.get(ProgressPayment, odeme_id)
    kalan_satirlar = (
        (
            await db_session.execute(
                select(ProgressPaymentLine).where(ProgressPaymentLine.payment_id == odeme_id)
            )
        )
        .scalars()
        .all()
    )
    assert kalan_baslik is not None  # başlık SİLİNMEDİ
    assert [s.code for s in kalan_satirlar] == ["11.099"]  # yalnız B satırı kaldı; A satırı gitti
    # Başlıkta toplam tutan bir kolon YOK, yeniden hesaplama da YOK → hiçbir yerde "A silindi,
    # toplam düştü" izi bırakılmadı. SIL-B2 başlığı yeniden hesaplamaya/durdurmaya karar verir.


async def test_onayli_tasaron_hakedisinin_fisi_onizlemede_gorunur_ve_silme_durur(
    client, db_session, sistem, project_factory, olusturan
) -> None:
    """FK dışı kanca: `journal_entries.source_id` taşeron hakedişini gösterir."""
    from datetime import date  # noqa: PLC0415

    from app.modules.accounting.models import (  # noqa: PLC0415
        JournalEntry,
        JournalEntryStatus,
        JournalSourceType,
    )

    proje = await project_factory("SIL-FIS")
    snt = await d.site(db_session, proje)
    odeme = await d.tasaron_hakedisi(
        db_session, proje, snt, olusturan, durum=SubcontractorPaymentStatus.approved
    )
    db_session.add(
        JournalEntry(
            entry_no="YEV-2026-9001",
            entry_date=date(2026, 3, 2),
            period_year=2026,
            period_month=3,
            description="Hakediş fişi",
            status=JournalEntryStatus.draft,
            source_type=JournalSourceType.subcontractor_progress_payment,
            source_id=odeme.id,
            created_by_id=olusturan.id,
        )
    )
    await db_session.flush()

    onizleme = (await onizle(client, sistem, "site", snt.id)).json()
    gruplar = {g["table"]: g for g in onizleme["groups"]}

    assert gruplar["journal_entries"]["relation"] == "linked"
    assert gruplar["journal_entries"]["is_financial"] is True
    assert gruplar["journal_entries"]["samples"] == ["YEV-2026-9001"]
    assert gruplar["subcontractor_progress_payments"]["is_financial"] is True  # onaylı
    yanit = await client.delete(
        f"/admin/silme/site/{snt.id}",
        params={"preview_token": onizleme["preview_token"]},
        headers=sistem,
    )
    assert yanit.json()["code"] == "financial_pending"


# --- FK dışı bağ (onay zinciri) ---


async def test_onay_zinciri_evrakla_birlikte_silinir_yetim_kalmaz(
    client, db_session, sistem, dolu_santiye
) -> None:
    assert await d.sayim(db_session, ApprovalChain) == 1

    assert (await sil_genel(client, sistem, "site", dolu_santiye["site"].id)).status_code == 204

    assert await d.sayim(db_session, ApprovalChain) == 0
    assert await d.sayim(db_session, ApprovalStep) == 0
    assert await d.sayim(db_session, SubcontractorContract) == 0


# --- Diğer aile üyeleri ---


async def test_dolu_bolum_silinir_bagli_kayit_olmadan_da(
    client, db_session, sistem, project_factory
) -> None:
    proje = await project_factory("SIL-BOLUM")
    snt = await d.site(db_session, proje)
    bolum = await d.section(db_session, snt)

    onizleme = (await onizle(client, sistem, "section", bolum.id)).json()
    yanit = await sil_genel(client, sistem, "section", bolum.id)

    assert onizleme["groups"] == [] and onizleme["dependent_count"] == 0
    assert onizleme["kind_label"] == "Bölüm"
    assert yanit.status_code == 204
    assert await d.sayim(db_session, Section, Section.id == bolum.id) == 0
    assert await d.sayim(db_session, Site, Site.id == snt.id) == 1  # üst kayıt DURUR


async def test_dolu_blok_uniteleriyle_silinir_restrict_cocuk_once(
    client, db_session, sistem, project_factory
) -> None:
    proje = await project_factory("SIL-BLOK")
    snt = await d.site(db_session, proje)
    blok = await d.block(db_session, proje, snt)
    await d.unit(db_session, proje, blok, "1")
    await d.unit(db_session, proje, blok, "2")
    diger = await d.block(db_session, proje, snt, "B Blok")
    await d.unit(db_session, proje, diger, "1")

    onizleme = (await onizle(client, sistem, "block", blok.id)).json()
    yanit = await sil_aile(client, sistem, "block", blok.id)

    assert [(g["table"], g["count"], g["relation"]) for g in onizleme["groups"]] == [
        ("units", 2, "restrict")
    ]
    assert yanit.status_code == 204
    assert await d.sayim(db_session, Block) == 1  # yalnız B Blok kaldı
    assert await d.sayim(db_session, Unit) == 1  # B Blok'un ünitesi dokunulmadı


async def test_unite_silinir(client, db_session, sistem, project_factory) -> None:
    proje = await project_factory("SIL-UNITE")
    snt = await d.site(db_session, proje)
    blok = await d.block(db_session, proje, snt)
    birim = await d.unit(db_session, proje, blok, "7")

    onizleme = (await onizle(client, sistem, "unit", birim.id)).json()
    yanit = await sil_genel(client, sistem, "unit", birim.id)

    assert onizleme["label"] == "7" and onizleme["kind_label"] == "Ünite"
    assert yanit.status_code == 204
    assert await d.sayim(db_session, Unit) == 0
    assert await d.sayim(db_session, Block) == 1


# --- Hatalar ---


@pytest.mark.parametrize(
    ("kind", "mesaj"),
    [
        ("site", "Şantiye bulunamadı"),
        ("section", "Bölüm bulunamadı"),
        ("block", "Blok bulunamadı"),
        ("unit", "Ünite bulunamadı"),
    ],
)
async def test_olmayan_kayit_404_onizleme_ve_silme(client, sistem, kind, mesaj) -> None:
    import uuid  # noqa: PLC0415

    kimlik = uuid.uuid4()
    onizleme = await onizle(client, sistem, kind, kimlik)
    silme = await client.delete(
        f"/admin/silme/{kind}/{kimlik}", params={"preview_token": "x"}, headers=sistem
    )

    for yanit in (onizleme, silme):
        assert yanit.status_code == 404
        assert yanit.json() == {"detail": mesaj}


async def test_bilinmeyen_tur_ve_gecersiz_kimlik_422(client, sistem) -> None:
    import uuid  # noqa: PLC0415

    assert (await onizle(client, sistem, "proje", uuid.uuid4())).status_code == 422
    assert (await onizle(client, sistem, "site", "uuid-degil")).status_code == 422


async def test_giris_yapmadan_401(client) -> None:
    import uuid  # noqa: PLC0415

    assert (await client.get(f"/admin/silme/site/{uuid.uuid4()}/onizleme")).status_code == 401
