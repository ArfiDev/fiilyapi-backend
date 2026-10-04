"""`DELETE /sites/{id}` ve `DELETE /sections/{id}` — SIL-B1 sonrası davranış (spec §7.1 değişti).

## Ne DEĞİŞTİ (karar: KARARLAR §1.7 K4 + SIL-B1)

Eskiden şantiye silme dokuz "bağlı kayıt var" korkuluğuyla (bölüm, poz, blok, sözleşme, hakediş
satırı, puantaj, günlük, belge, plan) 409 dönerdi ve bölüm silme günlük miktar satırı varken 409
dönerdi. Silme artık YALNIZ Sistem Yöneticisi'nindir ve bağlı kayıtlar KENDİSİYLE BİRLİKTE
silinir (önce liste + onay: `preview_token`). Bu dosyadaki eski "409 korkuluk" testleri bu yüzden
kaldırıldı; yerlerine "dolu şantiye silinir, bağlılar gider" testleri geldi. Motorun ayrıntılı
kanıtı `tests/modules/silme/`, `tests/core/test_silme_*.py` altındadır.
"""

import uuid

from sqlalchemy import select

from app.modules.audit.models import AuditAction, AuditLog
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.sites.models import Section, Site
from app.modules.units.models import Block, Unit
from tests._silme_yardimci import sil_aile, sisyon_girisi
from tests.modules.silme import _dunya as d

SITE_MISSING = "Şantiye bulunamadı"
SECTION_MISSING = "Bölüm bulunamadı"


async def test_bos_santiye_silinir_204_govdesiz_sonra_404(
    client, db_session, user_factory, project_factory
):
    proje = await project_factory("D-1")
    snt = await d.site(db_session, proje)
    baslik = await sisyon_girisi(client, user_factory)

    yanit = await sil_aile(client, baslik, "site", snt.id)

    assert yanit.status_code == 204
    assert yanit.content == b""
    sonra = await client.get(f"/sites/{snt.id}", headers=baslik)
    assert sonra.status_code == 404
    assert sonra.json()["detail"] == SITE_MISSING


async def test_dolu_santiye_bolum_poz_blok_unite_birlikte_silinir(
    client, db_session, user_factory, project_factory
):
    """Eski 409 korkulukları (bölüm/poz/blok) Sistem Yöneticisi için BYPASS edilir."""
    proje = await project_factory("D-2")
    snt = await d.site(db_session, proje)
    await d.section(db_session, snt)
    await d.boq(db_session, snt)
    blok = await d.block(db_session, proje, snt)
    await d.unit(db_session, proje, blok)
    baslik = await sisyon_girisi(client, user_factory)

    yanit = await sil_aile(client, baslik, "site", snt.id)

    assert yanit.status_code == 204
    for model in (Site, Section, BoqGroup, BoqItem, Block, Unit):
        assert await d.sayim(db_session, model) == 0, model.__name__


async def test_baska_santiyenin_kayitlari_dokunulmaz(
    client, db_session, user_factory, project_factory
):
    proje = await project_factory("D-3")
    hedef = await d.site(db_session, proje, "A", "Hedef")
    kalan = await d.site(db_session, proje, "B", "Kalan")
    await d.section(db_session, hedef)
    await d.section(db_session, kalan)
    baslik = await sisyon_girisi(client, user_factory)

    await sil_aile(client, baslik, "site", hedef.id)

    assert [s.id for s in (await db_session.execute(select(Site))).scalars()] == [kalan.id]
    assert await d.sayim(db_session, Section, Section.site_id == kalan.id) == 1


async def test_olmayan_santiye_404_ayni_govde(client, user_factory):
    baslik = await sisyon_girisi(client, user_factory)
    kimlik = uuid.uuid4()

    yanit = await client.delete(f"/sites/{kimlik}", params={"preview_token": "x"}, headers=baslik)

    assert yanit.status_code == 404
    assert yanit.json() == {"detail": SITE_MISSING}


async def test_bolum_silinir_204_ikinci_silme_404_ust_santiye_durur(
    client, db_session, user_factory, project_factory
):
    proje = await project_factory("D-4")
    snt = await d.site(db_session, proje)
    bolum = await d.section(db_session, snt)
    diger = await d.section(db_session, snt, "İnce İşler")
    baslik = await sisyon_girisi(client, user_factory)

    ilk = await sil_aile(client, baslik, "section", bolum.id)
    ikinci = await client.delete(
        f"/sections/{bolum.id}", params={"preview_token": "x"}, headers=baslik
    )

    assert ilk.status_code == 204 and ilk.content == b""
    assert ikinci.status_code == 404
    assert ikinci.json() == {"detail": SECTION_MISSING}
    assert await d.sayim(db_session, Site, Site.id == snt.id) == 1
    assert await d.sayim(db_session, Section, Section.id == diger.id) == 1


async def test_bolum_silme_kalan_bolumlerin_sirasini_degistirmez(
    client, db_session, user_factory, project_factory
):
    proje = await project_factory("D-5")
    snt = await d.site(db_session, proje)
    ilk = await d.section(db_session, snt, "Bir")
    orta = await d.section(db_session, snt, "İki")
    son = await d.section(db_session, snt, "Üç")
    once = {ilk.id: ilk.sort_order, son.id: son.sort_order}
    baslik = await sisyon_girisi(client, user_factory)

    await sil_aile(client, baslik, "section", orta.id)

    db_session.expire_all()
    kalan = {
        s.id: s.sort_order for s in (await db_session.execute(select(Section))).scalars().all()
    }
    assert kalan == once  # yeniden numaralanmaz


async def test_denetim_satiri_kimin_neyi_sildigini_yazar(
    client, db_session, user_factory, project_factory
):
    proje = await project_factory("D-6")
    snt = await d.site(db_session, proje, "A", "Kule Şantiyesi")
    await d.section(db_session, snt)
    baslik = await sisyon_girisi(client, user_factory)

    await sil_aile(client, baslik, "site", snt.id)

    satir = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == AuditAction.delete))
    ).scalar_one()
    assert satir.detail.startswith(f"Şantiye silindi: {proje.name} · Kule Şantiyesi")
    assert "1 bağlı kayıtla birlikte silindi (Bölüm 1)" in satir.detail
