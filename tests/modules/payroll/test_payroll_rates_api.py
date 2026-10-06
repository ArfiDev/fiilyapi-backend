"""İK-3 T5 — oran tablosu yönetimi (K1, spec §5'in 12. satırı).

| Uç | Yetki |
|---|---|
| `GET /payroll/rates` | `payroll:view` |
| `PUT /payroll/rates/{year}/{source}` | `ayarlar.bordro_oranlari` Düzenler (B5a) |

## 🔴 BU DOSYANIN ASIL İŞİ: GEÇMİŞ DÖNEM DEĞİŞMEZ

K1 oranları VERİ yaptı; veri olan her şey değiştirilebilir ve değiştirilebilen
her şey GEÇMİŞİ de değiştirebilir. Bordroda bu para sınıfı bir kırıktır:
`summary.py` işveren maliyetini satırdaki kopyadan değil DÖNEMİN YILINA ait
CANLI oran setinden türetir (kasıtlı — satıra oran KOPYALANMAZ, K1). Dolayısıyla
2026 oranı değişse, ONAYLANMIŞ bir 2026 döneminin "toplam maliyet" ve
"SGK işveren" sütunları da geriye dönük değişirdi.

Korkuluk: **bir yılda `approved`/`paid` dönem varsa O YILIN oran setine
yazılamaz (409)** — ne güncelleme ne yeni tip. Şema DEĞİŞMEDEN geçmişi
dondurmanın tek dürüst yolu budur; alternatifi (satıra yedi oranı kopyalamak)
K1'in "tek gerçek kaynak" kararını bozardı.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.modules.audit.models import AuditLog
from app.modules.payroll.models import PayrollRate

from .conftest import SGK_4A, YIL

pytestmark = pytest.mark.asyncio


def _govde(**degisiklik) -> dict:
    """🔴 `None` STRINGE ÇEVRİLMEZ — o bir DEĞER, "yok" değil (IK3-GV K3).

    `income_tax_pct = None` "dilimli gelir vergisi motoru" demektir; `"None"`
    diye gönderilseydi şema 422 verir ve rejim seçimi HİÇ test edilemezdi.
    """
    return {
        **{alan: (None if deger is None else str(deger)) for alan, deger in SGK_4A.items()},
        **degisiklik,
    }


async def _put(client, headers, year, source, **degisiklik):
    return await client.put(
        f"/payroll/rates/{year}/{source}", json=_govde(**degisiklik), headers=headers
    )


# --- Kapılar ---------------------------------------------------------------


async def test_yetkisiz_rol_oranlari_goremez_403(client, yetkisiz_headers, seeded_db):
    assert (await client.get("/payroll/rates", headers=yetkisiz_headers)).status_code == 403


async def test_yetkisiz_rol_oran_yazamaz_403(client, yetkisiz_headers, seeded_db):
    resp = await _put(client, yetkisiz_headers, 2027, "company")
    assert resp.status_code == 403


# --- GET /payroll/rates ----------------------------------------------------


async def test_oran_setleri_listelenir(client, ik_headers, oranlar):
    """Yıl + tip + YEDİ oran + `is_active` (spec §4)."""
    resp = await client.get("/payroll/rates", headers=ik_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()

    assert govde["total"] == 4  # company · subcontractor · freelance · intern
    sirket = next(s for s in govde["items"] if s["personnel_source"] == "company")
    # 🔴 `Numeric(6,3)`: damga vergisi %0,759 TAM gelmeli — iki ondalık onu
    # 0,76'ya yuvarlayıp kesintiyi sessizce şişirirdi (models.py).
    assert Decimal(sirket["stamp_tax_pct"]) == Decimal("0.759")
    assert Decimal(sirket["sgk_employer_pct"]) == Decimal("20.500")
    assert sirket["is_active"] is True
    assert sirket["year"] == YIL


async def test_yila_gore_suzulur(client, ik_headers, oranlar, db_session):
    db_session.add(PayrollRate(year=2027, personnel_source="company", **SGK_4A))
    await db_session.flush()

    govde = (await client.get("/payroll/rates?year=2027", headers=ik_headers)).json()
    assert govde["total"] == 1
    assert govde["items"][0]["year"] == 2027


# --- PUT /payroll/rates/{year}/{source} ------------------------------------


async def test_yeni_set_OLUSTURULUR(client, ik_headers, seeded_db, db_session, admin_headers):
    resp = await _put(client, admin_headers, 2027, "company")
    assert resp.status_code == 200, resp.text
    assert resp.json()["year"] == 2027

    kayit = (
        await db_session.execute(
            select(PayrollRate).where(
                PayrollRate.year == 2027, PayrollRate.personnel_source == "company"
            )
        )
    ).scalar_one()
    assert kayit.sgk_employee_pct == Decimal("14.000")


async def test_mevcut_set_GUNCELLENIR_ikinci_satir_ACILMAZ(
    client, ik_headers, oranlar, db_session, admin_headers
):
    """UQ `(year, personnel_source)` — PUT ikinci satır YARATMAZ."""
    resp = await _put(client, admin_headers, YIL, "company", sgk_employee_pct="15.000")
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["sgk_employee_pct"]) == Decimal("15.000")

    sayi = (
        await db_session.execute(
            select(func.count())
            .select_from(PayrollRate)
            .where(PayrollRate.year == YIL, PayrollRate.personnel_source == "company")
        )
    ).scalar_one()
    assert sayi == 1


async def test_negatif_oran_422(client, ik_headers, seeded_db, admin_headers):
    assert (
        await _put(client, admin_headers, 2027, "company", short_work_pct="-1")
    ).status_code == 422


async def test_yuzden_buyuk_oran_422(client, ik_headers, seeded_db, admin_headers):
    """Üst sınır **%100**: bir oran brütün TAMAMINDAN fazlasını kesemez.

    Sınırsız bırakılsaydı bir yazım hatası (%2000) neti eksiye düşürür ve
    `ck_payroll_lines_net_positive` ihlaliyle `compute` 500'e patlardı — kullanıcı
    hatası sunucu hatası gibi görünürdü.
    """
    assert (
        await _put(client, admin_headers, 2027, "company", income_tax_pct="150")
    ).status_code == 422


async def test_isci_paylari_toplami_yuzu_ASAMAZ_422(client, ik_headers, seeded_db, admin_headers):
    """🔴 Dört işçi kaleminin TOPLAMI da %100'ü aşamaz — net EKSİYE düşerdi.

    Tek tek geçerli (her biri ≤ %100) ama toplamı %101 olan bir set, brütü
    tanımlı her personelin netini negatife çevirir ve DB CHECK'ine çarpardı.
    Sınır kalem başına değil TOPLAM üzerinde de durur.
    """
    resp = await _put(
        client,
        admin_headers,
        2027,
        "company",
        sgk_employee_pct="60",
        income_tax_pct="41",
    )
    assert resp.status_code == 422, resp.text


async def test_gecersiz_personel_tipi_422(client, ik_headers, seeded_db, admin_headers):
    assert (await _put(client, admin_headers, 2027, "yok_boyle_tip")).status_code == 422


async def test_eksik_oran_alani_422(client, ik_headers, seeded_db, admin_headers):
    """PUT TAM SETTİR: yedi oranın hepsi zorunludur.

    Kısmi gönderim kabul edilseydi eksik alan sessizce 0 olur ve "kesinti yok"
    yalanı üretilirdi (`extra="forbid"` kardeşi kural).
    """
    eksik = _govde()
    eksik.pop("short_work_pct")
    resp = await client.put("/payroll/rates/2027/company", json=eksik, headers=admin_headers)
    assert resp.status_code == 422


async def test_oran_yazimi_TEK_denetim_satiri_yazar(
    client, ik_headers, seeded_db, db_session, admin_headers
):
    once = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    await _put(client, admin_headers, 2027, "company")
    sonra = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert sonra == once + 1


async def test_okuma_denetim_YAZMAZ(client, ik_headers, oranlar, db_session):
    once = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    await client.get("/payroll/rates", headers=ik_headers)
    sonra = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert sonra == once


# --- 🔴 GEÇMİŞ DÖNEM DEĞİŞMEZ ----------------------------------------------


async def _onayli_donem(client, headers, donem) -> dict:
    """Dönemi hesaplar ve `approved`e getirir (iki adım, S8: atlama yok)."""
    await client.post(f"/payroll/periods/{donem.id}/compute", headers=headers)
    await client.post(f"/payroll/periods/{donem.id}/approve", headers=headers)
    await client.post(f"/payroll/periods/{donem.id}/approve", headers=headers)
    detay = (await client.get(f"/payroll/periods/{donem.id}", headers=headers)).json()
    assert detay["status"] == "approved"
    return detay["summary"]


async def test_ONAYLI_DONEMIN_YILINDA_oran_yazilamaz_409(
    client, ik_headers, donem, dort_tip, admin_headers
):
    """🔴 PARA KORKULUĞU — onaylanmış geçmiş oranla oynanarak değiştirilemez.

    Yazmaya izin verilseydi `total_employer_cost` ve `sgk_employer_total`
    (ve SGK özetinin TAMAMI) canlı orandan türediği için ONAYLANMIŞ dönemin
    raporlanmış sayıları geriye dönük değişirdi.
    """
    once = await _onayli_donem(client, ik_headers, donem)

    resp = await _put(client, admin_headers, YIL, "company", sgk_employer_pct="30.000")
    assert resp.status_code == 409, resp.text

    sonra = (await client.get(f"/payroll/periods/{donem.id}", headers=ik_headers)).json()["summary"]
    assert sonra == once


async def test_onayli_donemin_yilinda_YENI_TIP_de_acilamaz_409(
    client, ik_headers, donem, dort_tip, admin_headers
):
    """Kapı GÜNCELLEMEYE değil YILA kapanır.

    Yalnız var olan satırın güncellenmesi engellenseydi, oran satırı OLMAYAN bir
    tip için yeni set açmak o tipin satırlarını `unknown_cost_count`tan çıkarıp
    maliyet toplamına EKLERDİ — onaylı dönemin toplamı yine değişirdi.
    """
    once = await _onayli_donem(client, ik_headers, donem)
    resp = await _put(client, admin_headers, YIL, "general")
    assert resp.status_code == 409, resp.text
    sonra = (await client.get(f"/payroll/periods/{donem.id}", headers=ik_headers)).json()["summary"]
    assert sonra == once


async def test_TASLAK_donemin_yilinda_oran_yazilabilir(
    client, ik_headers, donem, dort_tip, admin_headers
):
    """Kural bordroyu TIKAMAZ: kilit yalnız `approved`/`paid` dönemle gelir."""
    await client.post(f"/payroll/periods/{donem.id}/compute", headers=ik_headers)
    resp = await _put(client, admin_headers, YIL, "company", sgk_employer_pct="30.000")
    assert resp.status_code == 200, resp.text


async def test_BASKA_yila_yazmak_serbesttir(client, ik_headers, donem, dort_tip, admin_headers):
    """2026 onaylı olsa da 2027 seti açılabilir — mevzuat değişimi engellenmez."""
    await _onayli_donem(client, ik_headers, donem)
    resp = await _put(client, admin_headers, 2027, "company")
    assert resp.status_code == 200, resp.text


async def test_odenmis_donemin_yilinda_da_yazilamaz_409(
    client, ik_headers, donem, dort_tip, admin_headers
):
    await _onayli_donem(client, ik_headers, donem)
    assert (
        await client.post(f"/payroll/periods/{donem.id}/pay", headers=ik_headers)
    ).status_code == 200
    resp = await _put(client, admin_headers, YIL, "company", sgk_employer_pct="30.000")
    assert resp.status_code == 409, resp.text


async def test_donemi_olmayan_yil_serbesttir(client, ik_headers, seeded_db, admin_headers):
    """Regresyon: kapı YILA bakar, hiç dönemi olmayan yılı kilitlemez."""
    assert (await _put(client, admin_headers, 2098, "company")).status_code == 200
