"""IZN-B5b grup A — yazma kapıları SAYFA/SEKME BAŞINA (madde 1, 2, Ek/6, 4, 11 + belge bağlama).

Dünya: yalnız TEK hücresi açık özel rol (diğer tüm sayfalar `none`). Eskiden kapı modül
düzeyindeydi (`projects:full`, `accounting:full`, `equipment:full`, `<sahip>:view`): aynı
modülün BAŞKA sayfasının Düzenler'i ucu açardı. Şimdi yalnız ucun kendi sayfasının Düzenler'i açar.

Kapı sırası: bağımlılıklar gövde doğrulamasından ÖNCE çalışır → red `403`; geçiş `403` DEĞİL
(boş gövde/uydurma kimlik 404/422 verir — kapı geçti demektir).
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from sqlalchemy import update

from app.core.sayfalar import PageLevel
from app.modules.roles.models import RolePagePermission
from tests._ekip_dunyasi import rol_kur

PAROLA = "parola1234"
_SAYAC = itertools.count()
PID = uuid.uuid4()
UID = uuid.uuid4()

# (metot, yol şablonu, hedef sayfa, komşu sayfa — eskiden aynı modül kapısı yüzünden açardı)
_SATIS = {
    "blok": "mali.satis_blok",
    "unite": "mali.satis_unite",
    "toplu": "mali.satis_toplu_uretim",
    "excel": "mali.satis_excel",
    "paylasim": "mali.satis_paylasim",
}


def _komsu(sayfa: str, havuz: list[str]) -> str:
    return next(p for p in havuz if p != sayfa)


SATIS_HAVUZ = list(_SATIS.values())
MUHASEBE_HAVUZ = ["mali.hesap_plani", "mali.yevmiye", "mali.donem_kapanisi"]
MAKINE_HAVUZ = [
    "saha.makine_ekipman",
    "saha.makine_calisma",
    "saha.makine_yakit",
    "saha.makine_kira",
]

YAZMALAR: list[tuple[str, str, str, list[str]]] = [
    ("PATCH", "/projects/{pid}", "genel.projeler", ["mali.satis_blok"]),
    # madde 2
    ("POST", "/projects/{pid}/blocks", "mali.satis_blok", SATIS_HAVUZ),
    ("PATCH", "/blocks/{uid}", "mali.satis_blok", SATIS_HAVUZ),
    ("POST", "/projects/{pid}/units", "mali.satis_unite", SATIS_HAVUZ),
    ("PATCH", "/units/{uid}", "mali.satis_unite", SATIS_HAVUZ),
    ("POST", "/projects/{pid}/units/bulk", "mali.satis_toplu_uretim", SATIS_HAVUZ),
    ("POST", "/projects/{pid}/units/bulk/preview", "mali.satis_toplu_uretim", SATIS_HAVUZ),
    ("POST", "/projects/{pid}/units/import/validate", "mali.satis_excel", SATIS_HAVUZ),
    ("POST", "/projects/{pid}/units/import", "mali.satis_excel", SATIS_HAVUZ),
    ("PATCH", "/projects/{pid}/units/allocation", "mali.satis_paylasim", SATIS_HAVUZ),
    # madde 4
    ("POST", "/chart-of-accounts", "mali.hesap_plani", MUHASEBE_HAVUZ),
    ("PATCH", "/chart-of-accounts/{uid}", "mali.hesap_plani", MUHASEBE_HAVUZ),
    ("POST", "/journal-entries", "mali.yevmiye", MUHASEBE_HAVUZ),
    ("PATCH", "/journal-entries/{uid}", "mali.yevmiye", MUHASEBE_HAVUZ),
    ("PUT", "/journal-entries/{uid}/lines", "mali.yevmiye", MUHASEBE_HAVUZ),
    ("POST", "/journal-entries/{uid}/reverse", "mali.yevmiye", MUHASEBE_HAVUZ),
    ("POST", "/accounting-periods/2026/1/close", "mali.donem_kapanisi", MUHASEBE_HAVUZ),
    # madde 11
    ("POST", "/equipment", "saha.makine_ekipman", MAKINE_HAVUZ),
    ("PATCH", "/equipment/{uid}", "saha.makine_ekipman", MAKINE_HAVUZ),
    ("POST", "/equipment/{uid}/documents", "saha.makine_ekipman", MAKINE_HAVUZ),
    ("PATCH", "/equipment/documents/{uid}", "saha.makine_ekipman", MAKINE_HAVUZ),
    ("POST", "/equipment/work-logs", "saha.makine_calisma", MAKINE_HAVUZ),
    ("PATCH", "/equipment/work-logs/{uid}", "saha.makine_calisma", MAKINE_HAVUZ),
    ("POST", "/equipment/fuel-logs", "saha.makine_yakit", MAKINE_HAVUZ),
    ("PATCH", "/equipment/fuel-logs/{uid}", "saha.makine_yakit", MAKINE_HAVUZ),
    ("POST", "/equipment/rental-invoices", "saha.makine_kira", MAKINE_HAVUZ),
    ("PATCH", "/equipment/rental-invoices/{uid}", "saha.makine_kira", MAKINE_HAVUZ),
    ("POST", "/equipment/rental-invoices/{uid}/reload", "saha.makine_kira", MAKINE_HAVUZ),
    ("PATCH", "/equipment/rental-invoice-lines/{uid}", "saha.makine_kira", MAKINE_HAVUZ),
    # yan bulgu: belge bağlama yazmaları = sahibin ana sayfasının Düzenler'i
    ("POST", "/units/{uid}/documents", "mali.satis_unite", ["mali.satis_blok"]),
    ("PATCH", "/units/documents/{uid}", "mali.satis_unite", ["mali.satis_blok"]),
    ("POST", "/sales/{uid}/documents", "mali.satis", ["mali.satis_blok"]),
    ("PATCH", "/sales/documents/{uid}", "mali.satis", ["mali.satis_blok"]),
    ("POST", "/subcontractor-contracts/{uid}/documents", "teklif.taseron_sozlesme",
     ["teklif.sozlesmeler"]),
    ("PATCH", "/subcontractor-contracts/documents/{uid}", "teklif.taseron_sozlesme",
     ["teklif.sozlesmeler"]),
]  # fmt: skip


async def _giris(seeded_db, client, user_factory, hucreler: dict[str, PageLevel]):
    """Yalnız `hucreler` açık özel rol + kullanıcı + oturum başlığı."""
    n = next(_SAYAC)
    rol = await rol_kur(seeded_db, f"b5b_a_{n}", PageLevel.none)
    for sayfa, seviye in hucreler.items():
        res = await seeded_db.execute(
            update(RolePagePermission)
            .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key == sayfa)
            .values(level=seviye)
        )
        assert res.rowcount == 1, f"katalogda sayfa yok: {sayfa}"
    email = f"b5b_a_{n}@izn.co"
    user = await user_factory(email=email, password=PAROLA, role_key=f"b5b_a_{n}")
    user.role_id = rol.id
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": PAROLA})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _cagir(client, headers, metot: str, yol: str):
    url = yol.format(pid=PID, uid=UID)
    return await client.request(metot, url, json={}, headers=headers)


def _kimlik(satir) -> str:
    return f"{satir[0]} {satir[1]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("satir", YAZMALAR, ids=_kimlik)
async def test_komsu_sayfanin_duzenleri_ucu_acmaz(seeded_db, client, user_factory, satir):
    metot, yol, hedef, havuz = satir
    komsu = _komsu(hedef, havuz)
    basliklar = await _giris(seeded_db, client, user_factory, {komsu: PageLevel.edit})
    resp = await _cagir(client, basliklar, metot, yol)
    assert resp.status_code == 403, f"{komsu} Düzenler'i {metot} {yol}'u açtı: {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("satir", YAZMALAR, ids=_kimlik)
async def test_hedef_sayfanin_gorur_duzeyi_yetmez(seeded_db, client, user_factory, satir):
    metot, yol, hedef, _ = satir
    basliklar = await _giris(seeded_db, client, user_factory, {hedef: PageLevel.view})
    resp = await _cagir(client, basliklar, metot, yol)
    assert resp.status_code == 403, f"{hedef} Görür'ü {metot} {yol}'u açtı: {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("satir", YAZMALAR, ids=_kimlik)
async def test_hedef_sayfanin_duzenleri_ucu_acar(seeded_db, client, user_factory, satir):
    metot, yol, hedef, _ = satir
    basliklar = await _giris(seeded_db, client, user_factory, {hedef: PageLevel.edit})
    resp = await _cagir(client, basliklar, metot, yol)
    assert resp.status_code != 403, f"{hedef} Düzenler'i {metot} {yol}'u açmadı: {resp.text}"


# ---- Ek/6: GET /projects/{id}/costs — satış alt sayfalarından ayrıldı ----
COSTS_SAYFALARI = [
    "genel.projeler",
    "genel.proje_takvimi",
    "proje.ozet",
    "proje.paylasim_tablosu",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sayfa", SATIS_HAVUZ)
async def test_costs_satis_alt_sayfasi_goruru_acmaz(seeded_db, client, user_factory, sayfa):
    basliklar = await _giris(seeded_db, client, user_factory, {sayfa: PageLevel.view})
    resp = await client.get(f"/projects/{PID}/costs", headers=basliklar)
    assert resp.status_code == 403, f"{sayfa} Görür'ü /costs'u açtı: {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("sayfa", COSTS_SAYFALARI)
async def test_costs_proje_sayfalarinin_goruru_acar(seeded_db, client, user_factory, sayfa):
    basliklar = await _giris(seeded_db, client, user_factory, {sayfa: PageLevel.view})
    resp = await client.get(f"/projects/{PID}/costs", headers=basliklar)
    assert resp.status_code != 403, f"{sayfa} Görür'ü /costs'u açmadı: {resp.text}"


@pytest.mark.asyncio
async def test_patron_ve_pm_proje_guncellemeyi_kaybeder_diger_okumalar_surer(
    seeded_db, client, user_factory
):
    """Madde 1 (CEO A): patron/PM seed'de genel.projeler Görür + satış sekmeleri Düzenler →
    PATCH /projects/{id} artık 403 (bilinçli daralma); /costs hâlâ açık."""
    from sqlalchemy import select

    from app.modules.roles.models import Role

    for anahtar in ("patron", "project_manager"):
        rol = (await seeded_db.execute(select(Role).where(Role.key == anahtar))).scalar_one()
        email = f"b5b_a_{anahtar}@izn.co"
        user = await user_factory(email=email, password=PAROLA, role_key=anahtar)
        user.role_id = rol.id
        await seeded_db.flush()
        resp = await client.post("/auth/login", json={"email": email, "password": PAROLA})
        basliklar = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        patch = await client.patch(f"/projects/{PID}", json={}, headers=basliklar)
        assert patch.status_code == 403, f"{anahtar}: PATCH {patch.status_code}"
        costs = await client.get(f"/projects/{PID}/costs", headers=basliklar)
        assert costs.status_code != 403, f"{anahtar}: /costs {costs.status_code}"
