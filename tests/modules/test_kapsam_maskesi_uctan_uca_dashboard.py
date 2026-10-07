"""Kapsam maskesi UÇTAN UCA — `dashboard`. Gerçek rol, gerçek uç, gerçek yanıt.

## Neden bu dosya (ve kardeşi `…_sites.py`) SONRADAN yazıldı

`test_kapsam_maskesi_uctan_uca.py` aynı soruyu `boq` için soruyor. Kısıtlı ALTI
modülün (boq · contracts · dashboard · projects · sales · sites) İKİSİ —
`dashboard` ve `sites` — 2026-09-20'ye kadar hiçbir davranış bekçisine
düşmemişti. Ölçüldü: dört dashboard test dosyasında `Scope.limited` ya da
`Scope.finance` ile koşan TEK test yoktu; dahası iki test kapsamı BİLEREK
`Scope.all`a çekerek maskeyi deneyin dışında bırakıyordu.

Parçalar (maske · rota bağı · sınıflandırma) ayrı ayrı yeşilken zincirin bu
ucunun kopuk olması mümkündü. Bu dosya o boşluğu kapatır.

## Ölçülen roller (matristen, tohum DEĞİŞTİRİLMEDEN)

* `site_chief` → `dashboard = view/limited` → PARA gizli, ilerleme görünür
* `accounting` → `dashboard = view/finance` → ilerleme gizli, PARA görünür
* `system_admin` → `dashboard = admin/all` → POZİTİF KONTROL

İkisi birbirinin AYNASIDIR: tek yönlü bir test "her şeyi gizle" hâlini
yakalayamazdı.

## 🔴 ZARF alanları `None`a ÇEKİLMEZ, `kisitli()` olur

`portfolio`/`receivables`/`average_margin` çıplak `Decimal` değil
`MetricPlaceholder`dır. Maske onlara `None` yazsaydı ŞEMA KIRILIRDI (alan zorunlu
bir nesnedir). Bunun yerine zarfın ÜÇÜNCÜ hâli üretilir
(`available=false`, `pending_module=null` — *"rolün izni yok"*, kullanıcı kararı
2026-08-27). Aşağıda bu ayrım ayrıca ölçülür; yalnız `value is None` diyen bir
test, zarfın ikinci hâliyle ("modül henüz bağlanmadı") üçüncüsünü karıştırırdı.
"""

from decimal import Decimal

import pytest

from app.core.access import AccessLevel
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from ._boq import _auth, _login_with_access

_BUTCE = Decimal("12500000.00")
_ILERLEME = Decimal("37.50")


@pytest.fixture
async def dashboard_projesi(project_factory):
    return await project_factory(
        "DASH-KAPSAM", budget=str(_BUTCE), progress_pct=str(_ILERLEME), status="active"
    )


async def _panel(client, db_session, user_factory, rol: str, eposta: str) -> dict:
    token = await _login_with_access(client, db_session, user_factory, rol, eposta)
    resp = await client.get("/dashboard/summary", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _kart(govde: dict) -> dict:
    kartlar = [k for k in govde["projects"] if k["code"] == "DASH-KAPSAM"]
    assert kartlar, f"proje kartı panelde YOK: {[k['code'] for k in govde['projects']]}"
    return kartlar[0]


async def test_ADMIN_hem_PARAYI_hem_ILERLEMEYI_gorur(
    client, db_session, user_factory, dashboard_projesi
):
    """🔴 POZİTİF KONTROL — maske "herkesten gizle" hâline gelirse burası kırmızı."""
    govde = await _panel(client, db_session, user_factory, "system_admin", "adm@dash.co")
    kart = _kart(govde)

    assert kart["budget"] == "12500000.00"
    assert kart["progress_pct"] == "37.50"
    assert govde["portfolio"]["available"] is True, "portföy kartı dolu gelmeliydi"
    assert govde["active_project_count"] >= 1


async def test_SITE_CHIEF_limited_PARAYI_goremez_ILERLEMEYI_gorur(
    client, db_session, user_factory, dashboard_projesi
):
    """`dashboard = view/limited`."""
    govde = await _panel(client, db_session, user_factory, "site_chief", "sc@dash.co")
    kart = _kart(govde)

    assert kart["budget"] is None, "BÜTÇE SIZDI"
    assert govde["portfolio"]["value"] is None, "PORTFÖY TUTARI SIZDI"
    # kimlik ve operasyonel DURUR — panel kullanılabilir kalmalıdır
    assert kart["progress_pct"] == "37.50", "ilerleme yanlışlıkla gizlendi"
    assert kart["name"] == "Test Proje", "KİMLİK gizlendi"
    assert kart["status"] == "active"


async def test_ACCOUNTING_finance_ILERLEMEYI_ve_PARAYI_gorur(
    client, db_session, user_factory, dashboard_projesi
):
    """IZN-B4: eski `finance` kapsamı karşılıksız (IZN-PLAN §3, GECE KARARI) — `accounting`
    rolünün gizli bayrağı yok, ilerleme DE para DA görünür (ilerleme hiçbir kategoride değil)."""
    govde = await _panel(client, db_session, user_factory, "accounting", "acc@dash.co")
    kart = _kart(govde)

    assert kart["progress_pct"] == "37.50", "ilerleme yanlışlıkla gizlendi"
    assert kart["budget"] == "12500000.00", "PARA YANLIŞLIKLA GİZLENDİ"
    assert kart["name"] == "Test Proje", "KİMLİK gizlendi"
    assert govde["portfolio"]["available"] is True, "portföy gizlenmemeliydi"


async def test_LIMITED_rolde_ZARF_ucuncu_hale_duser_NONE_olmaz(
    client, db_session, user_factory, dashboard_projesi
):
    """🔴 `MetricPlaceholder` zarfı `None`a çekilseydi yanıt şema doğrulamasında
    patlardı (alan zorunlu bir NESNE). Maske bunun yerine `kisitli()` çağırır ve
    zarfın ÜÇÜNCÜ hâlini üretir.

    Ayrım ÖLÇÜLÜR: `pending_module is None` + `available is False` = *"rolün izni
    yok"*. `pending_module` DOLU olsaydı ekran *"modül henüz bağlanmadı"* derdi ve
    bu, kullanıcıya YALAN söylemek olurdu (kullanıcı kararı 2026-08-27).
    """
    govde = await _panel(client, db_session, user_factory, "site_chief", "sc2@dash.co")

    for ad in ("portfolio", "receivables", "average_margin"):
        zarf = govde[ad]
        assert isinstance(zarf, dict), f"`{ad}` zarfı `None`a çekilmiş: {zarf!r}"
        assert zarf["available"] is False, f"`{ad}` maskeli rolde dolu geldi"
        assert zarf["value"] is None, f"`{ad}` tutarı SIZDI"
        assert zarf["pending_module"] is None, (
            f"`{ad}` üçüncü hâl yerine 'modül bağlanmadı' hâlinde döndü; ekran "
            "izin eksikliğini eksik modül sanardı"
        )


async def test_SAYACLAR_ve_RISK_metinleri_her_kapsamda_DURUR(
    client, db_session, user_factory, dashboard_projesi
):
    """🔴 Maske ALAN düzeyindedir; sayaçlar ve serbest metin kovası DIŞINDADIR.

    Bu bir onay değil, ÖLÇÜLMÜŞ BİR SINIRDIR ve burada çivilenir: bir gün
    sayaçlar `operasyonel` kovasına alınırsa (ki bu bir ürün kararıdır — kovanın
    tanımı *"metraj · ilerleme · SAYAÇ · saha notu"* der) bu test kırmızı verir
    ve karar bilinçli alınmış olur.
    """
    limited = await _panel(client, db_session, user_factory, "site_chief", "sc3@dash.co")
    finance = await _panel(client, db_session, user_factory, "accounting", "acc3@dash.co")

    for govde in (limited, finance):
        assert isinstance(govde["active_project_count"], int)
        assert isinstance(govde["pending_approvals"]["count"], int)
        assert govde["role_name"], "rol adı (kimlik) gizlenmemeli"


# --------------------------------------------------------------------------- #
# ÇAPRAZ KAPSAM — panel, BAŞKA modülün verisini kimin kapsamıyla maskeler?
# --------------------------------------------------------------------------- #
#
# 🔴 ÖLÇÜLMÜŞ TUTARSIZLIK (2026-09-20). `DashboardProjectCard` `budget` (para) ve
# `progress_pct` (operasyonel) taşır; AYNI VERİ `GET /projects` ucundan da çıkar
# ve orada rolün **`projects`** kapsamıyla maskelenir. Panelde ise rota sınıfı
# `kapsam_rotasi("dashboard", …)`dır, yani **`dashboard`** kapsamı geçerlidir.
#
# Modülün kendi docstring'i (K4) bu yan kapıyı SEVİYE ekseninde zaten kapatmıştı:
# kart yalnız `can_read(projects)` doğruysa doldurulur, çünkü *"`projects`
# hücresi `none` yapılmış bir rol şirketin proje BÜTÇELERİNİ panelden okumaya
# devam ederdi"*. KAPSAM ekseni o gün açık kalmıştı — aynı cümle `limited` için
# de geçerlidir: `projects = view/limited` + `dashboard = full/all` olan bir rol
# projeler ekranında göremediği bütçeyi panelden okurdu.
#
# Tohumda iki satır BİREBİR aynı olduğu için bugün sızıntı YOKTUR; kapatılan şey
# bir yapılandırma hâlidir (matris ekrandan hücre hücre değiştirilebilir).
# Çözüm fail-CLOSED'dır: alan, İKİ kapsamdan HERHANGİ BİRİ gizliyorsa düşer.


async def _kapsamli_panel(client, db_session, user_factory, eposta: str, gizli: bool):
    token = await _login_with_access(client, db_session, user_factory, "project_manager", eposta)
    await modul_duzeyi_yaz(
        db_session,
        "project_manager",
        "projects",
        AccessLevel.view,
        tum_tutarlar=gizli,
    )
    resp = await client.get("/dashboard/summary", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_PANEL_proje_kartini_PROJECTS_kapsamiyla_da_maskeler(
    client, db_session, user_factory, dashboard_projesi
):
    """`projects = view/limited` + `dashboard = full/all` → BÜTÇE DÜŞER."""
    govde = await _kapsamli_panel(client, db_session, user_factory, "cpr@dash.co", True)
    kart = _kart(govde)

    assert kart["budget"] is None, (
        "Panel, `projects` kapsamı PARAYI gizlerken bütçeyi gösterdi — projeler "
        "ekranında görünmeyen tutar panelden okunuyor"
    )
    assert kart["progress_pct"] == "37.50", "operasyonel alan yanlışlıkla gizlendi"


async def test_POZITIF_KONTROL_PROJECTS_all_iken_kart_DOLU(
    client, db_session, user_factory, dashboard_projesi
):
    """🔴 Bu olmadan üstteki ikisi "kartı hep boşalt" hâlinde de yeşil kalırdı."""
    govde = await _kapsamli_panel(client, db_session, user_factory, "cpa@dash.co", False)
    kart = _kart(govde)

    assert kart["budget"] == "12500000.00"
    assert kart["progress_pct"] == "37.50"
