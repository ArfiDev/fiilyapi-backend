"""IZN-B5b (ajan B) — hakediş (madde 5/6) + planlama (madde 9/10) sayfa ayırma.

Her uç için iki iddia: (i) eski KOMŞU sayfanın Düzenler'i (ya da Onaylar'ı) ucu artık AÇMAZ
(403), (ii) doğru sayfanın hücresi açar (403 DEĞİL: sahte kimlikle 404/422 beklenir; kapı
gövde/varlık denetiminden ÖNCE koşar). Tek hücreli özel roller `modul_sayfa_hucreleri` DEĞİL,
yalnız adı geçen sayfaya hücre yazar.

CEO kararı 3 (proje rolü farkı): kümeden proje içi sayfa düşen uçlarda (EV ayar PUT) ekip üyesi
`ayarlar.planlama` hücresiyle, o projedeki ROLÜYLE değerlendirilir — `test_ev_ayar_ekip_uyesi_*`.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select, update

from app.core.sayfalar import PageLevel
from app.modules.earned_value import diary_adapter
from app.modules.roles.models import Role, RolePagePermission
from app.modules.users.models import User
from tests._ekip_dunyasi import proje_kur, rol_kur
from tests._proje_ekibi import ekibe_ekle

pytestmark = pytest.mark.asyncio

SIFRE = "parola1234"
HEPSI_YOK = uuid.UUID(int=0xB5B)

HI = ("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler")
HT = ("mali.hakedis_taseron", "proje.taseron_hakedis")
EVB = ("planlama.adam_saat_butcesi", "santiye.adam_saat_butcesi")


async def _tek_hucreli_rol(session, key: str, sayfa: str, *, onay: bool = False) -> Role:
    """Yalnız `sayfa`da Düzenler (+ istenirse Onaylar) hücresi olan özel rol."""
    rol = await rol_kur(session, key, PageLevel.none)
    await session.execute(
        update(RolePagePermission)
        .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key == sayfa)
        .values(level=PageLevel.edit, can_approve=onay)
    )
    await session.flush()
    return rol


async def _baslik(client, session, user_factory, key: str) -> dict[str, str]:
    email = f"{key}@b5b-b.co"
    if (await session.execute(select(User.id).where(User.email == email))).first() is None:
        await user_factory(email=email, password=SIFRE, role_key=key)
    yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert yanit.status_code == 200, yanit.text
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _cagir(client, seeded_db, user_factory, sayfa, metot, yol, *, onay=False):
    key = "r_" + sayfa.replace(".", "_") + ("_o" if onay else "")
    if (await seeded_db.execute(select(Role.id).where(Role.key == key))).first() is None:
        await _tek_hucreli_rol(seeded_db, key, sayfa, onay=onay)
    basliklar = await _baslik(client, seeded_db, user_factory, key)
    yol = yol.format(i=HEPSI_YOK)
    return await client.request(metot, yol, headers=basliklar, json={})


# (metot, yol) — YAZMA uçları
HI_YAZMA = [
    ("POST", "/projects/{i}/progress-payments"),
    ("PATCH", "/progress-payments/{i}"),
    ("PUT", "/progress-payments/{i}/lines"),
    ("POST", "/progress-payments/{i}/refresh-prices"),
    ("POST", "/progress-payments/{i}/submit"),
]
HT_YAZMA = [
    ("POST", "/subcontractor-contracts/{i}/progress-payments"),
    ("PATCH", "/subcontractor-progress-payments/{i}"),
    ("PUT", "/subcontractor-progress-payments/{i}/lines"),
    ("POST", "/subcontractor-progress-payments/{i}/refresh-prices"),
    ("POST", "/subcontractor-progress-payments/{i}/submit"),
]
HT_ONAY = [
    ("POST", "/subcontractor-progress-payments/{i}/approve"),
    ("POST", "/subcontractor-progress-payments/{i}/reject"),
    ("POST", "/subcontractor-progress-payments/{i}/mark-paid"),
]
EV_BUTCE = [
    ("POST", "/sites/{i}/earned-value/budget/revisions"),
    ("PUT", "/sites/{i}/earned-value/budget/group-disciplines"),
    ("PATCH", "/sites/{i}/earned-value/budget/items/{i}"),
    ("PATCH", "/sites/{i}/earned-value/budget/leaves"),
    ("POST", "/sites/{i}/earned-value/budget/fill-from-catalog"),
    ("POST", "/sites/{i}/earned-value/budget/fill-from-contract"),
    ("PUT", "/sites/{i}/earned-value/budget/distributions"),
    ("PUT", "/sites/{i}/earned-value/budget/windows"),
    ("PUT", "/sites/{i}/earned-value/days/2026-05-05/allocation"),
]
EV_DISIPLIN = [
    ("POST", "/earned-value/disciplines"),
    ("PATCH", "/earned-value/disciplines/{i}"),
]
EV_KATALOG = [
    ("POST", "/earned-value/catalog"),
    ("PATCH", "/earned-value/catalog/{i}"),
    ("POST", "/earned-value/catalog/{i}/adopt-actual"),
]


TUM_UCLAR = [*HI_YAZMA, *HT_YAZMA, *HT_ONAY, *EV_BUTCE, *EV_DISIPLIN, *EV_KATALOG]


@pytest.mark.parametrize("metot,yol", TUM_UCLAR)
async def test_tablodaki_her_uc_gercek_bir_rotadir(metot, yol):
    """Sahte-yeşil bekçisi: yanlış yol 404 döner ve `!= 403` pozitif iddiasını boşa geçirirdi."""
    import re

    from fastapi.routing import iter_route_contexts

    from app.main import app

    gercek = yol.format(i=HEPSI_YOK)
    bulundu = any(
        metot in (baglam.methods or ())
        and re.fullmatch(re.sub(r"{[^}]+}", "[^/]+", baglam.path), gercek)
        for baglam in iter_route_contexts(app.routes)
    )
    assert bulundu, (metot, yol)


def _uc(satirlar):
    return pytest.mark.parametrize("metot,yol", satirlar)


# --- Madde 5 ------------------------------------------------------------------------------------


@_uc(HI_YAZMA)
@pytest.mark.parametrize("komsu", [*HT, "mali.satis_unite"])
async def test_isveren_hakedis_yazma_komsu_sayfayi_ACMAZ(
    client, seeded_db, user_factory, metot, yol, komsu
):
    yanit = await _cagir(client, seeded_db, user_factory, komsu, metot, yol)
    assert yanit.status_code == 403, yanit.text


@_uc(HI_YAZMA)
@pytest.mark.parametrize("sayfa", HI)
async def test_isveren_hakedis_yazma_HI_sayfalari_acar(
    client, seeded_db, user_factory, metot, yol, sayfa
):
    yanit = await _cagir(client, seeded_db, user_factory, sayfa, metot, yol)
    assert yanit.status_code != 403, yanit.text


@_uc(HT_YAZMA)
@pytest.mark.parametrize("komsu", HI)
async def test_taseron_hakedis_yazma_isveren_ailesini_ACMAZ(
    client, seeded_db, user_factory, metot, yol, komsu
):
    """`santiye.hakedisler` (CEO kararı 2) dahil: şantiye hakedişi yalnız işveren ailesidir."""
    yanit = await _cagir(client, seeded_db, user_factory, komsu, metot, yol)
    assert yanit.status_code == 403, yanit.text


@_uc(HT_YAZMA)
@pytest.mark.parametrize("sayfa", HT)
async def test_taseron_hakedis_yazma_HT_sayfalari_acar(
    client, seeded_db, user_factory, metot, yol, sayfa
):
    yanit = await _cagir(client, seeded_db, user_factory, sayfa, metot, yol)
    assert yanit.status_code != 403, yanit.text


# --- Madde 6 ------------------------------------------------------------------------------------


@_uc(HT_ONAY)
async def test_santiye_hakedisler_onaylar_taseron_onayini_ACMAZ(
    client, seeded_db, user_factory, metot, yol
):
    yanit = await _cagir(
        client, seeded_db, user_factory, "santiye.hakedisler", metot, yol, onay=True
    )
    assert yanit.status_code == 403, yanit.text


@_uc(HT_ONAY)
@pytest.mark.parametrize("sayfa", HT)
async def test_taseron_onay_HT_onaylar_acar(client, seeded_db, user_factory, metot, yol, sayfa):
    yanit = await _cagir(client, seeded_db, user_factory, sayfa, metot, yol, onay=True)
    assert yanit.status_code != 403, yanit.text


async def test_isveren_onay_santiye_hakedisler_acik_KALIR(client, seeded_db, user_factory):
    """Değişmez: işveren ailesinde santiye.hakedisler Onaylar'ı onay/ret/ödendi'yi açar."""
    for yol in ("approve", "reject", "mark-paid"):
        yanit = await _cagir(
            client,
            seeded_db,
            user_factory,
            "santiye.hakedisler",
            "POST",
            f"/progress-payments/{{i}}/{yol}",
            onay=True,
        )
        assert yanit.status_code != 403, (yol, yanit.text)


# --- Madde 9 ------------------------------------------------------------------------------------


@_uc(EV_BUTCE)
async def test_ayarlar_planlama_butce_uclarini_ACMAZ(client, seeded_db, user_factory, metot, yol):
    yanit = await _cagir(client, seeded_db, user_factory, "ayarlar.planlama", metot, yol)
    assert yanit.status_code == 403, yanit.text


@_uc(EV_BUTCE)
@pytest.mark.parametrize("sayfa", EVB)
async def test_butce_sayfalari_butce_uclarini_acar(
    client, seeded_db, user_factory, metot, yol, sayfa
):
    yanit = await _cagir(client, seeded_db, user_factory, sayfa, metot, yol)
    assert yanit.status_code != 403, yanit.text


@pytest.mark.parametrize("komsu", [*EVB, "planlama.gunluk_rapor"])
async def test_ev_ayar_PUT_butce_sayfalarini_ACMAZ(client, seeded_db, user_factory, komsu):
    yanit = await _cagir(
        client, seeded_db, user_factory, komsu, "PUT", "/sites/{i}/earned-value/settings"
    )
    assert yanit.status_code == 403, yanit.text


async def test_ev_ayar_PUT_ayarlar_planlama_acar(client, seeded_db, user_factory):
    yanit = await _cagir(
        client,
        seeded_db,
        user_factory,
        "ayarlar.planlama",
        "PUT",
        "/sites/{i}/earned-value/settings",
    )
    assert yanit.status_code != 403, yanit.text


async def test_ev_ayar_ekip_uyesi_ayarlar_planlama_hucresiyle_degerlendirilir(
    client, seeded_db, user_factory, project_factory
):
    """CEO kararı 3: kümede proje içi sayfa YOK → ekip üyesi o projedeki rolünün
    `ayarlar.planlama` hücresiyle karar alır. Ekip rolü yalnız `santiye.adam_saat_butcesi`
    (proje içi sayfa) ise ayar PUT'u artık 403; ekip rolü `ayarlar.planlama` ise açılır."""
    ana = await rol_kur(seeded_db, "ana_bos", PageLevel.none)
    butce = await _tek_hucreli_rol(seeded_db, "ekip_butce", "santiye.adam_saat_butcesi")
    ayar = await _tek_hucreli_rol(seeded_db, "ekip_ayar", "ayarlar.planlama")
    kisi = await user_factory(email=f"{ana.key}@b5b-b.co", password=SIFRE, role_key=ana.key)
    admin = await user_factory(email="olusturan@b5b-b.co", password=SIFRE, role_key="system_admin")
    dunya = await proje_kur(seeded_db, project_factory, "B5BB", "B5b", admin.id)
    yol = f"/sites/{dunya.site.id}/earned-value/settings"
    basliklar = await _baslik(client, seeded_db, user_factory, ana.key)

    await ekibe_ekle(seeded_db, kisi, dunya.project.id, butce.id)
    assert (await client.put(yol, headers=basliklar, json={})).status_code == 403
    await ekibe_ekle(seeded_db, kisi, dunya.project.id, ayar.id)
    assert (await client.put(yol, headers=basliklar, json={})).status_code != 403


async def test_servis_ici_gonder_kapisi_butce_sayfasiyla_olculur(
    seeded_db, user_factory, project_factory, monkeypatch
):
    """`diary_adapter.submit_blockers`: planlama yazma yetkisi = EVB Düzenler (ayarlar.planlama
    değil)."""
    from app.core.day_hooks import SubmitContext

    async def _agac(session, site_id):
        return object()

    monkeypatch.setattr(diary_adapter, "active_tree", _agac)
    admin = await user_factory(email="a@b5b-b.co", password=SIFRE, role_key="system_admin")
    dunya = await proje_kur(seeded_db, project_factory, "B5BS", "B5bs", admin.id)
    sonuc = {}
    for sayfa in ("ayarlar.planlama", *EVB):
        rol = await _tek_hucreli_rol(seeded_db, "s_" + sayfa.replace(".", "_"), sayfa)
        kisi = await user_factory(email=f"{rol.key}@b5b-b.co", password=SIFRE, role_key=rol.key)
        ctx = SubmitContext(
            entry_id=uuid.uuid4(),
            site_id=dunya.site.id,
            entry_date=date(2026, 5, 5),
            actor_id=kisi.id,
        )
        nedenler = await diary_adapter.submit_blockers(seeded_db, ctx)
        sonuc[sayfa] = diary_adapter.SUBMIT_NO_PERMISSION in [n.code for n in nedenler]
    assert sonuc == {
        "ayarlar.planlama": True,
        "planlama.adam_saat_butcesi": False,
        "santiye.adam_saat_butcesi": False,
    }


# --- Madde 10 -----------------------------------------------------------------------------------


@_uc(EV_DISIPLIN)
async def test_birim_oran_katalogu_disiplin_yazmayi_ACMAZ(
    client, seeded_db, user_factory, metot, yol
):
    yanit = await _cagir(
        client, seeded_db, user_factory, "planlama.birim_oran_katalogu", metot, yol
    )
    assert yanit.status_code == 403, yanit.text


@_uc(EV_DISIPLIN)
async def test_disiplin_yonetimi_disiplin_yazmayi_acar(client, seeded_db, user_factory, metot, yol):
    yanit = await _cagir(client, seeded_db, user_factory, "planlama.disiplin_yonetimi", metot, yol)
    assert yanit.status_code != 403, yanit.text


@_uc(EV_KATALOG)
async def test_disiplin_yonetimi_katalog_yazmayi_ACMAZ(client, seeded_db, user_factory, metot, yol):
    yanit = await _cagir(client, seeded_db, user_factory, "planlama.disiplin_yonetimi", metot, yol)
    assert yanit.status_code == 403, yanit.text


@_uc(EV_KATALOG)
async def test_birim_oran_katalogu_katalog_yazmayi_acar(
    client, seeded_db, user_factory, metot, yol
):
    yanit = await _cagir(
        client, seeded_db, user_factory, "planlama.birim_oran_katalogu", metot, yol
    )
    assert yanit.status_code != 403, yanit.text
