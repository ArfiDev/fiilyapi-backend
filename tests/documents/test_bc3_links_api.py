"""BC-3 — bağ uçları: 1 katalog + dört sahip × 4 uç, uçtan çağrılarak.

Dört sahip `sahip` fixture'ıyla parametriktir; her test gövdesi dört kez koşar
(bölüm · ünite · satış · taşeron sözleşmesi). Kapılar UÇTAN çarpılarak ölçülür
(çağrı yeri de mutanttır) ve her 4xx'in yanında geçerli değerin GEÇTİĞİ bir
pozitif kontrol vardır (K-IKIZ1).

## Rota sırası — ÖLÇÜLDÜ, bugün gölgeleme YOK; tripwire var

`documents_router` `GET /documents/{document_id}` TAŞIMAZ (yalnız PATCH/DELETE),
sıra kısıtı ise yalnız aynı metotta doğar → `GET /documents/slot-types` hiçbir
sırada UUID sanılamaz (`test_rota_slot_types_link_routera_duser_SIRA_ne_olursa_olsun`
bunu ters sırayla da ölçer). Sırayı ölçen bir bekçi eşdeğer mutant olurdu; onun
yerine ÖN KOŞUL kilitlidir (`test_TRIPWIRE_documents_routerda_GET_detay_YOK`) ve
çözücünün kör olmadığı sentetik bir GET detay ucuyla kanıtlanır
(`test_POZITIF_KONTROL_sentetik_GET_detay_eklenince_golgeleme_OLUSUR`).
"""

import uuid
from datetime import date

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from app.core.access import AccessLevel
from app.core.router_registry import ROUTERS
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.documents import link_guards as guards
from app.modules.documents.link_router import router as link_router
from app.modules.documents.models import Document
from app.modules.documents.router import router as documents_router
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from tests.documents.conftest import SahipDurumu

SLOT_TYPES_PATH = "/documents/slot-types"


def _owner_path(sahip: SahipDurumu, owner_id: uuid.UUID) -> str:
    return f"{sahip.spec.route_root}/{owner_id}/documents"


def _link_path(sahip: SahipDurumu, link_id: uuid.UUID) -> str:
    return f"{sahip.spec.route_root}/documents/{link_id}"


def _ilk_slot(slot_katalogu, sahip: SahipDurumu):
    """Sahibin bölmesindeki `sort_order=1` slotu."""
    return next(
        t
        for (scope, _code), t in slot_katalogu.items()
        if scope == sahip.spec.scope.value and t.sort_order == 1
    )


def _yabanci_slot(slot_katalogu, sahip: SahipDurumu):
    """BAŞKA bölmenin bir slotu (bileşik FK / 422 yüzeyi)."""
    return next(t for (scope, _c), t in slot_katalogu.items() if scope != sahip.spec.scope.value)


async def _bagla(client: AsyncClient, headers, sahip: SahipDurumu, slot, belge, **extra) -> dict:
    resp = await client.post(
        _owner_path(sahip, sahip.owner_id),
        json={"type_id": str(slot.id), "document_id": str(belge.id), **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Katalog + rota sırası
# ---------------------------------------------------------------------------


async def test_katalog_18_slot_scope_suzgeci_ve_sira(
    client: AsyncClient, muhasebe_headers, slot_katalogu
) -> None:
    hepsi = await client.get(SLOT_TYPES_PATH, headers=muhasebe_headers)
    assert hepsi.status_code == 200, hepsi.text
    assert len(hepsi.json()["items"]) == 18

    satis = await client.get(
        SLOT_TYPES_PATH, params={"scope": "unit_sale"}, headers=muhasebe_headers
    )
    items = satis.json()["items"]
    assert [i["sort_order"] for i in items] == [1, 2, 3, 4, 5, 6]
    assert [i["code"] for i in items if i["is_required"]] == ["sales_contract", "buyer_id"]
    assert {i["scope"] for i in items} == {"unit_sale"}

    bozuk = await client.get(
        SLOT_TYPES_PATH, params={"scope": "project_contract"}, headers=muhasebe_headers
    )
    assert bozuk.status_code == 422, "işveren sözleşmesi bölmesi KAPSAM DIŞI — enum üyesi değil"


def _kazanan_router(uygulama: FastAPI, yol: str, metot: str = "GET"):
    """Starlette'in eşleme algoritmasını birebir koşar: İLK FULL eşleşme kazanır
    (`tests/modules/ai/test_ai0a_router_registry.py::_kazanan_router` ile aynı teknik;
    kimlik/seed gerektirmez, sırayı doğrudan okur). Hiçbir rota tutmazsa None."""
    kapsam = {
        "type": "http",
        "method": metot,
        "path": yol,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for rota in uygulama.routes:
        eslesme, _ = rota.matches(kapsam)
        if eslesme == Match.FULL and isinstance(rota, _IncludedRouter):
            return rota.original_router
    return None


def _documents_router_GET_detay_yollari() -> list[str]:
    return [
        r.path
        for r in documents_router.routes
        if isinstance(r, APIRoute) and "GET" in r.methods and r.path == "/documents/{document_id}"
    ]


def test_rota_slot_types_link_routera_duser_SIRA_ne_olursa_olsun() -> None:
    """Bugünkü sırada VE ters sırada literal kazanır — çünkü aynı metotta rakip yok.
    Bu test sırayı DEĞİL, gölgelemenin bugün imkânsız olduğunu ölçer."""
    assert _kazanan_router(app, SLOT_TYPES_PATH) is link_router

    sirali = list(ROUTERS)
    i, j = sirali.index(link_router), sirali.index(documents_router)
    assert i < j, "ROUTERS'ta link router documents_router'dan ÖNCE durur (sigorta)"
    sirali[i], sirali[j] = sirali[j], sirali[i]
    ters = FastAPI()
    for router in sirali:
        ters.include_router(router)
    assert _kazanan_router(ters, SLOT_TYPES_PATH) is link_router, (
        "ters sırada da literal kazanmalı: documents_router'da GET detay yolu yok"
    )


def test_TRIPWIRE_documents_routerda_GET_detay_YOK() -> None:
    """🔴 ÖN KOŞUL: `documents_router` `GET /documents/{document_id}` TAŞIMAZ (yalnız
    PATCH/DELETE). Biri o ucu açtığı gün bu test kırmızıya döner ve `ROUTERS`
    sırası GERÇEKTEN zorunlu olur — o gün bu tripwire sıra bekçisine dönüştürülür."""
    assert _documents_router_GET_detay_yollari() == []
    assert _kazanan_router(app, f"/documents/{uuid.uuid4()}") is None, (
        "GET /documents/<uuid> hiçbir rotaya FULL düşmemeli (405 yüzeyi)"
    )


def test_POZITIF_KONTROL_sentetik_GET_detay_eklenince_golgeleme_OLUSUR() -> None:
    """Çözücü kör değil: `GET /documents/{document_id}` taşıyan sentetik bir router
    link router'dan ÖNCE kaydedilirse `slot-types` UUID sanılır — gölgeleme gerçek."""
    sahte = APIRouter()

    @sahte.get("/documents/{document_id}")
    async def _sahte_detay(document_id: uuid.UUID) -> dict:  # pragma: no cover
        return {}

    golgeli = FastAPI()
    golgeli.include_router(sahte)
    golgeli.include_router(link_router)
    assert _kazanan_router(golgeli, SLOT_TYPES_PATH) is sahte

    # Aynı sentetik router link router'dan SONRA kaydedilirse literal kazanır:
    # sıra tam olarak o gün anlam kazanır.
    duzgun = FastAPI()
    duzgun.include_router(link_router)
    duzgun.include_router(sahte)
    assert _kazanan_router(duzgun, SLOT_TYPES_PATH) is link_router


async def test_rota_UCTAN_slot_types_200_UUID_405(
    client: AsyncClient, admin_headers, slot_katalogu
) -> None:
    """Aynı ölçümün HTTP hâli: literal 200; `GET /documents/<uuid>` 405 — yol var
    (PATCH/DELETE), GET yok. 404 DEĞİL: 404 bir GET detay ucunun VARLIĞI demek olurdu."""
    assert (await client.get(SLOT_TYPES_PATH, headers=admin_headers)).status_code == 200
    assert (
        await client.get(f"/documents/{uuid.uuid4()}", headers=admin_headers)
    ).status_code == 405


# ---------------------------------------------------------------------------
# Sahip başına dört uç (parametrik)
# ---------------------------------------------------------------------------


async def test_liste_bos_baslar_bagla_201_liste_dolar(
    client: AsyncClient,
    pm_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje: Project,
    belge_fabrikasi,
) -> None:
    bos = await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)
    assert bos.status_code == 200, bos.text
    assert bos.json() == {"items": []}

    slot = _ilk_slot(slot_katalogu, sahip)
    belge = await belge_fabrikasi(proje, "imzali.pdf", data=b"%PDF")
    govde = await _bagla(client, pm_headers, sahip, slot, belge, note="ıslak imzalı")

    assert govde["owner_id"] == str(sahip.owner_id)
    assert govde["scope"] == sahip.spec.scope.value
    assert govde["type_code"] == slot.code
    assert govde["is_required"] == slot.is_required
    assert govde["document_id"] == str(belge.id)
    assert govde["document"]["filename"] == "imzali.pdf"
    assert govde["note"] == "ıslak imzalı"

    liste = await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)
    assert [i["id"] for i in liste.json()["items"]] == [govde["id"]]


async def test_baska_projenin_belgesi_422_var_olmayan_belge_AYNI_cumle(
    client: AsyncClient,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    ikinci_proje: Project,
    belge_fabrikasi,
) -> None:
    """Kapsam eşitliği YALNIZ servistedir (bağ tablosu `project_id` kopyalamaz) —
    bu test o tek katın bekçisidir. Admin bile başka projenin belgesini bağlayamaz:
    kural görünürlük değil, KAPSAM eşitliğidir."""
    slot = _ilk_slot(slot_katalogu, sahip)
    yabanci_belge = await belge_fabrikasi(ikinci_proje, "yabanci.pdf")
    for headers in (pm_headers, admin_headers):
        resp = await client.post(
            _owner_path(sahip, sahip.owner_id),
            json={"type_id": str(slot.id), "document_id": str(yabanci_belge.id)},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == guards.DOCUMENT_NOT_IN_SCOPE

    yok = await client.post(
        _owner_path(sahip, sahip.owner_id),
        json={"type_id": str(slot.id), "document_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert yok.status_code == 422
    assert yok.json()["detail"] == guards.DOCUMENT_NOT_IN_SCOPE, "varlık sızdırılmaz"


async def test_baska_bolmenin_slotu_422_var_olmayan_slot_AYNI_cumle(
    client: AsyncClient,
    pm_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje: Project,
    belge_fabrikasi,
) -> None:
    belge = await belge_fabrikasi(proje, "dosya.pdf")
    for type_id in (_yabanci_slot(slot_katalogu, sahip).id, uuid.uuid4()):
        resp = await client.post(
            _owner_path(sahip, sahip.owner_id),
            json={"type_id": str(type_id), "document_id": str(belge.id)},
            headers=pm_headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == guards.SLOT_TYPE_INVALID


async def test_gorunmeyen_sahip_404_sahibin_cumlesi_admin_icin_VAR(
    client: AsyncClient, pm_headers, admin_headers, sahip: SahipDurumu, slot_katalogu
) -> None:
    """IDOR: kapsamı `proje`ye kısıtlı PM, ikinci projenin kaydını göremez — 404 ve
    cümle SAHİBİN kendi ucunun cümlesi (`GET /<kök>/{id}` ile fark yok, sızıntı yok).
    Pozitif kontrol: aynı UUID admin için 200 — kayıt gerçekten vardır."""
    yol = _owner_path(sahip, sahip.yabanci_owner_id)
    yasak = await client.get(yol, headers=pm_headers)
    assert yasak.status_code == 404, yasak.text
    assert yasak.json()["detail"] == sahip.spec.owner_missing

    yazma = await client.post(
        yol,
        json={"type_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4())},
        headers=pm_headers,
    )
    assert yazma.status_code == 404, "görünmeyen sahipte 422'ye bile ulaşılmaz"

    assert (await client.get(yol, headers=admin_headers)).status_code == 200

    hic_yok = await client.get(_owner_path(sahip, uuid.uuid4()), headers=admin_headers)
    assert hic_yok.status_code == 404
    assert hic_yok.json()["detail"] == sahip.spec.owner_missing


async def test_view_rolu_OKUR_BAGLAR_GUNCELLER_ama_SILEMEZ(
    client: AsyncClient,
    muhasebe_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    """Kapı kararı (görme → bağlama) IZN-B5b'de DEĞİŞTİ (CEO, yan bulgu): `unit`, `unit_sale`,
    `subcontractor_contract` sahiplerinde BAĞLAMA/KÜNYE yazması sahibin ANA sayfasının
    Düzenler'i ister (`link_router._YAZMA_SAYFASI`); `accounting` rolü bu sayfaları yalnız
    GÖRÜR → OKUR ama BAĞLAYAMAZ/GÜNCELLEYEMEZ. `section` bu turda kapsam dışı → eski
    2026-09-05 kararı (`sites:view` yeter). SİLME her durumda `full`/Sistem Yöneticisi (403).

    KARŞIT KANITLA ölçülür: rolün aynı ucu bir sahipte geçmesi, ötekinde 403 alması.
    """
    belge = await belge_fabrikasi(proje, "a.pdf")
    slot = _ilk_slot(slot_katalogu, sahip)
    govde = {"type_id": str(slot.id), "document_id": str(belge.id)}

    # GET — view yeter
    assert (
        await client.get(_owner_path(sahip, sahip.owner_id), headers=muhasebe_headers)
    ).status_code == 200

    if sahip.spec.key == "section":
        olustur = await client.post(
            _owner_path(sahip, sahip.owner_id), json=govde, headers=muhasebe_headers
        )
        assert olustur.status_code == 201, olustur.text
        link = _link_path(sahip, olustur.json()["id"])
        assert (
            await client.patch(link, json={"note": "muhasebe"}, headers=muhasebe_headers)
        ).status_code == 200
    else:
        reddedilen = await client.post(
            _owner_path(sahip, sahip.owner_id), json=govde, headers=muhasebe_headers
        )
        assert reddedilen.status_code == 403, reddedilen.text
        # Bağı yetkili (admin) kurar; görme rolü künyesini GÜNCELLEYEMEZ.
        olustur = await client.post(
            _owner_path(sahip, sahip.owner_id), json=govde, headers=admin_headers
        )
        assert olustur.status_code == 201, olustur.text
        link = _link_path(sahip, olustur.json()["id"])
        assert (
            await client.patch(link, json={"note": "muhasebe"}, headers=muhasebe_headers)
        ).status_code == 403

    # DELETE — KARŞIT KANIT: view YETMEZ
    silme = await client.delete(link, headers=muhasebe_headers)
    assert silme.status_code == 403, (
        f"DELETE view rolune ACILMIS: {silme.status_code}. Silme bilincli olarak "
        "`full` kapisindadir (link_router docstring'i)."
    )


async def test_yetkisiz_rol_403_ve_BOLUMDE_boyle_bir_rol_YOK(
    client: AsyncClient,
    yetkisiz_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    """Karşıt kanıt: sahip modülünde `none` olan rol hiçbir uca giremez (403).

    🔴 `sections` için böyle bir rol YOKTUR (`sites` satırında hiçbir rol `_N`
    değil) — bu ölçülmüş bir olgudur ve burada ADIYLA iddia edilir; sessizce
    atlanmaz. Matris değişip `sites`e bir `_N` girerse bu dal kırmızıya döner.
    """
    from tests.documents.conftest import NONE_ROLU

    rol = NONE_ROLU[sahip.spec.key]
    if rol is None:
        assert sahip.spec.permission_module == "sites"
        from app.modules.roles.seed_data import MATRIX

        seviyeler = MATRIX["sites"]
        assert all(s[0] is not AccessLevel.none for s in seviyeler), (
            "`sites` satirina NONE girmis — bolum ucu icin yetkisiz-rol testi "
            "artik KURULABILIR, bu dal gerceklenmelidir."
        )
        return

    belge = await belge_fabrikasi(proje, "a.pdf")
    slot = _ilk_slot(slot_katalogu, sahip)
    assert (
        await client.get(_owner_path(sahip, sahip.owner_id), headers=yetkisiz_headers)
    ).status_code == 403
    assert (
        await client.post(
            _owner_path(sahip, sahip.owner_id),
            json={"type_id": str(slot.id), "document_id": str(belge.id)},
            headers=yetkisiz_headers,
        )
    ).status_code == 403


async def test_patch_exclude_unset_ve_kimlik_alanlari_DEGISMEZ(
    client: AsyncClient, pm_headers, sahip: SahipDurumu, slot_katalogu, proje, belge_fabrikasi
) -> None:
    belge = await belge_fabrikasi(proje, "a.pdf")
    baska_belge = await belge_fabrikasi(proje, "b.pdf")
    slot = _ilk_slot(slot_katalogu, sahip)
    govde = await _bagla(client, pm_headers, sahip, slot, belge, issued_at="2026-08-01", note="ilk")
    link = _link_path(sahip, govde["id"])

    # yalnız valid_until: issued_at ve note DOKUNULMAZ
    r1 = await client.patch(link, json={"valid_until": "2026-12-31"}, headers=pm_headers)
    assert r1.status_code == 200, r1.text
    assert (r1.json()["issued_at"], r1.json()["note"], r1.json()["valid_until"]) == (
        "2026-08-01",
        "ilk",
        "2026-12-31",
    )

    # açık null TEMİZLER
    r2 = await client.patch(link, json={"issued_at": None}, headers=pm_headers)
    assert r2.json()["issued_at"] is None and r2.json()["note"] == "ilk"

    # type_id / document_id gövdede olsa da YOK SAYILIR
    r3 = await client.patch(
        link,
        json={
            "type_id": str(_yabanci_slot(slot_katalogu, sahip).id),
            "document_id": str(baska_belge.id),
        },
        headers=pm_headers,
    )
    assert r3.status_code == 200
    assert r3.json()["type_id"] == str(slot.id)
    assert r3.json()["document_id"] == str(belge.id)

    assert date.fromisoformat(r1.json()["valid_until"]) == date(2026, 12, 31)


async def test_detach_204_dosyayi_SILMEZ_ikinci_silme_404(
    client: AsyncClient,
    seeded_db: AsyncSession,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    belge = await belge_fabrikasi(proje, "a.pdf", data=b"x")
    govde = await _bagla(client, pm_headers, sahip, _ilk_slot(slot_katalogu, sahip), belge)
    link = _link_path(sahip, govde["id"])

    # SIL-B1: bağ kaldırma (DELETE) YALNIZ Sistem Yöneticisi'nindir (`pm` 403 alır).
    assert (await client.delete(link, headers=pm_headers)).status_code == 403
    assert (await client.delete(link, headers=admin_headers)).status_code == 204
    liste = await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)
    assert liste.json() == {"items": []}
    # Arşiv kaydı ayakta (GET detay ucu yok; künye DB'den okunur).
    seeded_db.expunge_all()
    assert await seeded_db.get(Document, belge.id) is not None

    tekrar = await client.delete(link, headers=admin_headers)
    assert tekrar.status_code == 404
    assert tekrar.json()["detail"] == guards.LINK_MISSING


async def test_arsiv_kaydi_silinince_bag_kalir_document_NULL_uctan(
    client: AsyncClient,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    belge = await belge_fabrikasi(proje, "a.pdf", data=b"x")
    govde = await _bagla(
        client, pm_headers, sahip, _ilk_slot(slot_katalogu, sahip), belge, note="n"
    )
    assert (await client.delete(f"/documents/{belge.id}", headers=admin_headers)).status_code == 204

    liste = (await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)).json()[
        "items"
    ]
    assert len(liste) == 1
    assert liste[0]["id"] == govde["id"]
    assert liste[0]["document_id"] is None and liste[0]["document"] is None
    assert liste[0]["note"] == "n"


async def test_gorunmeyen_sahibin_bagi_404_BAGIN_cumlesi(
    client: AsyncClient,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    ikinci_proje: Project,
    belge_fabrikasi,
) -> None:
    """Admin ikinci projede bağ açar; kapsamlı PM o bağı PATCH edemez — 404, cümle BAĞIN
    cümlesi (sahibin varlığı sızmaz). DELETE: PM'e kapı 403 (SIL-B1, yalnız Sistem Yöneticisi)."""
    yabanci_belge = await belge_fabrikasi(ikinci_proje, "y.pdf")
    resp = await client.post(
        _owner_path(sahip, sahip.yabanci_owner_id),
        json={
            "type_id": str(_ilk_slot(slot_katalogu, sahip).id),
            "document_id": str(yabanci_belge.id),
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    link = _link_path(sahip, resp.json()["id"])
    yanit = await client.patch(link, json={"note": "x"}, headers=pm_headers)
    assert yanit.status_code == 404
    assert yanit.json()["detail"] == guards.LINK_MISSING
    silme = await client.delete(link, headers=pm_headers)
    assert silme.status_code == 403
    # Pozitif kontrol: admin aynı bağı güncelleyebilir.
    assert (await client.patch(link, json={"note": "x"}, headers=admin_headers)).status_code == 200


async def test_liste_YALNIZ_o_sahibin_baglarini_dondurur(
    client: AsyncClient,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje: Project,
    ikinci_proje: Project,
    belge_fabrikasi,
) -> None:
    """🔴 `list_links`in SAHİP SÜZGECİ bekçisi (PR #116 incelemesi: süzgeç
    kaldırılınca dört test dosyası da yeşil kalıyordu — BC-3 bu IDOR yüzeyini
    DÖRDE katlıyor).

    Admin İKİNCİ projedeki sahibe bir bağ açar; sonra BİRİNCİ sahibin listesi
    çekilir ve o bağın **görünmediği** iddia edilir. Süzgeç silinirse liste
    başka sahibin (ve başka projenin) bağını sızdırır ve test kırmızıya döner.
    """
    slot = _ilk_slot(slot_katalogu, sahip)

    # (a) BAŞKA sahibe (ikinci proje) ait bağ — admin açar.
    yabanci_belge = await belge_fabrikasi(ikinci_proje, "yabanci.pdf")
    yabanci = await client.post(
        _owner_path(sahip, sahip.yabanci_owner_id),
        json={"type_id": str(slot.id), "document_id": str(yabanci_belge.id)},
        headers=admin_headers,
    )
    assert yabanci.status_code == 201, yabanci.text

    # (b) BU sahibe ait bağ.
    benim = await _bagla(client, pm_headers, sahip, slot, await belge_fabrikasi(proje, "benim.pdf"))

    # (c) Bu sahibin listesi YALNIZ kendi bağını taşır.
    liste = (await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)).json()
    kimlikler = {i["id"] for i in liste["items"]}
    assert kimlikler == {benim["id"]}, "liste başka sahibin bağını sızdırdı"
    assert yabanci.json()["id"] not in kimlikler
    assert {i["owner_id"] for i in liste["items"]} == {str(sahip.owner_id)}

    # (d) Pozitif kontrol: yabancı bağ GERÇEKTEN var — admin onu kendi
    #     sahibinin listesinde görür (yani (c) bir "hiçbir şey yok" testi değil).
    oteki = (
        await client.get(_owner_path(sahip, sahip.yabanci_owner_id), headers=admin_headers)
    ).json()
    assert {i["id"] for i in oteki["items"]} == {yabanci.json()["id"]}


async def test_bos_govdeli_PATCH_denetim_satiri_YAZMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    pm_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    """`PATCH {}` 200 döner ama hiçbir alan değişmez → denetim satırı YAZILMAZ.
    Yazsaydı günlük no-op "güncellendi" satırlarıyla dolar, gerçek değişikliğin
    izi kaybolurdu. Pozitif kontrol: dolu gövde satırı YAZAR."""
    belge = await belge_fabrikasi(proje, "a.pdf")
    govde = await _bagla(
        client, pm_headers, sahip, _ilk_slot(slot_katalogu, sahip), belge, note="ilk"
    )
    link = _link_path(sahip, govde["id"])

    async def _sayi() -> int:
        return len((await seeded_db.execute(select(AuditLog.detail))).scalars().all())

    onceki = await _sayi()
    bos = await client.patch(link, json={}, headers=pm_headers)
    assert bos.status_code == 200, bos.text
    assert bos.json()["note"] == "ilk", "boş gövde hiçbir alanı değiştirmemeli"
    assert await _sayi() == onceki, "boş PATCH denetim satırı yazdı"

    dolu = await client.patch(link, json={"note": "ikinci"}, headers=pm_headers)
    assert dolu.status_code == 200
    assert await _sayi() == onceki + 1, "dolu PATCH denetim satırı YAZMALI"


async def test_ayni_slota_ikinci_belge_IZINLI_unique_yok(
    client: AsyncClient, pm_headers, sahip: SahipDurumu, slot_katalogu, proje, belge_fabrikasi
) -> None:
    """`UNIQUE(sahip, type_id)` YOK: "Görseller / Render" slotu çoğuldur. Sıra iddiası
    edilmez — test tek transaction'da koşar ve `now()` sabittir, `created_at` iki
    satırı ayıramaz (canlıda ayrı isteklerde ayırır); kimlik kırılımı deterministiktir."""
    slot = _ilk_slot(slot_katalogu, sahip)
    a = await _bagla(client, pm_headers, sahip, slot, await belge_fabrikasi(proje, "1.jpg"))
    b = await _bagla(client, pm_headers, sahip, slot, await belge_fabrikasi(proje, "2.jpg"))
    liste = (await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)).json()
    assert {i["id"] for i in liste["items"]} == {a["id"], b["id"]}
    assert {i["type_code"] for i in liste["items"]} == {slot.code}


async def test_yazmalar_denetim_satiri_uretir_okuma_URETMEZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    pm_headers,
    admin_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje,
    belge_fabrikasi,
) -> None:
    slot = _ilk_slot(slot_katalogu, sahip)
    belge = await belge_fabrikasi(proje, "a.pdf")

    async def _kimlikler() -> set:
        return set((await seeded_db.execute(select(AuditLog.id))).scalars().all())

    onceki = await _kimlikler()
    await client.get(_owner_path(sahip, sahip.owner_id), headers=pm_headers)
    assert await _kimlikler() == onceki, "GET denetlenmez"

    govde = await _bagla(client, pm_headers, sahip, slot, belge)
    await client.patch(_link_path(sahip, govde["id"]), json={"note": "x"}, headers=pm_headers)
    await client.delete(_link_path(sahip, govde["id"]), headers=admin_headers)
    # FIX-B1: `[onceki:]` dilimi yeni satırların SONDA geldiğini varsayıyordu (ORDER BY yok).
    detaylar = [
        k.detail
        for k in (await seeded_db.execute(select(AuditLog))).scalars().all()
        if k.id not in onceki
    ]
    assert len(detaylar) == 3
    assert all(slot.name in d and sahip.spec.label in d for d in detaylar), detaylar


async def test_project_id_govdeden_ALINMAZ_sahipten_turer(
    client: AsyncClient,
    pm_headers,
    sahip: SahipDurumu,
    slot_katalogu,
    proje: Project,
    ikinci_proje: Project,
    belge_fabrikasi,
) -> None:
    """Gövdeye `project_id` (ikinci proje) yazılsa bile yok sayılır: belge sahibin
    projesindeyse bağ kurulur, değilse 422 — karar gövdeden değil sahipten gelir."""
    slot = _ilk_slot(slot_katalogu, sahip)
    dogru = await belge_fabrikasi(proje, "d.pdf")
    resp = await client.post(
        _owner_path(sahip, sahip.owner_id),
        json={
            "type_id": str(slot.id),
            "document_id": str(dogru.id),
            "project_id": str(ikinci_proje.id),
        },
        headers=pm_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["project_id"] == str(proje.id)


async def test_bolumun_projesi_santiye_uzerinden_turer(
    client: AsyncClient,
    seeded_db: AsyncSession,
    pm_headers,
    slot_katalogu,
    bolum,
    proje: Project,
    belge_fabrikasi,
) -> None:
    """Zincirin en uzunu (bölüm→şantiye→proje) ayrıca ölçülür: bölümün şantiyesi
    `proje`dedir, belge de — bağ kurulur; site_id bağda taşınmaz, künyeden okunur."""
    site = await seeded_db.get(Site, bolum.site_id)
    assert site.project_id == proje.id
    belge = await belge_fabrikasi(proje, "b.pdf", site=site)
    slot = slot_katalogu[("section", "application_project")]
    resp = await client.post(
        f"/sections/{bolum.id}/documents",
        json={"type_id": str(slot.id), "document_id": str(belge.id)},
        headers=pm_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["site_id"] == str(site.id)
