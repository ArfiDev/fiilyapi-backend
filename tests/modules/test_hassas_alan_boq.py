"""IZN-B4 — hassas alan maskesi, BOQ (ilk tam etiketlenen modül), GERÇEK HTTP.

Kapsananlar:
* kategori başına maskeli yanıt (`sozlesme_fiyat`; başka kategori → BOQ AÇIK; `tum_tutarlar`),
* türev `None` (satır tutarı, grup toplamı, genel toplam; zarflı toplamlar üçüncü hâle düşer),
* PROJE BAŞINA rol farkı (A'da fiyat açık, B'de kapalı) ve "Tüm projeler" → ana rol,
* yazma yolu: gizli kategorili alanı gövdede dolu gönderen 403, ilgisiz alan geçer,
* export: maskeli hücre BOŞ.
"""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.sayfalar import HiddenCategory
from app.modules.boq.models import BoqItem
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle, tum_projeler
from tests.modules._boq import _auth, _group, _item, _site

PASSWORD = "parola1234"
H = HiddenCategory


async def _giris(client: AsyncClient, user_factory, email: str, rol_key: str, session, **kw):
    user = await user_factory(email=email, password=PASSWORD, role_key=rol_key)
    if kw.get("tum_projeler"):
        await tum_projeler(session, user)
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return user, _auth(resp.json()["access_token"])


async def _proje(session, project_factory, kod: str):
    project = await project_factory(kod)
    site = await _site(session, project, code=f"{kod}-S")
    group = await _group(session, site)
    item = await _item(
        session, site, group, quantity=Decimal("1240.000"), unit_price=Decimal("280.00")
    )
    return project, site, item


async def _boq(client: AsyncClient, site, basliklar) -> dict:
    resp = await client.get(f"/sites/{site.id}/boq", headers=basliklar)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _kalem(govde: dict) -> dict:
    return govde["groups"][0]["items"][0]


_TUTAR = "347200.00"


async def test_sozlesme_fiyat_gizli_BIRIM_FIYAT_ve_TUM_turevler_duser(
    client, db_session, user_factory, project_factory
) -> None:
    await rol_gizli(db_session, "gz_fiyat", {H.sozlesme_fiyat})
    _p, site, _i = await _proje(db_session, project_factory, "HZ-1")
    _u, baslik = await _giris(
        client, user_factory, "gz@hz.co", "gz_fiyat", db_session, tum_projeler=True
    )

    govde = await _boq(client, site, baslik)
    kalem = _kalem(govde)

    assert kalem["unit_price"] is None
    assert kalem["amount"] is None, "TÜREV satır tutarı sızdı (birim fiyat geri hesaplanır)"
    assert govde["groups"][0]["group_total"] is None, "TÜREV grup toplamı sızdı"
    assert govde["totals"]["grand_total"] is None
    # zarflı toplamlar `null` değil ÜÇÜNCÜ hâl: "rolün izni yok" (ekran "veri yok"tan ayırır)
    for alan in ("contract_total", "realized_total", "remaining_total", "revision_total"):
        assert govde["totals"][alan] == {
            "available": False,
            "value": None,
            "pending_module": None,
        }, alan
    # `yok` etiketli alanlar (metraj, kimlik, ilerleme) DURUR
    assert kalem["quantity"] == "1240.000"
    assert kalem["code"] == "01.001"
    assert govde["totals"]["grand_progress_pct"]["available"] is True


async def test_BASKA_kategori_gizliyken_BOQ_fiyati_ACIK(
    client, db_session, user_factory, project_factory
) -> None:
    """🔴 POZİTİF KONTROL: maske "herkesten her şeyi gizle" hâline çökerse burası kırmızı olur."""
    await rol_gizli(
        db_session, "gz_baska", {H.maliyet_kar, H.banka_kasa, H.satis_alici, H.maas_kisisel}
    )
    _p, site, _i = await _proje(db_session, project_factory, "HZ-2")
    _u, baslik = await _giris(
        client, user_factory, "gb@hz.co", "gz_baska", db_session, tum_projeler=True
    )

    govde = await _boq(client, site, baslik)

    assert _kalem(govde)["unit_price"] == "280.00"
    assert _kalem(govde)["amount"] == _TUTAR
    assert govde["totals"]["grand_total"] == _TUTAR


async def test_tum_tutarlar_BOQ_fiyatlarini_da_gizler(
    client, db_session, user_factory, project_factory
) -> None:
    await rol_gizli(db_session, "gz_tum", {H.tum_tutarlar})
    _p, site, _i = await _proje(db_session, project_factory, "HZ-3")
    _u, baslik = await _giris(
        client, user_factory, "gt@hz.co", "gz_tum", db_session, tum_projeler=True
    )

    govde = await _boq(client, site, baslik)

    assert _kalem(govde)["unit_price"] is None
    assert _kalem(govde)["amount"] is None
    assert govde["totals"]["grand_total"] is None
    assert _kalem(govde)["quantity"] == "1240.000"


async def test_sistem_yoneticisi_hicbir_seyi_gizli_gormez(
    client, db_session, user_factory, project_factory
) -> None:
    _p, site, _i = await _proje(db_session, project_factory, "HZ-4")
    _u, baslik = await _giris(client, user_factory, "adm@hz.co", "system_admin", db_session)

    assert _kalem(await _boq(client, site, baslik))["unit_price"] == "280.00"


async def test_PROJE_BASINA_rol_A_da_fiyat_acik_B_de_kapali(
    client, db_session, user_factory, project_factory
) -> None:
    """🔴 Mutasyon (b) bekçisi: etkin rol ANA rol olsaydı iki projede de AYNI sonuç çıkardı."""
    acik = await rol_gizli(db_session, "gz_acik", set())
    kapali = await rol_gizli(db_session, "gz_kapali", {H.tum_tutarlar})
    _a, site_a, _ia = await _proje(db_session, project_factory, "HZ-A")
    _b, site_b, _ib = await _proje(db_session, project_factory, "HZ-B")
    kisi, baslik = await _giris(client, user_factory, "karma@hz.co", "gz_kapali", db_session)
    await ekibe_ekle(db_session, kisi, _a.id, acik.id)  # A: PM benzeri (gizli yok)
    await ekibe_ekle(db_session, kisi, _b.id, kapali.id)  # B: Görüntüleyici benzeri

    assert _kalem(await _boq(client, site_a, baslik))["unit_price"] == "280.00"
    assert _kalem(await _boq(client, site_b, baslik))["unit_price"] is None


async def test_PROJE_rolu_ana_rolden_KISITLAYICI_ise_projede_kisitlayici_olan_gecerli(
    client, db_session, user_factory, project_factory
) -> None:
    """Ana rol açık, B'deki proje rolü kapalı: B'de fiyat gizli (ana rolün yetkisi sızmaz)."""
    acik = await rol_gizli(db_session, "gz_acik2", set())
    kapali = await rol_gizli(db_session, "gz_kapali2", {H.sozlesme_fiyat})
    _a, site_a, _ia = await _proje(db_session, project_factory, "HZ-C")
    _b, site_b, _ib = await _proje(db_session, project_factory, "HZ-D")
    kisi, baslik = await _giris(client, user_factory, "ters@hz.co", "gz_acik2", db_session)
    await ekibe_ekle(db_session, kisi, _a.id)  # A: ana rolle
    await ekibe_ekle(db_session, kisi, _b.id, kapali.id)
    assert acik.id == kisi.role_id

    assert _kalem(await _boq(client, site_a, baslik))["unit_price"] == "280.00"
    assert _kalem(await _boq(client, site_b, baslik))["unit_price"] is None


async def test_TUM_PROJELER_kisisi_projede_de_ANA_rolle_maskelenir(
    client, db_session, user_factory, project_factory
) -> None:
    """Ekip satırı (bayat) ne derse desin "Tüm projeler" kişi ana rolle çalışır."""
    acik = await rol_gizli(db_session, "gz_acik3", set())
    await rol_gizli(db_session, "gz_kapali3", {H.tum_tutarlar})
    proje, site, _i = await _proje(db_session, project_factory, "HZ-E")
    kisi, baslik = await _giris(
        client, user_factory, "tum@hz.co", "gz_kapali3", db_session, tum_projeler=True
    )
    await ekibe_ekle(db_session, kisi, proje.id, acik.id)  # yok sayılmalı

    assert _kalem(await _boq(client, site, baslik))["unit_price"] is None


async def test_YAZMA_gizli_kategorili_alani_gonderen_403_ilgisiz_alan_gecer(
    client, db_session, user_factory, project_factory
) -> None:
    await rol_gizli(db_session, "gz_yaz", {H.sozlesme_fiyat})
    _p, site, item = await _proje(db_session, project_factory, "HZ-F")
    kalem_id = item.id
    _u, baslik = await _giris(
        client, user_factory, "yaz@hz.co", "gz_yaz", db_session, tum_projeler=True
    )

    red = await client.patch(f"/boq/items/{kalem_id}", json={"unit_price": "1.00"}, headers=baslik)
    assert red.status_code == 403, red.text
    assert "unit_price" in red.json()["detail"]
    db_session.expire_all()
    fiyat = (
        await db_session.execute(select(BoqItem.unit_price).where(BoqItem.id == kalem_id))
    ).scalar_one()
    assert fiyat == Decimal("280.00")

    ok = await client.patch(f"/boq/items/{kalem_id}", json={"description": "Yeni"}, headers=baslik)
    assert ok.status_code == 200, ok.text
    assert ok.json()["description"] == "Yeni"
    assert ok.json()["unit_price"] is None, "yazma yanıtı da maskelenir"
    # açık `null` da bir yazma girişimidir (alanı silmek)
    sil = await client.patch(f"/boq/items/{kalem_id}", json={"unit_price": None}, headers=baslik)
    assert sil.status_code == 403, sil.text


async def test_YAZMA_kategori_gizli_degilse_fiyat_yazilir(
    client, db_session, user_factory, project_factory
) -> None:
    """🔴 POZİTİF KONTROL: kapı "her yazmayı reddet" hâline çökerse burası kırmızı olur."""
    await rol_gizli(db_session, "gz_yaz_acik", {H.maas_kisisel})
    _p, _site, item = await _proje(db_session, project_factory, "HZ-G")
    _u, baslik = await _giris(
        client, user_factory, "yaz2@hz.co", "gz_yaz_acik", db_session, tum_projeler=True
    )

    ok = await client.patch(f"/boq/items/{item.id}", json={"unit_price": "300.00"}, headers=baslik)

    assert ok.status_code == 200, ok.text
    assert ok.json()["unit_price"] == "300.00"


async def test_YAZMA_olusturma_ucunda_zorunlu_gizli_alan_403(
    client, db_session, user_factory, project_factory
) -> None:
    await rol_gizli(db_session, "gz_yaz3", {H.tum_tutarlar})
    _p, site, item = await _proje(db_session, project_factory, "HZ-H")
    _u, baslik = await _giris(
        client, user_factory, "yaz3@hz.co", "gz_yaz3", db_session, tum_projeler=True
    )
    govde = {
        "group_id": str(item.group_id),
        "code": "9.9",
        "description": "d",
        "unit": "m",
        "quantity": "1",
        "unit_price": "5",
    }

    resp = await client.post(f"/sites/{site.id}/boq/items", json=govde, headers=baslik)

    assert resp.status_code == 403, resp.text
    adet = (await db_session.execute(select(BoqItem).where(BoqItem.code == "9.9"))).scalars().all()
    assert adet == []


@pytest.mark.parametrize(
    ("gizli", "fiyat_bos"), [({H.sozlesme_fiyat}, True), ({H.maliyet_kar}, False)]
)
async def test_EXPORT_maskeli_hucre_BOS_yazilir_diger_kategoride_dolu(
    client, db_session, user_factory, project_factory, gizli, fiyat_bos
) -> None:
    await rol_gizli(db_session, f"gz_exp_{int(fiyat_bos)}", gizli)
    _p, site, _i = await _proje(db_session, project_factory, f"HZ-X{int(fiyat_bos)}")
    _u, baslik = await _giris(
        client,
        user_factory,
        f"exp{int(fiyat_bos)}@hz.co",
        f"gz_exp_{int(fiyat_bos)}",
        db_session,
        tum_projeler=True,
    )

    resp = await client.get(f"/sites/{site.id}/boq/export", headers=baslik)

    assert resp.status_code == 200, resp.text
    sayfa = openpyxl.load_workbook(BytesIO(resp.content)).active
    metinler = [str(h.value) for satir in sayfa.iter_rows() for h in satir if h.value is not None]
    assert ("280.00" not in metinler) is fiyat_bos
    assert (_TUTAR not in metinler) is fiyat_bos
    assert "None" not in metinler
    assert "1240.000" in metinler  # metraj her durumda dolu
