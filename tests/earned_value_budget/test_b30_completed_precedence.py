"""PLN-B3.0 — tamamlanmış şantiyede 409 ÖNCELİĞİ: sıra 403 → 404 → 409 → 422.

PLN-B2 "AÇIK KALAN 2": tamamlanmış şantiyeye GEÇERSİZ gövde gidince 409 yerine 422/404
dönüyordu, çünkü FastAPI gövdeyi uç çağrılmadan doğrular ve durum kontrolü uç/serviste
koşuyordu. Kural artık yazma uçlarının bir BAĞIMLILIĞIDIR (`access.completed_site_guard`).

📏 Ölçüldü (pinli fastapi 0.141.1): rota `dependencies=[...]` → uç imzasındaki `Depends`
(sırayla) → path/query → GÖVDE doğrulaması. Bağımlılık hata fırlatırsa gövde hiç
doğrulanmaz. ⚠️ İstisna: BOZUK JSON (ayrıştırılamayan metin) bağımlılıklardan ÖNCE 422 olur
— şema-geçersiz gövdede değil, sözdizimi bozuk gövdede. Bu dosya şema-geçersizi çiviler.

Her yazma ucu üç hâlde koşar:
* aktif şantiye + geçersiz istek → 422/404 (KONTROL: istek gerçekten geçersiz; 409'u
  "istek zaten bozuktu" diye yanlış sebepten alan bir uç burada yakalanır);
* tamamlanmış şantiye + AYNI geçersiz istek → 409 + O EKRANIN metni;
* GÖRÜNMEYEN tamamlanmış şantiye + aynı istek → 404 (görünürlük 409'dan önce; 409 bir
  şantiyenin VARLIĞINI ve durumunu sızdırırdı).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient, Response

from app.modules.earned_value import guards
from app.modules.earned_value.models import EvReportApproval
from app.modules.sites.models import Site, SiteStatus

from .conftest import DAY

pytestmark = pytest.mark.asyncio

_Istek = Callable[[AsyncClient, dict, Site, dict], Awaitable[Response]]


def _budget(site: Site, tail: str = "") -> str:
    return f"/sites/{site.id}/earned-value/budget{tail}"


def _day(site: Site, tail: str = "") -> str:
    return f"/sites/{site.id}/earned-value/days/{DAY.isoformat()}{tail}"


# (ad, istek, aktif şantiyede beklenen, tamamlanmışta beklenen metin)
_YAZMALAR: list[tuple[str, _Istek, int, str]] = [
    (
        "settings-put",
        lambda c, h, s, _: c.put(f"/sites/{s.id}/earned-value/settings", headers=h, json={}),
        422,
        guards.SITE_COMPLETED_READ_ONLY,
    ),
    (
        "group-disciplines",
        lambda c, h, s, _: c.put(_budget(s, "/group-disciplines"), headers=h, json={"items": 1}),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "items-patch",
        lambda c, h, s, b: c.patch(
            _budget(s, f"/items/{b['i1'].id}"), headers=h, json={"contractor_type": "yok"}
        ),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "items-patch-yabanci-kalem",
        lambda c, h, s, _: c.patch(
            _budget(s, f"/items/{uuid.uuid4()}"), headers=h, json={"contractor_type": "subcon"}
        ),
        404,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "leaves-patch",
        lambda c, h, s, _: c.patch(_budget(s, "/leaves"), headers=h, json={"leaves": "x"}),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "distributions",
        lambda c, h, s, _: c.put(
            _budget(s, "/distributions"), headers=h, json={"items": [{"discipline_id": "yok"}]}
        ),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "windows",
        lambda c, h, s, _: c.put(
            _budget(s, "/windows"), headers=h, json={"windows": [{"start_date": "x"}]}
        ),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "taslak-sil-olmayan",
        lambda c, h, s, _: c.delete(_budget(s, f"/revisions/{uuid.uuid4()}"), headers=h),
        404,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "freeze",
        lambda c, h, s, _: c.post(_budget(s, "/freeze"), headers=h, json={"name": "x" * 151}),
        422,
        guards.SITE_COMPLETED_BUDGET_READ_ONLY,
    ),
    (
        "day-allocation",
        lambda c, h, s, _: c.put(_day(s, "/allocation"), headers=h, json={}),
        422,
        guards.SITE_COMPLETED_DAY_READ_ONLY,
    ),
    (
        "day-unlock",
        lambda c, h, s, _: c.post(_day(s, "/unlock"), headers=h, json={"reason": ""}),
        422,
        guards.SITE_COMPLETED_DAY_READ_ONLY,
    ),
]

#: IZN-B2: bütçe taslağı SİL (DELETE) `(earned_value, approve)` modül kapısında kalır; Onaylar
#: bayrağı modül kapısı açmadığı için yalnız Sistem Yöneticisi geçer (SIL hattı: SIL-B1 testi
#: sysadmin aktörüne çevirecek).
_SIL_HATTINDA = pytest.mark.xfail(
    strict=True,
    reason="IZN-B2: DELETE bütçe taslağı yalnız Sistem Yöneticisi (SIL hattında)",
)
_HER_YAZMA = pytest.mark.parametrize(
    ("ad", "istek", "aktifte", "metin"),
    [
        pytest.param(*y, id=y[0], marks=_SIL_HATTINDA)
        if y[0] == "taslak-sil-olmayan"
        else pytest.param(*y, id=y[0])
        for y in _YAZMALAR
    ],
)

#: Gövdesiz yazmalar: "geçersiz gövde" hâli yoktur ama sıra (404 → 409) yine geçerlidir.
_GOVDESIZ: list[tuple[str, _Istek]] = [
    ("taslak-ac", lambda c, h, s, _: c.post(_budget(s, "/revisions"), headers=h)),
    ("fill-from-catalog", lambda c, h, s, _: c.post(_budget(s, "/fill-from-catalog"), headers=h)),
    # EV-BORC-5: 13. site yazması — rapor onayı (APPROVE; aynı ekran metni)
    (
        "rapor-onayi",
        lambda c, h, s, _: c.post(
            f"/sites/{s.id}/earned-value/reports/daily/{DAY.isoformat()}/approve", headers=h
        ),
    ),
]


async def _tamamla(seeded_db, *sites: Site) -> None:  # noqa: ANN001
    for site in sites:
        site.status = SiteStatus.completed
    await seeded_db.flush()


@pytest.fixture
async def zemin(santiye, gorunmeyen_santiye, boq, disiplinler) -> dict:
    """`sef` (earned_value APPROVE, yalnız `proje`ye atanmış) her yazma ucunu çağırabilir."""
    return boq


@_HER_YAZMA
async def test_B30_invalid_request_on_active_site_is_rejected(
    client, sef, santiye, zemin, ad, istek, aktifte, metin
) -> None:
    """KONTROL: istek gerçekten geçersiz — 409'un tek sebebi durum olmalı."""
    resp = await istek(client, sef, santiye, zemin)
    assert resp.status_code == aktifte, f"{ad}: {resp.status_code} {resp.text}"


@_HER_YAZMA
async def test_B30_invalid_request_on_completed_site_is_409(
    client, sef, seeded_db, santiye, zemin, ad, istek, aktifte, metin
) -> None:
    await _tamamla(seeded_db, santiye)
    resp = await istek(client, sef, santiye, zemin)
    assert resp.status_code == 409, f"{ad}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == metin, f"{ad}: yanlış ekran metni {resp.json()}"


@_HER_YAZMA
async def test_B30_invisible_completed_site_stays_404(
    client, sef, seeded_db, gorunmeyen_santiye, zemin, ad, istek, aktifte, metin
) -> None:
    await _tamamla(seeded_db, gorunmeyen_santiye)
    resp = await istek(client, sef, gorunmeyen_santiye, zemin)
    assert resp.status_code == 404, f"{ad}: {resp.status_code} {resp.text}"


@pytest.mark.parametrize(("ad", "istek"), _GOVDESIZ, ids=[g[0] for g in _GOVDESIZ])
async def test_B30_bodyless_writes_404_before_409(
    client, sef, seeded_db, santiye, gorunmeyen_santiye, zemin, ad, istek
) -> None:
    await _tamamla(seeded_db, santiye, gorunmeyen_santiye)
    gorunmez = await istek(client, sef, gorunmeyen_santiye, zemin)
    assert gorunmez.status_code == 404, f"{ad}: {gorunmez.status_code} {gorunmez.text}"
    resp = await istek(client, sef, santiye, zemin)
    assert resp.status_code == 409, f"{ad}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == guards.SITE_COMPLETED_BUDGET_READ_ONLY


async def test_B30_permission_403_precedes_409(client, muhasebe, seeded_db, santiye) -> None:
    """VIEW rolü tamamlanmış şantiyede de 403 alır (yetki kapısı en önde)."""
    await _tamamla(seeded_db, santiye)
    resp = await client.put(f"/sites/{santiye.id}/earned-value/settings", headers=muhasebe, json={})
    assert resp.status_code == 403, resp.text


# ------------------------------------------------------------------ gün uçları, GEÇERLİ gövde


def _allocation(boq, gun) -> dict:  # noqa: ANN001
    leaf = f"l:{boq['i1'].id}:none"
    return {
        "codes": [{"node_id": leaf, "rule": "direct"}],
        "cells": [
            {
                "row": {"kind": "personnel", "ref_id": str(gun["ali"].id)},
                "node_id": leaf,
                "hours": "9",
            }
        ],
    }


async def test_B30_day_allocation_valid_body_on_completed_site_is_409(
    client, saha, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    body = _allocation(boq, saha_gunu)
    assert (
        await client.put(_day(santiye, "/allocation"), headers=saha, json=body)
    ).status_code == 200
    await _tamamla(seeded_db, santiye)
    resp = await client.put(_day(santiye, "/allocation"), headers=saha, json=body)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.SITE_COMPLETED_DAY_READ_ONLY
    assert (await client.get(_day(santiye), headers=saha)).status_code == 200  # okuma serbest


async def test_B30_day_unlock_on_completed_site_is_409(
    client, sef, seeded_db, santiye, boq, baseline, saha_gunu
) -> None:
    """Kilitli gün, geçerli gerekçe: aktifte 200 olurdu; tamamlanmışta 409 ve istisna YAZILMAZ."""
    seeded_db.add(
        EvReportApproval(
            site_id=santiye.id,
            report_date=DAY + timedelta(days=2),
            approved_at=datetime(2026, 5, 8, tzinfo=UTC),
        )
    )
    await _tamamla(seeded_db, santiye)
    resp = await client.post(_day(santiye, "/unlock"), headers=sef, json={"reason": "Düzeltme"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.SITE_COMPLETED_DAY_READ_ONLY
    view = (await client.get(_day(santiye), headers=sef)).json()
    assert view["lock"]["locked"] is True, "409'a rağmen gün açıldı"


async def test_B30_reads_stay_open_on_completed_site(
    client, sef, seeded_db, santiye, boq, baseline
) -> None:
    await _tamamla(seeded_db, santiye)
    okumalar = {
        "settings": await client.get(f"/sites/{santiye.id}/earned-value/settings", headers=sef),
        "code-tree": await client.get(f"/sites/{santiye.id}/earned-value/code-tree", headers=sef),
        "day": await client.get(_day(santiye), headers=sef),
        "previous": await client.get(_day(santiye, "/previous-allocation"), headers=sef),
        "budget": await client.get(_budget(santiye), headers=sef),
    }
    durumlar = {ad: r.status_code for ad, r in okumalar.items()}
    assert durumlar == dict.fromkeys(okumalar, 200), durumlar


async def test_B30_budget_view_not_editable_on_completed_site(
    client, sef, admin, seeded_db, santiye, boq, disiplinler
) -> None:
    """CEO eki: tamamlanmış şantiyenin TASLAĞI `editable: false` (yazmalar 409 ile aynı kural);
    taslak bulguları yine döner (dondurma engelleri gerçek analizdir)."""
    from .test_budget_api import _map

    await _map(client, santiye, admin, boq, disiplinler)  # taslak Rev 0
    active = (await client.get(_budget(santiye), headers=sef)).json()
    assert (active["revision"]["status"], active["editable"]) == ("draft", True)
    await _tamamla(seeded_db, santiye)
    view = (await client.get(_budget(santiye), headers=sef)).json()
    assert (view["revision"]["status"], view["editable"]) == ("draft", False)
    assert [f["code"] for f in view["freeze_blockers"]] == [
        f["code"] for f in active["freeze_blockers"]
    ]
