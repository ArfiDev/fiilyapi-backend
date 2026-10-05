"""FIN-1 T6 — yedi ucun uctan uca bekcileri (E10).

🔴 **SEMA KATMANI BEKCILERI SUITE'E GORUNMEZ (MU-1 dersi):** `test_fin1_schemas.py`
kurallarin VARLIGINI kanitlar, bu dosya ucta BAGLI olduklarini. Ikisi de gerekli:
sema kurali yazip yalniz modeli kuran test kosmak, kurali hic sinamamaktir.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog
from app.modules.treasury.instruments import derive, guards
from app.modules.treasury.models import (
    FinancialInstrumentDirection as Yon,
)
from app.modules.treasury.models import (
    FinancialInstrumentKind as Tur,
)
from app.modules.treasury.models import (
    FinancialInstrumentStatus as Durum,
)
from tests._silme_yardimci import sil_aile

KOK = "/financial-instruments"

_GECERLI_GOVDE = {
    "instrument_kind": "cheque",
    "direction": "received",
    "serial_no": "0123456789",
    "drawer_name": "Güneşkent A.Ş.",
    "description": "Proje iş avansı",
    "bank_name": "Ziraat Bank",
    "issue_date": "2026-07-01",
    "due_date": "2026-07-25",
    "amount": "1200000.00",
}

#: Testlerin "bugun"u. Gercek takvimden okunsaydi `is_due` iddialari ayin son
#: gunu kendiliginden kirmizi olurdu — bekci degil KUMAR olurdu.
BUGUN = date(2026, 7, 15)


@pytest.fixture(autouse=True)
def _sabit_bugun(monkeypatch: pytest.MonkeyPatch) -> None:
    """`derive.as_of_today` TEK giris noktasidir — bu yuzden tek yamayla butun
    uclar (liste · detay · yazma · gecis · ozet) ayni gunu okur.

    Uclar dogrudan `timezone.today()` cagirsaydi burada BES ayri yama gerekirdi
    ve biri unutuldugunda o uc gercek takvimi okumaya devam ederdi.
    """
    monkeypatch.setattr(derive, "as_of_today", lambda: BUGUN)


# --------------------------------------------------------------------------- #
# 🔴 ROTA SIRASI (MK-2 dersi)
# --------------------------------------------------------------------------- #


async def test_rota_sirasi_summary_UUID_SANILMAZ(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """`/financial-instruments/summary` LITERALDIR ve `{instrument_id}` (UUID)
    rotasindan ONCE kaydedilmelidir.

    Sonra kaydedilseydi FastAPI `summary`yi bir UUID sanip **422** dondururdu.
    Bekci 200 goruyorsa sira dogrudur; 422 gorurse rota sirasi bozulmustur.
    """
    resp = await client.get(f"{KOK}/summary", headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    assert "portfolio_received" in resp.json()


# --------------------------------------------------------------------------- #
# Izin matrisi — dort seviye, YEDI uc
# --------------------------------------------------------------------------- #


async def test_okuma_uclari_view_ISTER(
    client: AsyncClient, pm_headers: dict[str, str], yetkisiz_headers: dict[str, str]
) -> None:
    """`project_manager` (`treasury=_V`) OKUR; `site_chief` (`_N`) okumada bile 403."""
    for yol in (KOK, f"{KOK}/summary"):
        assert (await client.get(yol, headers=pm_headers)).status_code == 200, yol
        assert (await client.get(yol, headers=yetkisiz_headers)).status_code == 403, yol


async def test_yazma_uclari_full_ISTER_PM_YAZAMAZ(
    client: AsyncClient, pm_headers: dict[str, str], muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 `view` YAZMAYA YETMEZ: PM portfoyu okur ama degistiremez."""
    cek = await cek_fabrikasi()
    assert (await client.post(KOK, json=_GECERLI_GOVDE, headers=pm_headers)).status_code == 403
    assert (
        await client.patch(f"{KOK}/{cek.id}", json={"bank_name": "X"}, headers=pm_headers)
    ).status_code == 403
    assert (
        await client.post(
            f"{KOK}/{cek.id}/status", json={"status": "collected"}, headers=pm_headers
        )
    ).status_code == 403
    # …muhasebe (`full`) UCUNU DE yapabilir.
    assert (
        await client.post(KOK, json=_GECERLI_GOVDE, headers=muhasebe_headers)
    ).status_code == 201


async def test_DELETE_yalniz_admin_FULL_SILEMEZ(
    client: AsyncClient,
    admin_headers: dict[str, str],
    muhasebe_headers: dict[str, str],
    cek_fabrikasi,
) -> None:
    """🔴 Repo kanonu: **silme yalniz `admin`** — `full` silmeyi KAPSAMAZ.

    Muhasebeci cek/senet kaydini tek basina dusurememelidir; kardes uc
    `DELETE /bank-accounts/{id}` de aynen boyledir.
    """
    cek = await cek_fabrikasi()
    cek_id = cek.id
    assert (await client.delete(f"{KOK}/{cek_id}", headers=muhasebe_headers)).status_code == 403
    assert (
        await sil_aile(client, admin_headers, "financial_instrument", cek_id)
    ).status_code == 204


# --------------------------------------------------------------------------- #
# POST — govde kurallari
# --------------------------------------------------------------------------- #


async def test_POST_yeni_kayit_HER_ZAMAN_portfoyde_dogar(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    resp = await client.post(KOK, json=_GECERLI_GOVDE, headers=muhasebe_headers)
    assert resp.status_code == 201, resp.text
    govde = resp.json()
    assert govde["status"] == Durum.portfolio.value
    # E10:116 kesidecinin altindaki gri satir.
    assert govde["description"] == "Proje iş avansı"


async def test_POST_status_govdede_422(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """🔴 K7 UCTA. Sema testi kuralin VARLIGINI, bu test ucta BAGLI oldugunu
    kanitlar."""
    resp = await client.post(
        KOK, json={**_GECERLI_GOVDE, "status": "collected"}, headers=muhasebe_headers
    )
    assert resp.status_code == 422, resp.text


async def test_POST_vade_kesideden_ONCE_ise_422(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """Sessizce kabul edilseydi vade raporu ve "Bu Ay Vadeli" karti bozulurdu."""
    resp = await client.post(
        KOK,
        json={**_GECERLI_GOVDE, "issue_date": "2026-07-25", "due_date": "2026-07-01"},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == guards.DUE_BEFORE_ISSUE


async def test_POST_ayni_gun_vade_MESRUDUR(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """🔴 SINIRIN KABUL TARAFI: `>=` yerine `>` yazilirsa BU test kirmizi olur.
    Goruldugunde odenen cek gercek bir olgudur."""
    resp = await client.post(
        KOK,
        json={**_GECERLI_GOVDE, "issue_date": "2026-07-25", "due_date": "2026-07-25"},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 201, resp.text


async def test_POST_yarim_kurus_422_SESSIZCE_YUVARLANMAZ(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """🔴 Emrin ayrisma noktasi. Yuvarlansaydi kullanici girdiginden BASKA bir
    tutari kaydetmis olurdu ve fark yalnizca mutabakatta gorunurdu."""
    resp = await client.post(
        KOK, json={**_GECERLI_GOVDE, "amount": "0.005"}, headers=muhasebe_headers
    )
    assert resp.status_code == 422, resp.text


async def test_POST_serial_no_MUKERRER_olabilir(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """🔴 K3 UCTA: farkli bankalarin cek numaralari cakisir. UNIQUE konsaydi
    mesru bir kayit hic girilemezdi."""
    assert (
        await client.post(KOK, json=_GECERLI_GOVDE, headers=muhasebe_headers)
    ).status_code == 201
    ikinci = await client.post(
        KOK, json={**_GECERLI_GOVDE, "direction": "issued"}, headers=muhasebe_headers
    )
    assert ikinci.status_code == 201, ikinci.text


# --------------------------------------------------------------------------- #
# 🔴 GOVDE ICI VARLIK REFERANSI → 404 (ST kanonu), 403 DEGIL
# --------------------------------------------------------------------------- #


async def test_POST_GORUNMEYEN_proje_404(
    client: AsyncClient, kapsamli_muhasebe_headers: dict[str, str], gorunmeyen_proje
) -> None:
    """403 olsaydi "bu proje VAR ama goremezsin" bilgisi SIZARDI (IDOR)."""
    resp = await client.post(
        KOK,
        json={**_GECERLI_GOVDE, "project_id": str(gorunmeyen_proje.id)},
        headers=kapsamli_muhasebe_headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == guards.PROJECT_INVALID


async def test_POST_var_olmayan_banka_hesabi_404(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """Banka hesabi SIRKET GENELIDIR (HZ-1 K3) → yalnizca VARLIK denetlenir;
    burada bir kapsam suzgeci yazmak OLMAYAN bir suzgeci varmis gibi gosterirdi."""
    resp = await client.post(
        KOK,
        json={**_GECERLI_GOVDE, "bank_account_id": str(uuid.uuid4())},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == guards.BANK_ACCOUNT_INVALID


async def test_POST_gorunen_proje_ve_hesap_KABUL_EDILIR(
    client: AsyncClient, kapsamli_muhasebe_headers: dict[str, str], gorunen_proje, hesap_fabrikasi
) -> None:
    """🔴 KABUL TARAFI da olculur: 404 dalini `project_id is not None` diye
    daraltan bir mutasyon yalniz bu testle yakalanir."""
    hesap = await hesap_fabrikasi()
    resp = await client.post(
        KOK,
        json={
            **_GECERLI_GOVDE,
            "project_id": str(gorunen_proje.id),
            "bank_account_id": str(hesap.id),
        },
        headers=kapsamli_muhasebe_headers,
    )
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# Kapsam suzgeci — liste, detay ve `total`
# --------------------------------------------------------------------------- #


async def test_GORUNMEYEN_projenin_ceki_LISTEDE_ve_TOTALDE_YOK(
    client: AsyncClient,
    kapsamli_muhasebe_headers: dict[str, str],
    cek_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """🔴 `total` yetki suzgecini SQL `COUNT`un ICINDE tasir; aksi hâlde
    kullanici goremedigi kayitlari SAYARDI (BOR-TEMIZ kanonu).

    Kurulum ucuncu bir hâli de kapsar: PROJESIZ cek (sirket geneli) GORUNUR —
    `scope_clause` yalniz `IN` yazsaydi bu kayit HERKESTEN gizlenirdi.
    """
    await cek_fabrikasi(project=gorunen_proje, serial_no="GORUNUR")
    await cek_fabrikasi(project=gorunmeyen_proje, serial_no="GIZLI")
    await cek_fabrikasi(project=None, serial_no="PROJESIZ")

    resp = await client.get(KOK, headers=kapsamli_muhasebe_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    numaralar = {satir["serial_no"] for satir in govde["items"]}
    assert numaralar == {"GORUNUR", "PROJESIZ"}
    assert govde["total"] == 2


async def test_GORUNMEYEN_projenin_ceki_DETAYDA_404(
    client: AsyncClient, kapsamli_muhasebe_headers: dict[str, str], cek_fabrikasi, gorunmeyen_proje
) -> None:
    """Govde var OLMAYAN kimliginkiyle BIREBIR AYNIDIR: ayrisirsa varligi
    sizdirir."""
    cek = await cek_fabrikasi(project=gorunmeyen_proje)
    gizli = await client.get(f"{KOK}/{cek.id}", headers=kapsamli_muhasebe_headers)
    yok = await client.get(f"{KOK}/{uuid.uuid4()}", headers=kapsamli_muhasebe_headers)
    assert gizli.status_code == yok.status_code == 404
    assert gizli.json() == yok.json()


async def test_gorunmeyen_projenin_ceki_GECISTE_de_404(
    client: AsyncClient, kapsamli_muhasebe_headers: dict[str, str], cek_fabrikasi, gorunmeyen_proje
) -> None:
    """Kapsam denetimi gecis tablosundan ONCE kosar: gorunmeyen bir kaydin
    DURUMU hakkinda 409 ile bilgi sizdirilmaz (`apply_request_transition`
    kanonu)."""
    cek = await cek_fabrikasi(project=gorunmeyen_proje, status=Durum.collected)
    resp = await client.post(
        f"{KOK}/{cek.id}/status", json={"status": "cancelled"}, headers=kapsamli_muhasebe_headers
    )
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------- #
# Sayfalama ve suzgecler
# --------------------------------------------------------------------------- #


async def test_liste_SAYFALIDIR_ve_total_SAYFADAN_BAGIMSIZDIR(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 IKI SAYAC AYRI SEYDIR: `total` suzgeclenmis kumenin boyutudur
    (sayfalamadan ONCE), `items` ise SAYFADIR. Ikisinin esit oldugu bir kurulumda
    yazilan test hicbir sey kanitlamaz — bu yuzden `limit` kumeden KUCUKTUR."""
    for gun in (10, 11, 12):
        await cek_fabrikasi(due_date=date(2026, 7, gun))
    resp = await client.get(KOK, params={"limit": 2, "offset": 0}, headers=muhasebe_headers)
    govde = resp.json()
    assert len(govde["items"]) == 2
    assert govde["total"] == 3
    assert govde["limit"] == 2 and govde["offset"] == 0


async def test_limit_TAVANI_ASILIRSA_422_KIRPILMAZ(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """TB3 kanonu: sessiz kirpma kullaniciyi "kayit yok" sanisina dusururdu."""
    assert (
        await client.get(KOK, params={"limit": 201}, headers=muhasebe_headers)
    ).status_code == 422
    assert (
        await client.get(KOK, params={"limit": 200}, headers=muhasebe_headers)
    ).status_code == 200


async def test_siralama_VADEYE_gore_ARTANDIR(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """Portfoy ekrani vadeye gore okunur; `ORDER BY` olmasaydi PG sirasi
    kosudan kosuya degisebilirdi."""
    await cek_fabrikasi(due_date=date(2026, 9, 30), serial_no="UCUNCU")
    await cek_fabrikasi(due_date=date(2026, 7, 20), serial_no="BIRINCI")
    await cek_fabrikasi(due_date=date(2026, 8, 15), serial_no="IKINCI")
    resp = await client.get(KOK, headers=muhasebe_headers)
    assert [s["serial_no"] for s in resp.json()["items"]] == ["BIRINCI", "IKINCI", "UCUNCU"]


@pytest.mark.parametrize(
    ("param", "deger", "beklenen"),
    [
        ("direction", "issued", {"VERILEN"}),
        ("instrument_kind", "promissory_note", {"SENET"}),
        ("status", "cancelled", {"IPTAL"}),
        ("due_before", "2026-07-05", {"ERKEN"}),
        ("due_after", "2026-12-01", {"GEC"}),
        ("q", "Çelik", {"KESIDECI"}),
        ("q", "9876", {"NOARAMA98765"}),
    ],
)
async def test_suzgecler(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    cek_fabrikasi,
    param: str,
    deger: str,
    beklenen: set[str],
) -> None:
    """Her suzgec AYRI olculur: tek testte toplansaydi biri hic kosmadan yesil
    kalabilirdi. `q` hem KESIDECI hem CEK NO uzerinde arar (emir K6)."""
    await cek_fabrikasi(direction=Yon.issued, serial_no="VERILEN")
    await cek_fabrikasi(instrument_kind=Tur.promissory_note, serial_no="SENET")
    await cek_fabrikasi(status=Durum.cancelled, serial_no="IPTAL")
    await cek_fabrikasi(due_date=date(2026, 7, 3), serial_no="ERKEN")
    await cek_fabrikasi(due_date=date(2026, 12, 25), serial_no="GEC")
    await cek_fabrikasi(drawer_name="Çelik Holding", serial_no="KESIDECI")
    await cek_fabrikasi(serial_no="NOARAMA98765")

    resp = await client.get(KOK, params={param: deger}, headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    bulunan = {s["serial_no"] for s in resp.json()["items"]}
    assert beklenen <= bulunan, (param, deger, bulunan)


async def test_q_LIKE_jokerini_KACIRIR(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """Kacirilmasaydi arama kutusuna `%` yazan kullanici TUM portfoyu gorurdu."""
    await cek_fabrikasi(serial_no="0000000001")
    resp = await client.get(KOK, params={"q": "%"}, headers=muhasebe_headers)
    assert resp.json()["total"] == 0


# --------------------------------------------------------------------------- #
# 🔴 `is_due` — TUREV, uctan
# --------------------------------------------------------------------------- #


async def test_is_due_status_ile_AYRI_alandir_UCTAN(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """K2: frontend rozeti IKISINDEN kurar (E10:121 turuncu · E10:130 yesil)."""
    vadede = await cek_fabrikasi(due_date=date(2026, 7, 25))
    portfoyde = await cek_fabrikasi(due_date=date(2026, 8, 15))
    tahsil = await cek_fabrikasi(
        issue_date=date(2026, 4, 1), due_date=date(2026, 6, 1), status=Durum.collected
    )

    resp = await client.get(KOK, headers=muhasebe_headers)
    satirlar = {s["id"]: s for s in resp.json()["items"]}
    assert satirlar[str(vadede.id)]["is_due"] is True
    assert satirlar[str(vadede.id)]["status"] == "portfolio"
    assert satirlar[str(portfoyde.id)]["is_due"] is False
    # Tahsil edilmis kayit vadesi GECMIS olsa da "Vadede" DEGILDIR.
    assert satirlar[str(tahsil.id)]["is_due"] is False


async def test_is_due_AY_SINIRI_uctan_IKI_BEKCI(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 MU-2 dersi: pencerenin SINIR GUNU acikca kullanilir. Ayin son gunu
    DAHIL, bir sonraki ayin ilk gunu DISARIDA."""
    son_gun = await cek_fabrikasi(due_date=date(2026, 7, 31))
    ertesi_gun = await cek_fabrikasi(due_date=date(2026, 8, 1))
    resp = await client.get(KOK, headers=muhasebe_headers)
    satirlar = {s["id"]: s for s in resp.json()["items"]}
    assert satirlar[str(son_gun.id)]["is_due"] is True
    assert satirlar[str(ertesi_gun.id)]["is_due"] is False


async def test_as_of_ECHO_edilir(client: AsyncClient, muhasebe_headers: dict[str, str]) -> None:
    """`is_due` onsuz DOGRULANAMAZ: istemci kendi saatiyle hesaplarsa TR gecesi
    00:00-03:00 arasinda sunucudan bir gun sapar."""
    assert (await client.get(KOK, headers=muhasebe_headers)).json()["as_of"] == BUGUN.isoformat()


# --------------------------------------------------------------------------- #
# PATCH
# --------------------------------------------------------------------------- #


async def test_PATCH_status_govdede_422(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 K7: POST'a konan sinir PATCH'i KORUMAZ — ayri ayri olculur."""
    cek = await cek_fabrikasi()
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"status": "collected"}, headers=muhasebe_headers
    )
    assert resp.status_code == 422, resp.text


async def test_PATCH_gonderilmeyen_alani_EZMEZ(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """`exclude_unset` olmasaydi yalniz banka duzelten bir istek aciklamayi
    SESSIZCE silerdi."""
    cek = await cek_fabrikasi(description="Proje iş avansı")
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"bank_name": "İş Bank"}, headers=muhasebe_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Proje iş avansı"
    assert resp.json()["bank_name"] == "İş Bank"


async def test_PATCH_acikca_null_TEMIZLER(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    cek = await cek_fabrikasi(description="Proje iş avansı")
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"description": None}, headers=muhasebe_headers
    )
    assert resp.json()["description"] is None


async def test_PATCH_zorunlu_alana_null_422(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """Sema hepsini `| None` yazmak ZORUNDADIR (govde kismidir); ayrim SERVISTE
    yapilir. Yoksa `NOT NULL` ihlali ham **500** olurdu."""
    cek = await cek_fabrikasi()
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"drawer_name": None}, headers=muhasebe_headers
    )
    assert resp.status_code == 422, resp.text


async def test_PATCH_vade_kurali_BIRLESIK_degerlerde_kosar(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 Kural yalniz govdeye baksaydi, tek basina gonderilen bir `due_date`
    kayittaki `issue_date`in ONUNE gecebilirdi."""
    cek = await cek_fabrikasi(issue_date=date(2026, 7, 10), due_date=date(2026, 7, 25))
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"due_date": "2026-07-01"}, headers=muhasebe_headers
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == guards.DUE_BEFORE_ISSUE


async def test_PATCH_TERMINAL_kayitta_YON_degistirilemez(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 EMIRDE YOKTU, T4'te eklendi: yon PATCH ile degistirilebilseydi
    `collected` bir "alinan" cek "verilen"e cevrilir ve K2'nin ASLA
    uretemeyecegi `(issued, collected)` cifti PATCH uzerinden dogardi —
    invaryantin IKINCI yazma kapisi (BOQ-SEC-B kanonu)."""
    cek = await cek_fabrikasi(status=Durum.collected)
    resp = await client.patch(
        f"{KOK}/{cek.id}", json={"direction": "issued"}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.TERMINAL_STATUS_DIRECTION
    # …PORTFOYDEYKEN serbesttir: orada hicbir cifti bozmaz.
    portfoyde = await cek_fabrikasi()
    serbest = await client.patch(
        f"{KOK}/{portfoyde.id}", json={"direction": "issued"}, headers=muhasebe_headers
    )
    assert serbest.status_code == 200, serbest.text


# --------------------------------------------------------------------------- #
# Durum gecisi — uctan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("yon", "hedef"),
    [
        ("received", "collected"),
        ("received", "returned"),
        ("received", "cancelled"),
        ("issued", "paid"),
        ("issued", "returned"),
        ("issued", "cancelled"),
    ],
)
async def test_GECERLI_gecis_uctan_200(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi, yon: str, hedef: str
) -> None:
    cek = await cek_fabrikasi(direction=Yon(yon))
    resp = await client.post(
        f"{KOK}/{cek.id}/status", json={"status": hedef}, headers=muhasebe_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == hedef


async def test_ALINAN_cek_ODENDI_olamaz_uctan_409(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    cek = await cek_fabrikasi(direction=Yon.received)
    resp = await client.post(
        f"{KOK}/{cek.id}/status", json={"status": "paid"}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DIRECTION_MISMATCH


async def test_VERILEN_cek_TAHSIL_EDILDI_olamaz_uctan_409(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 Emir: iki dal AYRI testlerle cakilir, "gecersiz gecis" diye
    TOPLANMAZ."""
    cek = await cek_fabrikasi(direction=Yon.issued)
    resp = await client.post(
        f"{KOK}/{cek.id}/status", json={"status": "collected"}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.DIRECTION_MISMATCH


async def test_TERMINAL_kayitta_IKINCI_gecis_409_ve_DURUM_DEGISMEZ(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 EMRIN AYRISMA NOKTASI: yanit kodunu dogrulayan test, SATIRIN DA
    degismedigini AYRICA okumali.

    Yalniz 409 iddia eden bir test, durumu once yazip sonra hata firlatan bir
    uygulamada YESIL kalirdi.
    """
    cek = await cek_fabrikasi(status=Durum.collected)
    resp = await client.post(
        f"{KOK}/{cek.id}/status", json={"status": "cancelled"}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.TERMINAL_STATUS

    tekrar = await client.get(f"{KOK}/{cek.id}", headers=muhasebe_headers)
    assert tekrar.json()["status"] == Durum.collected.value


@pytest.mark.parametrize(
    ("islem", "beklenen_parca"),
    [
        ("create", "oluşturuldu"),
        ("status", "Tahsil Edildi"),
        ("update", "güncellendi"),
        ("delete", "silindi"),
    ],
)
async def test_YAZMA_uclari_DENETIM_gunlugu_yazar(
    client: AsyncClient,
    admin_headers: dict[str, str],
    seeded_db: AsyncSession,
    cek_fabrikasi,
    islem: str,
    beklenen_parca: str,
) -> None:
    """Dort yazma ucunun DORDU DE tek denetim satiri yazar.

    🔴 Gunluk DB'den okunur, `/audit-log` UCUNDAN DEGIL: uc kendi yetki kapisini
    tasir ve rol yetmezse test SESSIZCE atlanirdi (ilk hâlinde `pytest.skip`
    boyle bir kor nokta uretmisti — atlanan test bir bekci degildir).

    🔴 Yeni `AuditAction` uyesi ACILMADI (TB3/T3 kanonu): durum gecisi de
    `update`tir, ayrim METINDEDIR — bu yuzden iddia METNEDIR.
    """
    cek = await cek_fabrikasi(serial_no="0123456789", drawer_name="Güneşkent A.Ş.")
    if islem == "create":
        await client.post(KOK, json=_GECERLI_GOVDE, headers=admin_headers)
    elif islem == "status":
        await client.post(
            f"{KOK}/{cek.id}/status", json={"status": "collected"}, headers=admin_headers
        )
    elif islem == "update":
        await client.patch(f"{KOK}/{cek.id}", json={"bank_name": "İş Bank"}, headers=admin_headers)
    else:
        await sil_aile(client, admin_headers, "financial_instrument", cek.id)

    satirlar = (await seeded_db.execute(select(AuditLog))).scalars().all()
    metinler = [s.detail for s in satirlar]
    assert any("Çek/senet" in m and beklenen_parca in m for m in metinler), metinler


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #


async def test_DELETE_tahsil_edilmis_cek_de_silinir_SIL_B2(
    client: AsyncClient, admin_headers: dict[str, str], cek_fabrikasi
) -> None:
    """SIL-B2: Sistem Yoneticisi her kosulda siler (K2/K4); eski "yalniz portfoyde" 409'u
    yalniz silme yolunda kalkti. Silinen mali iz denetim satirina yazilir (fis dokumu dahil)."""
    portfoyde = await cek_fabrikasi()
    tahsil = await cek_fabrikasi(status=Durum.collected)
    portfoyde_id, tahsil_id = portfoyde.id, tahsil.id
    assert (
        await sil_aile(client, admin_headers, "financial_instrument", portfoyde_id)
    ).status_code == 204
    assert (
        await sil_aile(client, admin_headers, "financial_instrument", tahsil_id)
    ).status_code == 204
    assert (await client.get(f"{KOK}/{tahsil_id}", headers=admin_headers)).status_code == 404


# --------------------------------------------------------------------------- #
# 🔴 K8 — ozet kartlari
# --------------------------------------------------------------------------- #


async def test_ozet_kartlari_ORTUSUR_ve_bu_TANIMDIR(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 Emir: *"Kartlarin toplaminin portfoye esit olmasini bekleyen bir test
    YAZMA; tersine, ortusmeyi KANITLAYAN bir bekci yaz."*

    Kurulum beklenen sayilari SABIT YAZMAZ, KUMEYI KURUP sonucu TURETIR
    (MT-2 kanonu: bir kumeyle calisan bekci kumenin kendisini de sinamali).
    """
    # Bu ay vadeli, portfoyde, ALINAN → hem 1. hem 3. karta girer.
    await cek_fabrikasi(direction=Yon.received, due_date=date(2026, 7, 20), amount="100.00")
    # Gelecek ay vadeli, portfoyde, ALINAN → yalniz 1. karta.
    await cek_fabrikasi(direction=Yon.received, due_date=date(2026, 9, 10), amount="200.00")
    # Bu ay vadeli, portfoyde, VERILEN → hem 2. hem 3. karta.
    await cek_fabrikasi(direction=Yon.issued, due_date=date(2026, 7, 5), amount="400.00")
    # Iade → yalniz 4. karta (portfoy kartlarina GIRMEZ).
    await cek_fabrikasi(direction=Yon.received, status=Durum.returned, amount="800.00")

    ozet = (await client.get(f"{KOK}/summary", headers=muhasebe_headers)).json()

    assert ozet["portfolio_received"] == {"amount": "300.00", "count": 2}
    assert ozet["issued"] == {"amount": "400.00", "count": 1}
    assert ozet["due_this_month"] == {"amount": "500.00", "count": 2}
    assert ozet["returned_cancelled"] == {"amount": "800.00", "count": 1}

    # 🔴 ORTUSMENIN KANITI: "bu ay vadeli" toplami, iki portfoy kartinin
    # PARCASIDIR — dordunun toplami portfoyu ASAR. Kartlar birbirini dislasaydi
    # bu iddia kirmizi olurdu.
    portfoy_toplami = Decimal(ozet["portfolio_received"]["amount"]) + Decimal(
        ozet["issued"]["amount"]
    )
    bu_ay = Decimal(ozet["due_this_month"]["amount"])
    assert bu_ay > 0
    assert bu_ay < portfoy_toplami
    dort_kart = portfoy_toplami + bu_ay + Decimal(ozet["returned_cancelled"]["amount"])
    assert dort_kart > portfoy_toplami + Decimal(ozet["returned_cancelled"]["amount"])


async def test_ozet_BU_AY_VADELI_TAKVIM_AYIDIR_IKI_SINIR_BEKCISI(
    client: AsyncClient, muhasebe_headers: dict[str, str], cek_fabrikasi
) -> None:
    """🔴 K8 + MU-2 dersi: ay sinirinda IKI AYRI bekci.

    * ayin **ILK** gunu (01.07) ve **SON** gunu (31.07) kartta OLMALI;
    * onceki ayin son gunu (30.06) ve sonraki ayin ilk gunu (01.08) OLMAMALI.

    "Bugunden 30 gun" secilseydi 01.07 (gecmis) DISARIDA, 01.08+ (17 gun sonra)
    ICERIDE kalirdi — iki iddia da tersine donerdi.
    """
    await cek_fabrikasi(due_date=date(2026, 7, 1), amount="1.00")
    await cek_fabrikasi(due_date=date(2026, 7, 31), amount="2.00")
    await cek_fabrikasi(issue_date=date(2026, 6, 1), due_date=date(2026, 6, 30), amount="400.00")
    await cek_fabrikasi(due_date=date(2026, 8, 1), amount="800.00")

    ozet = (await client.get(f"{KOK}/summary", headers=muhasebe_headers)).json()
    assert ozet["due_this_month"] == {"amount": "3.00", "count": 2}
    assert ozet["as_of"] == BUGUN.isoformat()


async def test_ozet_BOS_kumede_SIFIR_doner_NULL_DEGIL(
    client: AsyncClient, muhasebe_headers: dict[str, str]
) -> None:
    """`SUM()` kayitsiz kumede NULL doner ve kart "₺" yaninda bosluk basardi."""
    ozet = (await client.get(f"{KOK}/summary", headers=muhasebe_headers)).json()
    for kart in ("portfolio_received", "issued", "due_this_month", "returned_cancelled"):
        assert Decimal(ozet[kart]["amount"]) == 0
        assert ozet[kart]["count"] == 0


async def test_ozet_KAPSAM_suzgecini_uygular(
    client: AsyncClient,
    kapsamli_muhasebe_headers: dict[str, str],
    cek_fabrikasi,
    gorunen_proje,
    gorunmeyen_proje,
) -> None:
    """🔴 Kapsam `WHERE`dedir; kart basina tekrarlansaydi biri unutuldugunda
    YALNIZ o kart sizdirirdi — bu yuzden DORT kart da ayri ayri okunur."""
    await cek_fabrikasi(project=gorunen_proje, amount="100.00", due_date=date(2026, 7, 20))
    await cek_fabrikasi(project=gorunmeyen_proje, amount="900.00", due_date=date(2026, 7, 21))
    await cek_fabrikasi(project=gorunmeyen_proje, status=Durum.cancelled, amount="700.00")

    ozet = (await client.get(f"{KOK}/summary", headers=kapsamli_muhasebe_headers)).json()
    assert ozet["portfolio_received"] == {"amount": "100.00", "count": 1}
    assert ozet["due_this_month"] == {"amount": "100.00", "count": 1}
    assert ozet["issued"] == {"amount": "0.00", "count": 0}
    assert ozet["returned_cancelled"] == {"amount": "0.00", "count": 0}
