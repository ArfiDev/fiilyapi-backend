"""Kapsam maskesi UÇTAN UCA — `sites`. Gerçek rol, gerçek uç, gerçek yanıt.

Kardeşi `…_dashboard.py`; gerekçe orada yazılı. Kısa hâli: kısıtlı altı modülün
İKİSİ (`dashboard`, `sites`) hiçbir davranış bekçisine düşmemişti. `sites`te
ayrıca `tests/modules/sites/` altındaki yardımcı kapsamı **varsayılan olarak
`Scope.all`a sıfırlıyordu** (`_set_permission(..., scope: Scope = Scope.all)`),
yani 11 ucun hiçbiri gerçek kısıtlı kapsamla koşmuyordu.

## Bu dosya ÜÇ yüzeyi birden ölçer — ve bu bilinçlidir

`sites` tek bir ekran değil: liste kartı (`SiteCard`), detay
(`SiteDetailResponse`) ve bölüm listesi (`SectionResponse`) AYRI şemalardır ve
maskeye AYRI yollardan girerler. Yalnız birine bakan bir test, ötekinde
unutulmuş bir alanı görmezdi — `contracts` sızıntısı tam olarak böyle
bulunmuştu (ekran maskeliyken gömülü özet açıktaydı).

## Üç ALAN TİPİ, üç FARKLI maskeleme davranışı

| tip | örnek | maskelenince |
|---|---|---|
| çıplak `Decimal \\| None` | `SiteCard.budget` (BLF-B1: bölüm `budget_amount` kalktı) | `null` |
| zarf (`MetricPlaceholder`) | `SectionResponse.budget`, `…progress_pct` | `kisitli()` üçüncü hâl |
| sayaç (`CountPlaceholder`) | `worker_count`, `boq_item_count` | ETİKETSİZ → DEĞİŞMEZ |

Üçü de ayrı ölçülür; yalnız birine bakan bir bekçi ötekilerin sessizce
bozulmasına izin verirdi.
"""

from decimal import Decimal

import pytest

from app.modules.sites.models import Section

from ._boq import _auth, _login_with_access, _site

_BUTCE = Decimal("8400000.00")
_ARSA_M2 = Decimal("5200.00")


@pytest.fixture
async def santiye_ve_bolum(db_session, project_factory):
    project = await project_factory("SITE-KAPSAM")
    site = await _site(
        db_session,
        project,
        code="SK-01",
        budget=_BUTCE,
        land_area_m2=_ARSA_M2,
        construction_area_m2=Decimal("3100.00"),
    )
    section = Section(
        site_id=site.id,
        code="B-1",
        name="A Blok",
        planned_worker_count=12,
    )
    db_session.add(section)
    await db_session.flush()
    return project, site, section


async def _basliklar(client, db_session, user_factory, rol: str, eposta: str) -> dict[str, str]:
    return _auth(await _login_with_access(client, db_session, user_factory, rol, eposta))


async def _uc_yuzey(client, basliklar, project, site) -> tuple[dict, dict, dict]:
    """Liste kartı · detay · bölüm satırı — üçü de TEK rolle okunur."""
    liste = await client.get(f"/projects/{project.id}/sites", headers=basliklar)
    assert liste.status_code == 200, liste.text
    detay = await client.get(f"/sites/{site.id}", headers=basliklar)
    assert detay.status_code == 200, detay.text
    bolumler = await client.get(f"/sites/{site.id}/sections", headers=basliklar)
    assert bolumler.status_code == 200, bolumler.text

    kart = next(s for s in liste.json()["items"] if s["code"] == "SK-01")
    return kart, detay.json(), bolumler.json()["items"][0]


async def test_ADMIN_uc_yuzeyde_de_her_seyi_gorur(
    client, db_session, user_factory, santiye_ve_bolum
):
    """🔴 POZİTİF KONTROL — maske "herkesten gizle" hâline gelirse burası kırmızı."""
    project, site, _section = santiye_ve_bolum
    basliklar = await _basliklar(client, db_session, user_factory, "system_admin", "adm@site.co")

    kart, detay, bolum = await _uc_yuzey(client, basliklar, project, site)

    assert kart["budget"] == "8400000.00"
    assert kart["land_area_m2"] == "5200.00"
    assert detay["budget"] == "8400000.00"
    assert "budget_amount" not in bolum, "BLF-B1: eski elle bedel yanıttan kalktı"


async def test_SITE_CHIEF_limited_PARAYI_goremez_ALANI_gorur(
    client, db_session, user_factory, santiye_ve_bolum
):
    """`sites = view/limited` — para gizli, metraj/alan görünür."""
    project, site, _section = santiye_ve_bolum
    basliklar = await _basliklar(client, db_session, user_factory, "site_chief", "sc@site.co")

    kart, detay, bolum = await _uc_yuzey(client, basliklar, project, site)

    assert kart["budget"] is None, "ŞANTİYE BÜTÇESİ SIZDI (liste)"
    assert detay["budget"] is None, "ŞANTİYE BÜTÇESİ SIZDI (detay)"
    assert "budget_amount" not in bolum, "BLF-B1: eski elle bedel yanıttan kalktı"
    assert bolum["budget"]["value"] is None, "BÖLÜM BOQ BÜTÇESİ SIZDI (zarf)"
    assert detay["total_progress_payment"]["value"] is None, "HAKEDİŞ TOPLAMI SIZDI"
    assert detay["contract_amount"]["value"] is None, "SÖZLEŞME BEDELİ SIZDI"
    # operasyonel ve kimlik DURUR
    assert kart["land_area_m2"] == "5200.00", "arsa alanı yanlışlıkla gizlendi"
    assert kart["construction_area_m2"] == "3100.00"
    assert kart["name"] == "A-Blok Şantiyesi", "KİMLİK gizlendi"
    assert bolum["planned_worker_count"] == 12, "SAYAÇ gizlendi"


async def test_ACCOUNTING_finance_ALANI_goremez_PARAYI_gorur(
    client, db_session, user_factory, santiye_ve_bolum
):
    """`sites = view/finance` — `limited`in AYNASI."""
    project, site, _section = santiye_ve_bolum
    basliklar = await _basliklar(client, db_session, user_factory, "accounting", "acc@site.co")

    kart, detay, bolum = await _uc_yuzey(client, basliklar, project, site)

    assert kart["land_area_m2"] is None, "ARSA ALANI SIZDI"
    assert kart["construction_area_m2"] is None, "İNŞAAT ALANI SIZDI"
    assert kart["progress_pct"]["value"] is None, "İLERLEME SIZDI (zarf)"
    assert bolum["progress_pct"]["value"] is None, "BÖLÜM İLERLEMESİ SIZDI"
    # PARA muhasebenin işidir — gizlenmesi ekranı kullanılamaz yapardı
    assert kart["budget"] == "8400000.00", "PARA YANLIŞLIKLA GİZLENDİ"
    assert "budget_amount" not in bolum
    assert bolum["budget"]["value"] is not None, "BÖLÜM TÜREV BEDELİ YANLIŞLIKLA GİZLENDİ"
    assert kart["name"] == "A-Blok Şantiyesi", "KİMLİK gizlendi"


async def test_ZARFLI_alan_NONE_olmaz_UCUNCU_hale_duser(
    client, db_session, user_factory, santiye_ve_bolum
):
    """🔴 Zarf `None`a çekilseydi yanıt doğrulaması patlardı; bunun yerine
    `kisitli()` üçüncü hâli üretilir (*"rolün izni yok"*).

    `pending_module is None` AYRICA ölçülür: dolu olsaydı ekran *"modül henüz
    bağlanmadı"* derdi ve bu, izin eksikliğini eksik modül gibi göstermek olurdu.
    """
    project, site, _section = santiye_ve_bolum
    basliklar = await _basliklar(client, db_session, user_factory, "site_chief", "sc2@site.co")

    _kart, detay, bolum = await _uc_yuzey(client, basliklar, project, site)

    for ad, zarf in (
        ("detay.total_progress_payment", detay["total_progress_payment"]),
        ("detay.contract_amount", detay["contract_amount"]),
        ("bolum.budget", bolum["budget"]),
    ):
        assert isinstance(zarf, dict), f"`{ad}` zarfı `None`a çekilmiş"
        assert zarf["available"] is False, f"`{ad}` maskeli rolde dolu geldi"
        assert zarf["pending_module"] is None, (
            f"`{ad}` üçüncü hâl yerine 'modül bağlanmadı' hâlinde döndü"
        )


async def test_LISTE_TOPLAMLARI_da_maskelenir(client, db_session, user_factory, santiye_ve_bolum):
    """🔴 Toplam satırı, satırların maskelenmesinden BAĞIMSIZ bir alandır.

    `boq`da tam bu ayrım bir kusur doğurmuştu: kalem tutarları türev olarak
    düşerken `grand_total` DÜZ bir alan olduğu için gerçek kalıyordu ve ekran
    TUTARSIZ oluyordu. Aynı soru burada da sorulur.
    """
    project, _site, _section = santiye_ve_bolum
    basliklar = await _basliklar(client, db_session, user_factory, "site_chief", "sc3@site.co")

    liste = await client.get(f"/projects/{project.id}/sites", headers=basliklar)
    assert liste.status_code == 200, liste.text
    toplamlar = liste.json()["totals"]

    assert toplamlar["total_progress_payment"]["value"] is None, "TOPLAM HAKEDİŞ SIZDI"
    assert toplamlar["average_margin"]["value"] is None, "ORTALAMA MARJ SIZDI"
