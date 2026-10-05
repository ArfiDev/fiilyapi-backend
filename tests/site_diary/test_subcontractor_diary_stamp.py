"""TB4 T1 (B1/SD-2) — TAŞERON hakediş satırının sunucu-tarafı `diary` damgası.

İşveren ikizi `test_employer_diary_stamp.py` ile AYNI kural; İKİ FARK bilinçlidir:

* köprü İKİ ADIMLIDIR (`boq_items.contract_item_id` → `subcontractor_contract_items.
  source_contract_item_id`) ve şantiye süzgeci SÖZLEŞMEDEN gelir,
* `site_id` NULL olan proje-geneli sözleşmede köprü PROJENİN TÜM ŞANTİYELERİNİN
  günlüğüdür (kullanıcı kararı 2026-08-27, spec §7 S5 TERSİNE ÇEVRİLDİ). Eskiden
  o satırlar HER ZAMAN `manual` damgalanırdı.

Damga taşeron yanıt şemasında zaten VARDIR (`quantity_source`), bu yüzden
doğrulama uçtan uca yanıttan okunur.
"""

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.modules.approvals.conftest import rol_sahipleri_dolgusu  # noqa: F401  (fixture)

from ._suggestion import (
    HAKEDIS_DONEM as DONEM,
)
from ._suggestion import (
    _damgalar,
    _hakedis,
    _kalem_id,
    _kaydet,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("rol_sahipleri_dolgusu")]


@pytest.fixture
async def kurulum(santiye, sozlesme_kalemi_fabrikasi, taseron_sozlesmesi_fabrikasi):
    """Şantiye + iki köprülü poz + iki kalemli taşeron sözleşmesi."""
    site, project, items = santiye
    kalem_a, kalem_b = sorted(items, key=lambda i: i.code)
    sozlesme_a = await sozlesme_kalemi_fabrikasi(kalem_a, project)
    sozlesme_b = await sozlesme_kalemi_fabrikasi(kalem_b, project)
    contract = await taseron_sozlesmesi_fabrikasi(
        project, site=site, kalemler=[("TK-1", sozlesme_a), ("TK-2", sozlesme_b)]
    )
    return site, project, (kalem_a, kalem_b), contract


# --- 1) Günlük toplamı = satır miktarı → diary ---


async def test_gunluk_toplamiyla_BIREBIR_esit_satir_diary_damgalanir(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "10"}],
    )
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 12),
        [{"boq_item_id": str(kalem_a.id), "quantity": "2.5"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12.5"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "diary"}


# --- 2) Farklı miktar (kısmi ve fazla) → manual ---


@pytest.mark.parametrize("miktar", ["6", "20"])
async def test_gunluk_toplamindan_FARKLI_miktar_manual_kalir(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api, miktar: str
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": miktar}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


# --- 3) Taslak günlük SAYILMAZ ---


async def test_yalniz_TASLAK_gunluk_varsa_miktar_esitse_bile_manual(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
        gonder=False,
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


# --- 4) Köprüsüz satır → manual ---


async def test_gunlugu_olmayan_kalem_manual_kalir(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    """Damga SATIR bazındadır: aynı gövdede biri `diary`, diğeri `manual`."""
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")
    tk2 = await _kalem_id(seeded_db, contract.id, "TK-2")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client,
        admin_headers,
        hakedis["id"],
        [
            {"contract_item_id": str(tk1), "quantity": "12"},
            {"contract_item_id": str(tk2), "quantity": "4"},
        ],
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "diary", str(tk2): "manual"}


async def test_KOPRUSUZ_taseron_kalemi_manual_kalir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    santiye,
    sozlesme_kalemi_fabrikasi,
    taseron_sozlesmesi_fabrikasi,
    gunluk_api,
) -> None:
    """`source_contract_item_id` boş kalem hangi poza karşılık geldiğini
    BİLMEZ — günlükte aynı miktar dursa bile damgalanamaz."""
    site, project, items = santiye
    kalem_a = sorted(items, key=lambda i: i.code)[0]
    await sozlesme_kalemi_fabrikasi(kalem_a, project)
    contract = await taseron_sozlesmesi_fabrikasi(
        project, site=site, kalemler=[("TK-X", None)], code="TS-KOPRUSUZ"
    )
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tkx = await _kalem_id(seeded_db, contract.id, "TK-X")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tkx), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tkx): "manual"}


async def test_SANTIYESIZ_sozlesmede_PROJE_TOPLAMI_diary_damgalanir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    proje,
    santiye,
    santiye_fabrikasi,
    sozlesme_kalemi_fabrikasi,
    taseron_sozlesmesi_fabrikasi,
    gunluk_api,
) -> None:
    """Spec §7 S5 TERSİNE ÇEVRİLDİ, kullanıcı kararı 2026-08-27.

    ESKİ İDDİA (yerine geçtiği test `test_SANTIYESIZ_sozlesmede_damga_BASILMAZ`):
    proje-geneli sözleşmede hangi şantiyenin günlüğüne bakılacağı belirsizdir →
    köprü yok, damga HER ZAMAN `manual`.

    YENİ İDDİA: proje-geneli sözleşmenin doğru cevabı PROJENİN TÜM
    ŞANTİYELERİNİN toplamıdır. İki ayrı şantiyenin gönderilmiş günlüğünün
    TOPLAMIYLA (12 + 5 = 17) birebir eşit satır `diary` damgalanır — TEK bir
    şantiyenin toplamıyla eşit satır (12) ise `manual` KALIR, yoksa "proje
    toplamı" iddiası sessizce "herhangi bir şantiye yeter"e çürürdü.
    """
    site_a, project, pozlar_a = santiye
    poz_a = sorted(pozlar_a, key=lambda item: item.code)[0]
    sozlesme_kalemi = await sozlesme_kalemi_fabrikasi(poz_a, project)

    site_b, _, pozlar_b = await santiye_fabrikasi("SD-GENEL-B", project=proje)
    poz_b = sorted(pozlar_b, key=lambda item: item.code)[0]
    poz_b.contract_item_id = sozlesme_kalemi.id
    await seeded_db.flush()

    contract = await taseron_sozlesmesi_fabrikasi(
        project, site=None, kalemler=[("TK-1", sozlesme_kalemi)], code="TS-GENEL"
    )
    await gunluk_api(
        admin_headers,
        site_a.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_a.id), "quantity": "12"}],
    )
    await gunluk_api(
        admin_headers,
        site_b.id,
        date(2026, 7, 11),
        [{"boq_item_id": str(poz_b.id), "quantity": "5"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)

    tek_santiye = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert tek_santiye.status_code == 200, tek_santiye.text
    assert _damgalar(tek_santiye.json()) == {str(tk1): "manual"}

    proje_toplami = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "17"}]
    )
    assert proje_toplami.status_code == 200, proje_toplami.text
    assert _damgalar(proje_toplami.json()) == {str(tk1): "diary"}


# --- Dönem süzgeci ---


async def test_BASKA_ayin_gunlugu_damgayi_DOLDURMAZ(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 6, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


async def test_DONEMSIZ_hakedis_damgalanmaz(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


# --- 5) Damga her PUT'ta TAZELENİR ---


async def test_ikinci_PUTta_damga_her_iki_yonde_TAZELENIR(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")
    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)

    async def _damga(miktar: str) -> str:
        yanit = await _kaydet(
            client,
            admin_headers,
            hakedis["id"],
            [{"contract_item_id": str(tk1), "quantity": miktar}],
        )
        assert yanit.status_code == 200, yanit.text
        return _damgalar(yanit.json())[str(tk1)]

    assert await _damga("12") == "diary"
    assert await _damga("9") == "manual"
    assert await _damga("12") == "diary"


# --- 6) Gövdeden damga sızdırılamaz ---


async def test_govdedeki_quantity_source_ETKISIZDIR(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    """Mevcut bilinçli kural SÜRER: giriş şemasında alan yok, gönderilse de
    yok sayılır (miktar eşleşmiyorsa `manual`)."""
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client,
        admin_headers,
        hakedis["id"],
        [{"contract_item_id": str(tk1), "quantity": "5", "quantity_source": "diary"}],
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


# --- 7) PATCH ile DÖNEM değişince damga BAYAT KALMAZ (T5 bulgusu) ---


async def _donem_yamasi(client: AsyncClient, headers: dict[str, str], payment_id, **govde):
    return await client.patch(
        f"/subcontractor-progress-payments/{payment_id}", json=govde, headers=headers
    )


async def test_PATCH_ile_donem_degisince_diary_damgasi_DUSER(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    """İşveren ikizindeki bulgunun taşeron karşılığı: dönem taşınınca satır artık
    başka bir ayın günlüğüyle kıyaslanır, eski `diary` iddiası düşer."""
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 6, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, period_year=2026, period_month=6)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "diary"}

    yanit = await _donem_yamasi(client, admin_headers, hakedis["id"], period_month=7)
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}


async def test_PATCH_ile_donem_gunluge_TASININCA_damga_basilir(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api
) -> None:
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, period_year=2026, period_month=6)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}

    yanit = await _donem_yamasi(client, admin_headers, hakedis["id"], period_month=7)
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "diary"}


async def test_DONEM_DISI_alan_yamasi_gunluk_sorgusu_KOSTURMAZ(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, kurulum, gunluk_api, monkeypatch
) -> None:
    """Yalnız `description` değişen PATCH damgaya dokunmaz ve günlük sorgusunu
    hiç koşturmaz."""
    site, _, (kalem_a, _), contract = kurulum
    await gunluk_api(
        admin_headers,
        site.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(kalem_a.id), "quantity": "12"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")

    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text

    from app.modules.site_diary import bridge

    cagrilar: list[int] = []
    gercek = bridge.subcontractor_period_totals

    async def _sayan(*args, **kwargs):
        cagrilar.append(1)
        return await gercek(*args, **kwargs)

    monkeypatch.setattr(bridge, "subcontractor_period_totals", _sayan)

    yanit = await _donem_yamasi(client, admin_headers, hakedis["id"], description="yalnız açıklama")
    assert yanit.status_code == 200, yanit.text
    assert cagrilar == []
    assert _damgalar(yanit.json()) == {str(tk1): "diary"}


# --- 8) Sözleşmenin ŞANTİYESİ değişince damga BAYAT KALMAZ (T6, karar S9/2) ---
#
# Damganın İKİNCİ bayatlama kapısı: köprü `contract.site_id`'ye bağlıdır, ama
# sözleşme PATCH ile başka şantiyeye taşınabilir (ya da proje geneline
# düşürülebilir). Tazeleme YALNIZ `draft` hakedişlere iner — onaylı/ödenmiş
# evrak DONMUŞTUR.


async def _sozlesme_yamasi(client: AsyncClient, headers: dict[str, str], contract_id, **govde):
    return await client.patch(
        f"/subcontractor-contracts/{contract_id}", json=govde, headers=headers
    )


async def _hakedis_oku(client: AsyncClient, headers: dict[str, str], payment_id) -> dict:
    yanit = await client.get(f"/subcontractor-progress-payments/{payment_id}", headers=headers)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


@pytest.fixture
async def tasima_kurulumu(
    seeded_db: AsyncSession,
    proje,
    santiye_fabrikasi,
    sozlesme_kalemi_fabrikasi,
    taseron_sozlesmesi_fabrikasi,
):
    """Aynı projede İKİ şantiye; ikisinin pozu da AYNI işveren kalemine köprülü.

    Sözleşme A'ya bağlı kurulur. Köprünün iki adımlı olması (poz → işveren
    kalemi → taşeron kalemi) taşımanın gerçek etkisini gösterir: taşeron kalemi
    DEĞİŞMEZ, değişen tek şey hangi şantiyenin günlüğüne bakıldığıdır.
    """
    site_a, project, pozlar_a = await santiye_fabrikasi("SD-TA", project=proje)
    poz_a = sorted(pozlar_a, key=lambda item: item.code)[0]
    sozlesme_kalemi = await sozlesme_kalemi_fabrikasi(poz_a, project)

    site_b, _, pozlar_b = await santiye_fabrikasi("SD-TB", project=project)
    poz_b = sorted(pozlar_b, key=lambda item: item.code)[0]
    poz_b.contract_item_id = sozlesme_kalemi.id
    await seeded_db.flush()

    contract = await taseron_sozlesmesi_fabrikasi(
        project, site=site_a, kalemler=[("TK-1", sozlesme_kalemi)], code="TS-TASIMA"
    )
    return (site_a, poz_a), (site_b, poz_b), contract


async def _damgali_hakedis(
    client: AsyncClient, seeded_db: AsyncSession, headers: dict[str, str], contract, beklenen: str
) -> tuple[str, str]:
    """Tek satırlı (`TK-1`, 12) taslak hakediş açar ve damgasını doğrular."""
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")
    hakedis = await _hakedis(client, headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "12"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): beklenen}
    return hakedis["id"], str(tk1)


async def _zincir_onaycilari(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> list[dict[str, str]]:
    """OK-1A T3 — taşeron zincirinin ÜÇ adımını atacak ÜÇ ayrı aktör.

    Her aktörün İKİSİ birden gerekir: uç kapısı (`progress_payments ≥ approve`,
    matriste `project_manager`/`accounting` = `_APR`) ve adımın ONAY ROLÜ.
    `site_chief` SİSTEM rolü `progress_payments=_DRF`tir, yani gerçek şantiye
    şefi uçtan geçemez — o ADIM burada `project_manager` sistem rolündeki bir
    aktör tarafından atılır ve bu, onay rolü ≠ sistem rolü ayrımının kendisidir.
    """
    from app.modules.approvals.models import ApprovalRole
    from tests.modules.approvals.conftest import onay_rolu_ver

    tanimlar = (
        ("sd-sef@ok1a.co", "project_manager", ApprovalRole.site_chief),
        ("sd-pm@ok1a.co", "project_manager", ApprovalRole.project_manager),
        ("sd-muhasebe@ok1a.co", "accounting", ApprovalRole.accounting),
    )
    basliklar: list[dict[str, str]] = []
    for email, sistem_rolu, onay_rolu in tanimlar:
        user = await user_factory(email=email, password="parola1234", role_key=sistem_rolu)
        # IZN-B3b: onay rolü = PROJE rolü → ekip kişisi `all_projects=False` (üretimde "Tüm
        # projeler" kişide ekip satırı yoktur; `step_owner_clause` o satırı yok sayar).
        user.all_projects = False
        await onay_rolu_ver(seeded_db, user, onay_rolu)
        giris = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
        assert giris.status_code == 200, giris.text
        basliklar.append({"Authorization": f"Bearer {giris.json()['access_token']}"})
    return basliklar


async def test_sozlesme_BASKA_santiyeye_TASININCA_diary_damgasi_DUSER(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, tasima_kurulumu, gunluk_api
) -> None:
    """A'nın günlüğüyle damgalanan satır, sözleşme B'ye taşınınca B'nin günlüğüyle
    hiç ilgisi olmadan rozetli KALAMAZ."""
    (site_a, poz_a), (site_b, _), contract = tasima_kurulumu
    await gunluk_api(
        admin_headers,
        site_a.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_a.id), "quantity": "12"}],
    )
    payment_id, tk1 = await _damgali_hakedis(client, seeded_db, admin_headers, contract, "diary")

    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, site_id=str(site_b.id))
    assert yama.status_code == 200, yama.text
    assert _damgalar(await _hakedis_oku(client, admin_headers, payment_id)) == {tk1: "manual"}


async def test_sozlesme_PROJE_GENELINE_dusunce_damga_PROJE_TOPLAMINA_gore_TAZELENIR(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, tasima_kurulumu, gunluk_api
) -> None:
    """Spec §7 S5 TERSİNE ÇEVRİLDİ, kullanıcı kararı 2026-08-27.

    ESKİ İDDİA (`test_sozlesme_PROJE_GENELINE_dusunce_diary_damgasi_DUSER`):
    `site_id: null` = köprü TÜMDEN düşer, damga da düşer (`manual`).

    YENİ İDDİA: `site_id: null` köprüyü DÜŞÜRMEZ, GENİŞLETİR — süzgeç şantiyeden
    projeye çıkar. A'da 12, B'de 3 varken A'ya bağlı sözleşmede 15'lik satır
    `manual`dır (A'nın toplamı 12); sözleşme proje geneline düşünce aynı satır
    proje toplamıyla (15) eşleşir ve `diary` OLUR. Tazelemenin yönü tek taraflı
    bir SİLME değil, iddianın yeniden sınanmasıdır.
    """
    (site_a, poz_a), (site_b, poz_b), contract = tasima_kurulumu
    await gunluk_api(
        admin_headers,
        site_a.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_a.id), "quantity": "12"}],
    )
    await gunluk_api(
        admin_headers,
        site_b.id,
        date(2026, 7, 11),
        [{"boq_item_id": str(poz_b.id), "quantity": "3"}],
    )
    tk1 = await _kalem_id(seeded_db, contract.id, "TK-1")
    hakedis = await _hakedis(client, admin_headers, contract.id, **DONEM)
    yanit = await _kaydet(
        client, admin_headers, hakedis["id"], [{"contract_item_id": str(tk1), "quantity": "15"}]
    )
    assert yanit.status_code == 200, yanit.text
    assert _damgalar(yanit.json()) == {str(tk1): "manual"}

    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, site_id=None)
    assert yama.status_code == 200, yama.text
    assert _damgalar(await _hakedis_oku(client, admin_headers, hakedis["id"])) == {
        str(tk1): "diary"
    }


async def test_sozlesme_GUNLUGU_OLAN_santiyeye_tasininca_damga_BASILIR(
    client: AsyncClient, seeded_db: AsyncSession, admin_headers, tasima_kurulumu, gunluk_api
) -> None:
    """Ters yön: tazeleme tek yönlü bir SİLME değil, iddianın yeniden sınanmasıdır."""
    _, (site_b, poz_b), contract = tasima_kurulumu
    await gunluk_api(
        admin_headers,
        site_b.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_b.id), "quantity": "12"}],
    )
    payment_id, tk1 = await _damgali_hakedis(client, seeded_db, admin_headers, contract, "manual")

    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, site_id=str(site_b.id))
    assert yama.status_code == 200, yama.text
    assert _damgalar(await _hakedis_oku(client, admin_headers, payment_id)) == {tk1: "diary"}


async def test_ONAYLI_hakedisin_damgasi_santiye_degisse_bile_DONMUSTUR(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    user_factory,
    tasima_kurulumu,
    gunluk_api,
) -> None:
    """Donmuş evrak prensibi (S9/2): aynı sözleşmenin onaylı hakedişi tazelemeden
    ETKİLENMEZ, yanındaki taslak ETKİLENİR.

    🔴 OK-1A T3 UYARLAMASI: hakedişi `approved` yapmak artık TEK bir `/approve`
    çağrısı DEĞİL, taşeron zincirinin ÜÇ adımıdır (Şantiye Şefi → Proje Müdürü
    → Muhasebe) ve GÖREVLER AYRILIĞI yüzünden üçünü de aynı kişi atamaz. Testin
    iddiası (donmuş damga) değişmedi; yalnız "onaylı hâle getirme" yolu
    gerçekçileşti — ve bu, damganın onay ZİNCİRİNDEN de etkilenmediğini
    kanıtlıyor.
    """
    (site_a, poz_a), (site_b, _), contract = tasima_kurulumu
    await gunluk_api(
        admin_headers,
        site_a.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_a.id), "quantity": "12"}],
    )
    onayli_id, tk1 = await _damgali_hakedis(client, seeded_db, admin_headers, contract, "diary")
    gonder = await client.post(
        f"/subcontractor-progress-payments/{onayli_id}/submit", headers=admin_headers
    )
    assert gonder.status_code == 200, gonder.text
    for basliklar in await _zincir_onaycilari(client, seeded_db, user_factory):
        yanit = await client.post(
            f"/subcontractor-progress-payments/{onayli_id}/approve", headers=basliklar
        )
        assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "approved", yanit.text
    taslak_id, _ = await _damgali_hakedis(client, seeded_db, admin_headers, contract, "diary")

    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, site_id=str(site_b.id))
    assert yama.status_code == 200, yama.text
    assert _damgalar(await _hakedis_oku(client, admin_headers, onayli_id)) == {tk1: "diary"}
    assert _damgalar(await _hakedis_oku(client, admin_headers, taslak_id)) == {tk1: "manual"}


async def test_SANTIYE_DISI_sozlesme_yamasi_gunluk_sorgusu_KOSTURMAZ(
    client: AsyncClient,
    seeded_db: AsyncSession,
    admin_headers,
    tasima_kurulumu,
    gunluk_api,
    monkeypatch,
) -> None:
    """Şantiyeye dokunmayan PATCH gereksiz iş yapmaz — `site_id` gövdede olsa
    bile DEĞER aynıysa köprü hiç sorgulanmaz."""
    (site_a, poz_a), _, contract = tasima_kurulumu
    await gunluk_api(
        admin_headers,
        site_a.id,
        date(2026, 7, 10),
        [{"boq_item_id": str(poz_a.id), "quantity": "12"}],
    )
    payment_id, tk1 = await _damgali_hakedis(client, seeded_db, admin_headers, contract, "diary")

    from app.modules.site_diary import bridge

    cagrilar: list[int] = []
    gercek = bridge.subcontractor_period_totals

    async def _sayan(*args, **kwargs):
        cagrilar.append(1)
        return await gercek(*args, **kwargs)

    monkeypatch.setattr(bridge, "subcontractor_period_totals", _sayan)

    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, work_category="Kaba yapı")
    assert yama.status_code == 200, yama.text
    yama = await _sozlesme_yamasi(client, admin_headers, contract.id, site_id=str(site_a.id))
    assert yama.status_code == 200, yama.text

    assert cagrilar == []
    assert _damgalar(await _hakedis_oku(client, admin_headers, payment_id)) == {tk1: "diary"}
