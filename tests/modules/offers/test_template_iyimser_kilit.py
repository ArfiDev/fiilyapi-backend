"""TKL-B5.4 — sablon yazimlarinda iyimser kilit (`expected_updated_at`).

`PUT …/content` ve `PATCH …` gövdesinde ZORUNLU; satir `FOR UPDATE` kilitliyken `updated_at`
farkliysa 409. Yaris testi: emsal `test_template_default_race.py` (tutulan-kilit +
`pg_stat_activity` bariyeri + POZITIF KONTROL). "Varsayilan yap" ve sil kapsam disidir.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.modules.offers import template_service
from app.modules.offers.models import OfferTemplate
from app.modules.offers.template_schemas import (
    TemplateContentReplace,
    TemplateCreate,
    TemplateGroupInput,
)

from ._offers import TPL, sablon, sablon_kalemleri, sablon_ua
from .test_offers_race import _kullanici, _Ortam, _ortam, _yaris

STALE = "Şablon başka biri tarafından değiştirildi; sayfayı yenileyin"


def _icerik(ua: str, *katalog_idler, ad: str = "G") -> dict:
    return {
        "groups": [{"name": ad, "items": [{"catalog_item_id": str(k)} for k in katalog_idler]}],
        "expected_updated_at": ua,
    }


async def _detay(client, admin, sid: str) -> dict:
    resp = await client.get(f"{TPL}/{sid}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------------- PUT content


async def test_put_dogru_beklenen_200_ve_updated_at_ilerler(client, admin, katalog) -> None:
    s = await sablon(client, admin)
    resp = await client.put(
        f"{TPL}/{s['id']}/content", json=_icerik(s["updated_at"], katalog[0].id), headers=admin
    )
    assert resp.status_code == 200, resp.text
    yeni = resp.json()["updated_at"]
    assert datetime.fromisoformat(yeni) > datetime.fromisoformat(s["updated_at"])
    assert (await _detay(client, admin, s["id"]))["updated_at"] == yeni  # yanit = DB


async def test_put_eski_deger_409_icerik_DEGISMEZ(client, admin, katalog) -> None:
    s = await sablon(client, admin)
    ilk = await client.put(
        f"{TPL}/{s['id']}/content", json=_icerik(s["updated_at"], katalog[0].id), headers=admin
    )
    assert ilk.status_code == 200
    resp = await client.put(
        f"{TPL}/{s['id']}/content",
        json=_icerik(s["updated_at"], katalog[1].id, ad="Ezici"),
        headers=admin,
    )
    assert resp.status_code == 409, resp.text
    assert STALE in resp.text
    d = await _detay(client, admin, s["id"])
    assert [g["name"] for g in d["groups"]] == ["G"]
    assert sablon_kalemleri(d) == [[katalog[0].poz_no]]
    assert d["updated_at"] == ilk.json()["updated_at"]


async def test_put_alan_eksik_422(client, admin) -> None:
    s = await sablon(client, admin)
    resp = await client.put(f"{TPL}/{s['id']}/content", json={"groups": []}, headers=admin)
    assert resp.status_code == 422, resp.text


async def test_put_tzsiz_deger_422(client, admin) -> None:
    s = await sablon(client, admin)
    govde = {"groups": [], "expected_updated_at": "2026-01-01T00:00:00"}
    resp = await client.put(f"{TPL}/{s['id']}/content", json=govde, headers=admin)
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------------------ PATCH


async def test_patch_dogru_beklenen_200_ve_updated_at_ilerler(client, admin) -> None:
    s = await sablon(client, admin, "A")
    resp = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "B", "expected_updated_at": s["updated_at"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert datetime.fromisoformat(resp.json()["updated_at"]) > datetime.fromisoformat(
        s["updated_at"]
    )


async def test_patch_eski_deger_409_kunye_DEGISMEZ(client, admin) -> None:
    s = await sablon(client, admin, "A")
    ilk = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "B", "expected_updated_at": s["updated_at"]},
        headers=admin,
    )
    assert ilk.status_code == 200
    resp = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "Ezici", "expected_updated_at": s["updated_at"]},
        headers=admin,
    )
    assert resp.status_code == 409, resp.text
    assert STALE in resp.text
    assert (await _detay(client, admin, s["id"]))["name"] == "B"


async def test_patch_alan_eksik_422(client, admin) -> None:
    s = await sablon(client, admin)
    resp = await client.patch(f"{TPL}/{s['id']}", json={"name": "X"}, headers=admin)
    assert resp.status_code == 422, resp.text


# ------------------------------------------------------- bayat sekme + JSON gidis-donus


async def test_bayat_sekme_A_yazar_B_409_A_icerigi_durur(client, admin, katalog) -> None:
    s = await sablon(client, admin)
    a_ua = await sablon_ua(client, admin, s["id"])
    b_ua = await sablon_ua(client, admin, s["id"])
    assert a_ua == b_ua
    a = await client.put(
        f"{TPL}/{s['id']}/content", json=_icerik(a_ua, katalog[0].id, ad="A"), headers=admin
    )
    assert a.status_code == 200
    b = await client.put(
        f"{TPL}/{s['id']}/content", json=_icerik(b_ua, katalog[2].id, ad="B"), headers=admin
    )
    assert b.status_code == 409, b.text
    d = await _detay(client, admin, s["id"])
    assert [g["name"] for g in d["groups"]] == ["A"]
    assert sablon_kalemleri(d) == [[katalog[0].poz_no]]


async def test_json_gidis_donus_GET_metni_aynen_geri_yollanir_sahte_409_yok(
    client, admin, katalog
) -> None:
    s = await sablon(client, admin)
    ua = s["updated_at"]
    for tur in range(3):  # her tur yeni updated_at; mikro saniye kesilmesi sahte 409 verirdi
        resp = await client.put(
            f"{TPL}/{s['id']}/content", json=_icerik(ua, katalog[tur % 3].id), headers=admin
        )
        assert resp.status_code == 200, (tur, resp.text)
        ua = await sablon_ua(client, admin, s["id"])
        assert ua == resp.json()["updated_at"]
        patch = await client.patch(
            f"{TPL}/{s['id']}", json={"name": f"N{tur}", "expected_updated_at": ua}, headers=admin
        )
        assert patch.status_code == 200, (tur, patch.text)
        ua = patch.json()["updated_at"]


async def test_degisiklik_yoksa_patch_updated_at_ilerlemez_ama_kontrol_yine_yapilir(
    client, admin
) -> None:
    s = await sablon(client, admin, "A")
    ayni = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "A", "expected_updated_at": s["updated_at"]},
        headers=admin,
    )
    assert ayni.status_code == 200 and ayni.json()["updated_at"] == s["updated_at"]
    bayat = await client.patch(
        f"{TPL}/{s['id']}",
        json={"name": "A", "expected_updated_at": "2020-01-01T00:00:00Z"},
        headers=admin,
    )
    assert bayat.status_code == 409


# -------------------------------------------------------------------------------- YARIS


async def _sablon_ve_ua(ortam: _Ortam) -> tuple[uuid.UUID, datetime]:
    async with ortam.Session() as s:
        user = await _kullanici(s, ortam)
        t = await template_service.create_template(s, user, TemplateCreate(name="Y"))
        await s.commit()
        return t.id, t.updated_at


async def _grup_adlari(ortam: _Ortam, sid: uuid.UUID) -> list[str]:
    from app.modules.offers.models import OfferTemplateGroup

    async with ortam.Session() as s:
        rows = await s.scalars(
            select(OfferTemplateGroup.name).where(OfferTemplateGroup.template_id == sid)
        )
        return sorted(rows)


async def _yaris_iki_put(ortam: _Ortam):
    sid, ua = await _sablon_ve_ua(ortam)

    def _govde(ad: str) -> TemplateContentReplace:
        return TemplateContentReplace(groups=[TemplateGroupInput(name=ad)], expected_updated_at=ua)

    async def _put(session: AsyncSession, ad: str) -> None:
        await template_service.replace_content(
            session, await _kullanici(session, ortam), sid, _govde(ad)
        )

    async def _a(session: AsyncSession) -> None:
        await _put(session, "A")

    async def _b(session: AsyncSession) -> None:
        await _put(session, "B")

    bekleyen, hata = await _yaris(ortam, _a, _b)
    return sid, bekleyen, hata


async def test_eszamanli_iki_put_ayni_beklenenle_biri_200_oteki_409() -> None:
    async with _ortam(gonderilmis=False) as ortam:
        sid, bekleyen, hata = await _yaris_iki_put(ortam)

        assert "FOR UPDATE" in bekleyen, bekleyen  # ikincisi satir kilidinde BEKLEDI
        assert isinstance(hata, ConflictError), f"ikinci istek 409 olmaliydi: {hata!r}"
        assert STALE in str(hata)
        assert await _grup_adlari(ortam, sid) == ["A"]  # A'nin icerigi duruyor


async def test_KONTROL_kilitsiz_iki_put_ikisi_de_gecer_sessiz_ezme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kilit (FOR UPDATE) kaldirilinca ikinci istek ayni `updated_at`i gorur → karsilastirma
    gecer; yalniz UPDATE'te bekler ve A commit olunca ONUN USTUNE yazar (sessiz ezme)."""

    async def _kilitsiz(session: AsyncSession, template_id: uuid.UUID) -> OfferTemplate:
        template = await session.scalar(
            select(OfferTemplate)
            .where(OfferTemplate.id == template_id)
            .execution_options(populate_existing=True)
        )
        assert template is not None
        return template

    monkeypatch.setattr(template_service, "lock_template", _kilitsiz)
    async with _ortam(gonderilmis=False) as ortam:
        sid, bekleyen, hata = await _yaris_iki_put(ortam)

        assert bekleyen.startswith("UPDATE offer_templates"), bekleyen  # FOR UPDATE'te DEGIL
        assert hata is None, f"kilitsiz de ikinci istek 409 aldi: {hata!r}"
        assert await _grup_adlari(ortam, sid) == ["A", "B"]  # sessiz ezme: ikisi de yazildi
