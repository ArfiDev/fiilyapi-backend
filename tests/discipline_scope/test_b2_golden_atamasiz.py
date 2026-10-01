"""DSC-B2 GOLDEN: ATAMASIZ aktörün B2 yazma uçları BİREBİR değişmez.

Her uç KENDİ TAZE dünyasında (test başına dünya, kök savepoint'te geri alınır) koşar ve
şunları dondurur: yazma YANITI · yazma sonrası DB durum dökümü (ev_* + BOQ + günlük tabloları,
TÜM kolonlar, sıralı) · yalnız o istekte yazılan audit satırları (`detail` metni dahil).

Golden `app/` DEĞİŞMEDEN (B1 sonu kodunda) alındı: `UPDATE_B2_GOLDEN=1 pytest …` YALNIZ yazar,
normal koşuda KIYASLAR. Kısıtsızda her `if not scope.is_restricted:` dalı bugünkü kodu AYNEN
çağırır; bu test o iddianın kanıtıdır (ör. `apply_lines_scoped` kısıtsız yolda çağrılırsa
kırılır).

Aktör: atamasız `system_admin` (her uç kapısından geçer: boq DELETE / reopen `admin`, katalog
`full`). Yazan-atamasız patron eşi (`yazar_atamasiz`) 403 testlerinin pozitif kontrolüdür.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests._disiplin_dunyasi import GUN1, GUN2, Dunya
from tests.discipline_scope._b2_golden_araclari import (
    anlik_goruntu,
    audit_kimlikleri,
    durum_dokumu,
    rastgele_kimlikleri_etiketle,
    yeni_audit,
)
from tests.discipline_scope._golden import golden_yolu, oku, yaz

YAZANLAR = True
GUNCELLE = os.environ.get("UPDATE_B2_GOLDEN") == "1"


@dataclass
class Ctx:
    c: AsyncClient
    d: Dunya
    h: dict[str, str]
    s: dict[str, str] = field(default_factory=dict)  # hazırlıkta üretilen kimlikler
    db: AsyncSession | None = None  # ham DB hazırlığı gereken senaryolar için


Adim = Callable[[Ctx], Awaitable[Response]]


def _b(x: Ctx, kuyruk: str = "") -> str:
    return f"/sites/{x.d.santiye.id}/earned-value/budget{kuyruk}"


async def _ok(resp: Response, kod: int) -> dict:
    assert resp.status_code == kod, resp.text
    return resp.json() if resp.content else {}


# ------------------------------------------------------------------ hazırlıklar


async def _bos_grup(x: Ctx) -> None:
    veri = await _ok(
        await x.c.post(
            f"/sites/{x.d.santiye.id}/boq/groups",
            headers=x.h,
            json={"name": "Boş Grup", "sort_order": 9},
        ),
        201,
    )
    x.s["grup"] = veri["id"]


async def _silinecek_kalem(x: Ctx) -> None:
    veri = await _ok(
        await x.c.post(
            f"/sites/{x.d.santiye.id}/boq/items",
            headers=x.h,
            json={
                "group_id": str(x.d.g["g3"].id),
                "code": "03.999",
                "description": "Silinecek",
                "unit": "m2",
                "quantity": "5",
                "unit_price": "2",
            },
        ),
        201,
    )
    x.s["kalem"] = veri["id"]


async def _gunluk_yeniden_ac(x: Ctx) -> None:
    await _ok(await x.c.post(f"/diary/{x.d.gunluk['e1'].id}/reopen", headers=x.h), 200)


async def _gerekceli_dagilim(x: Ctx) -> None:
    """05.05 dağılımı ÖNCE gerekçeli yazılır (sonraki gövdede YOK → temizlenir)."""
    govde = _dagilim(x, [("Ali Usta", "i1", "s1", "5"), ("Veli Usta", "i2", "s1", "4")])
    govde["unallocated_reason"] = "Eksik saat gerekçesi"
    yol = f"/sites/{x.d.santiye.id}/earned-value/days/{GUN1.isoformat()}/allocation"
    await _ok(await x.c.put(yol, headers=x.h, json=govde), 200)


async def _dusmus_satir_asili_kod(x: Ctx) -> None:
    """07.05'te canlı puantaj YOK: kayıtlı satır (Ali) canlıdan düşmüş + baseline'da olmayan
    (asılı) kod ve hücre. Kısıtsız tam değiştirme bunların HEPSİNİ siler."""
    from app.modules.earned_value.models import EvDayCell, EvDayCode, EvDayRow
    from tests._disiplin_dunyasi import GUN3, _kimlik

    assert x.db is not None
    asili = f"l:{x.d.yabanci_kalem_kimligi}:none"
    satir = EvDayRow(
        id=_kimlik(60, 1),
        site_id=x.d.santiye.id,
        day=GUN3,
        kind="personnel",
        personnel_id=_kimlik(40, 1),
        source_hours=5,
    )
    x.db.add(satir)
    await x.db.flush()
    x.db.add_all(
        [
            EvDayCode(site_id=x.d.santiye.id, day=GUN3, node_id=asili, rule="direct"),
            EvDayCell(row_id=satir.id, node_id=asili, hours=5),
        ]
    )
    await x.db.flush()


async def _taslagi_sil(x: Ctx) -> None:
    liste = await _ok(await x.c.get(_b(x, "/revisions"), headers=x.h), 200)
    taslak = next(r for r in liste if r["status"] == "draft")
    await _ok(await x.c.delete(_b(x, f"/revisions/{taslak['id']}"), headers=x.h), 204)


async def _yeni_disiplin(x: Ctx) -> None:
    veri = await _ok(
        await x.c.post(
            "/earned-value/disciplines",
            headers=x.h,
            json={
                "code": "MEK",
                "name": "Mekanik",
                "color": "#dc2626",
                "default_contractor_type": "own",
                "sort_order": 3,
            },
        ),
        201,
    )
    x.s["disiplin"] = veri["id"]


async def _rapor_onayla(x: Ctx) -> None:
    await _ok(
        await x.c.post(
            f"/sites/{x.d.santiye.id}/earned-value/reports/daily/{GUN1.isoformat()}/approve",
            headers=x.h,
        ),
        200,
    )


async def _taslak_sil_istek(x: Ctx) -> Response:
    """Taslağın kimliği rastgele: önce listeden çözülür (istekten ÖNCE, audit'e girmez)."""
    liste = await _ok(await x.c.get(_b(x, "/revisions"), headers=x.h), 200)
    taslak = next(r for r in liste if r["status"] == "draft")
    return await x.c.delete(_b(x, f"/revisions/{taslak['id']}"), headers=x.h)


# ------------------------------------------------------------------ istekler


def _post(yol: Callable[[Ctx], str], govde: Callable[[Ctx], dict] | dict | None = None) -> Adim:
    async def adim(x: Ctx) -> Response:
        g = govde(x) if callable(govde) else govde
        return await x.c.post(yol(x), headers=x.h, json=g)

    return adim


def _put(yol: Callable[[Ctx], str], govde: Callable[[Ctx], dict] | dict) -> Adim:
    async def adim(x: Ctx) -> Response:
        return await x.c.put(yol(x), headers=x.h, json=govde(x) if callable(govde) else govde)

    return adim


def _patch(yol: Callable[[Ctx], str], govde: Callable[[Ctx], dict] | dict) -> Adim:
    async def adim(x: Ctx) -> Response:
        return await x.c.patch(yol(x), headers=x.h, json=govde(x) if callable(govde) else govde)

    return adim


def _delete(yol: Callable[[Ctx], str]) -> Adim:
    async def adim(x: Ctx) -> Response:
        return await x.c.delete(yol(x), headers=x.h)

    return adim


def _site(x: Ctx) -> str:
    return f"/sites/{x.d.santiye.id}"


def _yaprak(x: Ctx, kalem: str, bolum: str) -> str:
    return f"l:{x.d.i[kalem].id}:{x.d.s1.id if bolum == 's1' else x.d.s2.id}"


def _dagilim(x: Ctx, hucreler: list[tuple[str, str, str, str]]) -> dict:
    """(kişi, kalem, bölüm, saat) → PUT allocation gövdesi."""
    kod = sorted({_yaprak(x, k, b) for _, k, b, _ in hucreler})
    kisi = {"Ali Usta": 1, "Veli Usta": 2}
    from tests._disiplin_dunyasi import _kimlik

    return {
        "codes": [{"node_id": n, "rule": "direct"} for n in kod],
        "cells": [
            {
                "row": {"kind": "personnel", "ref_id": str(_kimlik(40, kisi[p]))},
                "node_id": _yaprak(x, k, b),
                "hours": saat,
            }
            for p, k, b, saat in hucreler
        ],
    }


def _satirlar_e3(x: Ctx) -> dict:
    return {
        "lines": [
            {"boq_item_id": str(x.d.i["i2"].id), "section_id": str(x.d.s1.id), "quantity": "7"},
            {"boq_item_id": str(x.d.i["i1"].id), "section_id": None, "quantity": "3"},
        ]
    }


def _satirlar_e1(x: Ctx) -> dict:
    i, s1, s2 = x.d.i, str(x.d.s1.id), str(x.d.s2.id)
    return {
        "lines": [
            {"boq_item_id": str(i["i1"].id), "section_id": s1, "quantity": "6"},
            {"boq_item_id": str(i["i1"].id), "section_id": s2, "quantity": "4"},
            {"boq_item_id": str(i["i2"].id), "section_id": s1, "quantity": "5"},
            {"boq_item_id": str(i["i3"].id), "section_id": s2, "quantity": "1"},
        ]
    }


@dataclass(frozen=True)
class Senaryo:
    istek: Adim
    hazirlik: Callable[[Ctx], Awaitable[None]] | None = None


SENARYOLAR: dict[str, Senaryo] = {
    # ---- BOQ
    "boq_grup_olustur": Senaryo(
        _post(lambda x: f"{_site(x)}/boq/groups", {"name": "Yeni Grup", "sort_order": 4})
    ),
    "boq_kalem_olustur": Senaryo(
        _post(
            lambda x: f"{_site(x)}/boq/items",
            lambda x: {
                "group_id": str(x.d.g["g1"].id),
                "code": "01.999",
                "description": "Yeni kalem",
                "unit": "m3",
                "quantity": "12",
                "unit_price": "3.50",
                "sort_order": 2,
            },
        )
    ),
    # CEO onaylı ek vaka: atamasız yazar EŞLEMESİZ gruba (G3) kalem ekler — B1 kodunda da 201
    # (hedef grup denetimi atamasıza UYGULANMAZ); kısıtsız yol bugünkü gibi kalmalı.
    "boq_kalem_olustur_eslemesiz": Senaryo(
        _post(
            lambda x: f"{_site(x)}/boq/items",
            lambda x: {
                "group_id": str(x.d.g["g3"].id),
                "code": "03.999",
                "description": "Eşlemesiz gruba kalem",
                "unit": "m2",
                "quantity": "5",
                "unit_price": "4",
                "sort_order": 2,
            },
        )
    ),
    "boq_grup_guncelle": Senaryo(
        _patch(lambda x: f"/boq/groups/{x.d.g['g1'].id}", {"name": "Betonarme II", "sort_order": 7})
    ),
    "boq_kalem_guncelle": Senaryo(
        _patch(
            lambda x: f"/boq/items/{x.d.i['i1'].id}",
            # SZK-B1: I1 sözleşmeye bağlı; `description`/`unit_price` kilitli (422) → yalnız
            # kilitsiz alanlar (`quantity`, `group_id`) yazılır.
            lambda x: {
                "quantity": "110",
                "group_id": str(x.d.g["g2"].id),
            },
        )
    ),
    "boq_tahsis_degistir": Senaryo(
        _put(
            lambda x: f"/boq/items/{x.d.i['i1'].id}/allocations",
            lambda x: {
                "allocations": [
                    {"section_id": str(x.d.s1.id), "quantity": "50"},
                    {"section_id": str(x.d.s2.id), "quantity": "30"},
                ]
            },
        )
    ),
    "boq_grup_sil": Senaryo(_delete(lambda x: f"/boq/groups/{x.s['grup']}"), _bos_grup),
    "boq_kalem_sil": Senaryo(_delete(lambda x: f"/boq/items/{x.s['kalem']}"), _silinecek_kalem),
    # ---- günlük
    "gunluk_olustur": Senaryo(
        _post(
            lambda x: f"{_site(x)}/diary",
            lambda x: {
                "entry_date": "2026-05-09",
                # Başlıkta section_id YOK: GKS-B1 başlık bölümü iskeleti süzer (tam
                # tahsisli kalem Bölümsüz açılmaz); o davranış
                # tests/site_diary/test_gks_b1_* içinde bekçili. Bu golden yalnız
                # "atamasız kullanıcının günlük OLUŞTURMA yanıtı DSC ile değişmedi"yi
                # bekçiler; bölümsüz POST bölüm süzmesinden bağımsızdır.
                # I2 (tam tahsisli) G4 gereği S1 satırıyla açılır; bu golden G4'ü de
                # atamasız kullanıcı için bekçiler; main'den farkı yalnız I2 satırıdır (GKS-B1).
                "work_done": "Yeni gün",
            },
        )
    ),
    "gunluk_guncelle": Senaryo(
        _patch(
            lambda x: f"/diary/{x.d.gunluk['e3'].id}",
            {"work_done": "Güncel iş", "chief_note": "Not", "weather": "sunny"},
        )
    ),
    "gunluk_satirlar": Senaryo(_put(lambda x: f"/diary/{x.d.gunluk['e3'].id}/lines", _satirlar_e3)),
    "gunluk_satirlar_yetim_dusurur": Senaryo(
        _put(lambda x: f"/diary/{x.d.gunluk['e1'].id}/lines", _satirlar_e1), _gunluk_yeniden_ac
    ),
    "gunluk_sil": Senaryo(_delete(lambda x: f"/diary/{x.d.gunluk['e3'].id}")),
    "gunluk_gonder": Senaryo(_post(lambda x: f"/diary/{x.d.gunluk['e3'].id}/submit")),
    "gunluk_yeniden_ac": Senaryo(_post(lambda x: f"/diary/{x.d.gunluk['e1'].id}/reopen")),
    # ---- EV: gün dağılımı ve bütçe
    "ev_gun_dagilim": Senaryo(
        _put(
            lambda x: f"{_site(x)}/earned-value/days/{GUN2.isoformat()}/allocation",
            lambda x: _dagilim(x, [("Ali Usta", "i1", "s1", "5"), ("Veli Usta", "i2", "s1", "4")]),
        )
    ),
    "ev_gun_dagilim_tam_degistirme": Senaryo(
        _put(
            lambda x: f"{_site(x)}/earned-value/days/{GUN1.isoformat()}/allocation",
            lambda x: _dagilim(x, [("Ali Usta", "i1", "s1", "5")]),
        )
    ),
    "ev_gun_dagilim_gerekce_temizlenir": Senaryo(
        _put(
            lambda x: f"{_site(x)}/earned-value/days/{GUN1.isoformat()}/allocation",
            lambda x: _dagilim(x, [("Ali Usta", "i1", "s1", "5")]),
        ),
        _gerekceli_dagilim,
    ),
    "ev_gun_dagilim_dusmus_satir_asili_kod": Senaryo(
        _put(
            lambda x: f"{_site(x)}/earned-value/days/2026-05-07/allocation",
            lambda x: (
                _dagilim(x, []) | {"codes": [{"node_id": _yaprak(x, "i1", "s1"), "rule": "direct"}]}
            ),
        ),
        _dusmus_satir_asili_kod,
    ),
    "ev_butce_kalem": Senaryo(
        _patch(
            lambda x: _b(x, f"/items/{x.d.i['i2'].id}"),
            {"contractor_type": "own", "is_direct": False},
        )
    ),
    "ev_butce_yapraklar": Senaryo(
        _patch(
            lambda x: _b(x, "/leaves"),
            lambda x: {
                "leaves": [
                    {
                        "boq_item_id": str(x.d.i["i1"].id),
                        "section_id": str(x.d.s1.id),
                        "unit_mhr": "3",
                    }
                ]
            },
        )
    ),
    "ev_grup_esleme": Senaryo(
        _put(
            lambda x: _b(x, "/group-disciplines"),
            lambda x: {
                "items": [{"boq_group_id": str(x.d.g["g3"].id), "discipline_id": str(x.d.kab.id)}]
            },
        )
    ),
    "ev_dagilim_tipi": Senaryo(
        _put(
            lambda x: _b(x, "/distributions"),
            lambda x: {"items": [{"discipline_id": str(x.d.kab.id), "distribution": "bell"}]},
        )
    ),
    "ev_pencere": Senaryo(
        _put(
            lambda x: _b(x, "/windows"),
            lambda x: {
                "windows": [
                    {
                        "discipline_id": str(x.d.kab.id),
                        "section_id": str(x.d.s1.id),
                        "start_date": "2026-05-05",
                        "end_date": "2026-05-12",
                    }
                ]
            },
        )
    ),
    "ev_katalogdan_doldur": Senaryo(_post(lambda x: _b(x, "/fill-from-catalog"))),
    "ev_dondur": Senaryo(_post(lambda x: _b(x, "/freeze"), {"name": "Rev A", "description": "d"})),
    "ev_taslak_ac": Senaryo(_post(lambda x: _b(x, "/revisions")), _taslagi_sil),
    "ev_taslak_sil": Senaryo(_taslak_sil_istek),
    # ---- EV: Ü6 katalog / disiplin / rapor / kilit
    "ev_katalog_olustur": Senaryo(
        _post(
            lambda x: "/earned-value/catalog",
            lambda x: {
                "discipline_id": str(x.d.kab.id),
                "name": "Kalıp",
                "uom": "m2",
                "standard_unit_mhr": "0.75",
                "default_contractor_type": "own",
                "description": "Yeni",
            },
        )
    ),
    "ev_katalog_guncelle": Senaryo(
        _patch(
            lambda x: f"/earned-value/catalog/{_katalog1()}",
            {"standard_unit_mhr": "2.10", "description": "Güncel"},
        )
    ),
    "ev_katalog_gercek_al": Senaryo(
        _post(lambda x: f"/earned-value/catalog/{_katalog1()}/adopt-actual")
    ),
    "ev_disiplin_olustur": Senaryo(
        _post(
            lambda x: "/earned-value/disciplines",
            {
                "code": "MEK",
                "name": "Mekanik",
                "color": "#dc2626",
                "default_contractor_type": "own",
                "sort_order": 3,
            },
        )
    ),
    "ev_disiplin_guncelle": Senaryo(
        _patch(lambda x: f"/earned-value/disciplines/{x.d.kab.id}", {"name": "Kaba İnşaat"})
    ),
    "ev_disiplin_sil": Senaryo(
        _delete(lambda x: f"/earned-value/disciplines/{x.s['disiplin']}"), _yeni_disiplin
    ),
    "ev_rapor_onayla": Senaryo(
        _post(lambda x: f"{_site(x)}/earned-value/reports/daily/{GUN1.isoformat()}/approve")
    ),
    "ev_gun_kilidi_ac": Senaryo(
        _post(
            lambda x: f"{_site(x)}/earned-value/days/{GUN1.isoformat()}/unlock",
            {"reason": "Düzeltme gerekli"},
        ),
        _rapor_onayla,
    ),
}


def _katalog1() -> str:
    from tests._disiplin_dunyasi import _kimlik

    return str(_kimlik(21, 1))


@pytest.mark.parametrize("ad", sorted(SENARYOLAR))
async def test_atamasiz_yazma_golden_ile_birebir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    dunya: Dunya,
    atamasiz: dict[str, str],
    ad: str,
) -> None:
    senaryo = SENARYOLAR[ad]
    x = Ctx(client, dunya, atamasiz, db=seeded_db)
    if senaryo.hazirlik is not None:
        await senaryo.hazirlik(x)
    once = await audit_kimlikleri(seeded_db)
    resp = await senaryo.istek(x)
    yanit = {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "body": resp.json() if resp.content else None,
    }
    await rastgele_kimlikleri_etiketle(seeded_db, dunya)
    goruntu = anlik_goruntu(
        dunya, yanit, await durum_dokumu(seeded_db, dunya), await yeni_audit(seeded_db, once)
    )
    dosya = f"b2_{ad}"
    if GUNCELLE:
        yaz(dosya, goruntu)
        return
    assert golden_yolu(dosya).exists(), f"golden yok: {dosya} (UPDATE_B2_GOLDEN=1 ile üret)"
    assert goruntu == oku(dosya)
