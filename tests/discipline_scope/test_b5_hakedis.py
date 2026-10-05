"""DSC-B5 Ü2 — hakediş router'ları + günlükten hakediş önerisi + sözleşme dağıtımı: KISITLI
kullanıcıya 403 (`RequireUnrestricted`, gövde birebir); K7: günlük özetinde sözleşme birim
fiyatı kısıtlıda None.

F1 POZİTİF KONTROL: her 403'ün yanında aynı roldeki ATAMASIZ eş 403 ALMAZ (200 ya da uca özgü
iş kuralı 404/409/422). S2: bağımlılık 404'ten önce koşar → olmayan kimlik de kısıtlıya 403.
Bağımlılık ağacı bekçisi: `tests/core/test_disiplin_rota_bekcisi.py::U6_ROTALARI`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import AsyncClient

from app.core.discipline_deps import require_unrestricted
from app.main import app
from tests._disiplin_dunyasi import _kimlik
from tests._silme_yardimci import AILE_YOLLARI, sil_aile
from tests.core.test_disiplin_rota_bekcisi import _bagimlilik_agacinda
from tests.discipline_scope._b2_yardim import SISYON_ONLY, YETKI_YOK
from tests.discipline_scope._b4_dunya import DunyaB4

D = Decimal
YOK = uuid.UUID(int=0xB5B5)
ODEME = _kimlik(70, 1)

# (kısıtlı, atamasız eş) rol çiftleri
PM = ("civil", "pm_atamasiz")
PATRON = ("civil_yazar", "yazar_atamasiz")

# ad -> (çift, yöntem, yol, gövde, atamasız için kabul edilen durumlar)
SENARYOLAR: dict[str, tuple[tuple[str, str], str, str, dict | None, tuple[int, ...]]] = {
    "isv_liste": (PM, "GET", "/progress-payments", None, (200,)),
    "isv_detay": (PM, "GET", f"/progress-payments/{ODEME}", None, (200,)),
    "isv_ozet": (PM, "GET", "/projects/{pid}/progress-payments/summary", None, (200,)),
    "isv_olustur": (PATRON, "POST", "/projects/{pid}/progress-payments", {}, (200, 201)),
    "isv_satirlar": (PATRON, "PUT", f"/progress-payments/{ODEME}/lines", {"lines": []}, (409,)),
    "isv_guncelle": (PATRON, "PATCH", f"/progress-payments/{ODEME}", {"description": "x"}, (409,)),
    "isv_gecis_submit": (PATRON, "POST", f"/progress-payments/{ODEME}/submit", None, (409,)),
    "isv_olmayan_kimlik": (PM, "GET", f"/progress-payments/{YOK}", None, (404,)),
    "tas_liste": (PM, "GET", "/subcontractor-progress-payments", None, (200,)),
    "tas_ozet": (PM, "GET", "/subcontractor-progress-payments/summary", None, (200,)),
    "tas_detay_olmayan": (PM, "GET", f"/subcontractor-progress-payments/{YOK}", None, (404,)),
    "tas_olustur_olmayan": (
        PATRON,
        "POST",
        f"/subcontractor-contracts/{YOK}/progress-payments",
        {},
        (404,),
    ),
    "tas_satirlar_olmayan": (
        PATRON,
        "PUT",
        f"/subcontractor-progress-payments/{YOK}/lines",
        {"lines": []},
        (404,),
    ),
    "tas_gecis_approve_olmayan": (
        PATRON,
        "POST",
        f"/subcontractor-progress-payments/{YOK}/approve",
        None,
        (404,),
    ),
    "oneri_isveren": (
        PM,
        "GET",
        "/projects/{pid}/progress-payments/diary-suggestion",
        None,
        (200,),
    ),
    "oneri_tasaron_olmayan": (
        PM,
        "GET",
        f"/subcontractor-contracts/{YOK}/progress-payments/diary-suggestion",
        None,
        (404,),
    ),
    "dagitim_oku": (PATRON, "GET", "/projects/{pid}/contract/distribution", None, (200,)),
    "dagitim_yaz": (
        PATRON,
        "PUT",
        "/projects/{pid}/contract/distribution",
        {"allocations": []},
        (200,),
    ),
}


def _yol(x: DunyaB4, sablon: str) -> str:
    return sablon.replace("{pid}", str(x.d.proje.id))


#: IZN-B3: proje baglamsiz LISTE uclari 403 DEGIL proje basina SUZER (kisitli projenin satirlari
#: disarida kalir; asagida `test_liste_uclari_*`). Geri kalan her senaryo tek-proje → 403.
LISTE_SENARYOLARI = frozenset({"isv_liste", "tas_liste", "tas_ozet"})


@pytest.mark.parametrize("ad", sorted(set(SENARYOLAR) - LISTE_SENARYOLARI))
async def test_kisitliya_403_govde_birebir(client: AsyncClient, dunya_b4: DunyaB4, ad: str) -> None:
    (kisitli, _), yontem, yol, govde, _ = SENARYOLAR[ad]
    ret = await client.request(
        yontem, _yol(dunya_b4, yol), headers=dunya_b4.d.baslik[kisitli], json=govde
    )
    assert ret.status_code == 403 and ret.json() == YETKI_YOK, (ad, ret.status_code, ret.text)


@pytest.mark.parametrize("ad", sorted(SENARYOLAR))
async def test_atamasiz_es_403_almaz_pozitif_kontrol(
    client: AsyncClient, dunya_b4: DunyaB4, ad: str
) -> None:
    (_, es), yontem, yol, govde, kabul = SENARYOLAR[ad]
    ret = await client.request(
        yontem, _yol(dunya_b4, yol), headers=dunya_b4.d.baslik[es], json=govde
    )
    assert ret.status_code != 403, (ad, ret.text)
    assert ret.status_code in kabul, (ad, ret.status_code, ret.text)


@pytest.mark.parametrize(
    ("kind", "kimlik", "beklenen"),
    [
        # SIL-B2: onaylı/ödenmiş hakediş de silinir (mali aile motorla); disiplin ASLA 403 değil.
        ("progress_payment", ODEME, 204),
        ("subcontractor_progress_payment", YOK, 404),
    ],
)
async def test_silmede_disiplin_kisiti_uygulanmaz_sistem_yoneticisi_atanmis_olsa_da_siler(
    client: AsyncClient, dunya_b4: DunyaB4, kind: str, kimlik: uuid.UUID, beklenen: int
) -> None:
    """SIL-B1/B2: DELETE'te disiplin kısıtı YOK. Disiplin atanmış Sistem Yöneticisi 403 DEĞİL:
    belirteçsiz DELETE 428 (önizleme şartı, disiplinden önce), önizleme + belirteçle motor sonucu
    (silinir / kayıt yok). Sistem Yöneticisi olmayan kısıtlı rol sistem yöneticisi kapısında 403."""
    baslik = dunya_b4.d.baslik
    yol = f"{AILE_YOLLARI[kind]}/{kimlik}"
    belirtecsiz = await client.delete(yol, headers=baslik["admin_kisitli"])
    assert belirtecsiz.status_code == 428, belirtecsiz.text
    assert belirtecsiz.json()["code"] == "preview_required"
    kisitli_admin = await sil_aile(client, baslik["admin_kisitli"], kind, kimlik)
    assert kisitli_admin.status_code == beklenen, kisitli_admin.text
    sirada = await client.delete(yol, headers=baslik["civil_yazar"])
    assert sirada.status_code == 403 and sirada.json() == SISYON_ONLY


async def test_admin_kisitli_da_403(client: AsyncClient, dunya_b4: DunyaB4) -> None:
    """Rol ne olursa olsun kısıtlı atama TEK-PROJE ucunda 403 alır (atama belirleyici)."""
    ret = await client.get(
        f"/progress-payments/{ODEME}", headers=dunya_b4.d.baslik["admin_kisitli"]
    )
    assert ret.status_code == 403 and ret.json() == YETKI_YOK, ret.text


async def test_liste_uclari_kisitli_projeyi_disarida_birakir_403_vermez(
    client: AsyncClient, dunya_b4: DunyaB4
) -> None:
    """IZN-B3 (CEO karari): kisitli kisi liste ucunda 403 ALMAZ; disiplinle kisitli oldugu projenin
    (P1) hakedisleri listede/ozette YOK (tek-proje ucundaki 403 ile tutarli: "o projenin hakedisi
    yok"), atamasiz es (ayni rol) P1 satirini GORUR (pozitif kontrol)."""
    for ad, es in (("civil", "pm_atamasiz"), ("admin_kisitli", "atamasiz")):
        kisitli = await client.get("/progress-payments", headers=dunya_b4.d.baslik[ad])
        assert kisitli.status_code == 200, (ad, kisitli.text)
        assert [
            i for i in kisitli.json()["items"] if i["project_id"] == str(dunya_b4.d.proje.id)
        ] == []
        serbest = await client.get("/progress-payments", headers=dunya_b4.d.baslik[es])
        assert any(i["project_id"] == str(dunya_b4.d.proje.id) for i in serbest.json()["items"])
    for yol in ("/subcontractor-progress-payments", "/subcontractor-progress-payments/summary"):
        assert (await client.get(yol, headers=dunya_b4.d.baslik["civil"])).status_code == 200, yol


def test_hakedis_router_larindaki_HER_rota_require_unrestricted_tasir() -> None:
    """Router düzeyi bağımlılık: hakediş router'larına sonradan eklenen rota da otomatik kapalı."""
    from app.modules.progress_payments.router import router as isveren
    from app.modules.site_diary.router_suggestion import router as oneri
    from app.modules.subcontractor_progress_payments.router import router as tasaron
    from app.modules.subcontractor_progress_payments.router_transitions import (
        router as gecisler,
    )

    sayac = 0
    for router in (isveren, tasaron, gecisler, oneri):
        for rota in router.routes:
            if not isinstance(rota, APIRoute):
                continue
            sayac += 1
            assert _bagimlilik_agacinda(rota.dependant, require_unrestricted), rota.path
    assert sayac >= 28, sayac


def test_hakedis_yollari_uygulamada_hep_kapali() -> None:
    """Uygulama seviyesinde (EFEKTİF bağlam, `include_router` sarmaları dahil): yolu hakediş
    önekli HER rota `require_unrestricted` taşır."""
    bulunan = 0
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute) or "progress-payments" not in ctx.path:
            continue
        bulunan += 1
        dependant = ctx.dependant or ctx.original_route.dependant
        assert _bagimlilik_agacinda(dependant, require_unrestricted), ctx.path
    assert bulunan >= 28, bulunan


# --------------------------------------------------------------------------- K7


async def test_k7_gunluk_ozeti_sozlesme_birim_fiyati_kisitlida_none(
    client: AsyncClient, dunya_b4: DunyaB4
) -> None:
    d = dunya_b4.d
    url = f"/sites/{d.santiye.id}/diary/summary"
    civil = (await client.get(url, headers=d.baslik["civil"])).json()["items"]
    es = (await client.get(url, headers=d.baslik["pm_atamasiz"])).json()["items"]
    civil_i = {i["boq_item_id"]: i for i in civil}
    assert civil_i and set(civil_i) <= {i["boq_item_id"] for i in es}
    for kalem in es:
        kisitli = civil_i.get(kalem["boq_item_id"])
        if kisitli is None:
            continue
        assert kalem["contract_item_unit_price"] is not None  # atamasızda dolu
        assert kisitli["contract_item_unit_price"] is None
        assert kisitli["contract_item_quantity"] == kalem["contract_item_quantity"]
        assert kisitli["contract_item_id"] == kalem["contract_item_id"]
