"""IZN-B3 ONARIM — GENEL rota tabanlı IDOR bekçileri (yazma + geçiş uçları dahil, TÜM `app.routes`).

`test_izn_b3_ekip_idor.py` yalnız GET'leri tarıyordu; onay/ret uçlarındaki KRİTİK açık (başka
projedeki ekip rolü ve ana rol Köprü hakedişini onaylıyordu) buradan kaçtı. Bu dosya HER yöntemi
(POST/PUT/PATCH/DELETE; approve/reject/mark-paid/reopen/submit/send/… geçişleri dahil) tarar.

Üç kişi, iki proje (Kule / Köprü):

* `p1`: ana rol `hicbiri` (hiçbir sayfa), Kule'de `guclu` (her sayfa Düzenler + Onaylar), Köprü'de
  ekipte DEĞİL.
* `p2`: ana rol `guclu`, Kule'de `guclu`, Köprü'de `zayif` (her sayfa yalnız Görür). ← KRİTİK
  senaryo: ana rol onay/yazma yetkili ama Köprü'deki PROJE rolü yetersiz.

Bekçiler:
1. KÖPRÜ KİMLİKLİ yazma/geçiş uçları: `p1`/`p2` hiçbirinde 403/404 dışında yanıt ALMAZ (2xx, 409 ve
   422 kapının/görünürlüğün GEÇİLDİĞİNİ gösterir ve KIRMIZIdır). Aynı uçlar Kule kimlikleriyle 403
   ALMAZ (pozitif kontrol; yalnız Sistem Yöneticisi'ne ait uçlar listede).
2. KAYIT SAVUNMASI: proje bağlamı çözücüsü KAPATILIR (kimliği çözülemeyen varlık gibi); `p2` ana
   rolüyle kapıyı geçer, Köprü'yü yalnız kapının bağlama KENDİ yazdığı çiftler + `visible_projects`
   korur. Kayıt kalkarsa onay/ret/yeniden aç 200/409 döner ya da `MissingGateContextError` atar.
3. ŞİRKET GENELİ uçlar: bağlamı çözülemeyen her kapılı rota, ana rolü yetersiz ama Kule'de güçlü
   `p1`'e 403 verir; yalnız `MULTI_PROJE_LISTELERI` (ana rol VEYA ekip rolü) ile kapısız uçlar
   açıktır.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import AsyncClient

from app.core.sayfalar import PageLevel
from app.main import app
from app.modules.projects import context as proje_baglami
from tests._ekip_dunyasi import EkipProje, proje_kur, rol_kur
from tests._proje_ekibi import ekibe_ekle

PASSWORD = "parola1234"
PARAM = re.compile(r"{(\w+)}")
YAZMA = ("POST", "PUT", "PATCH", "DELETE")

#: Hiç doldurulmayacak rotalar (oturum/sağlık).
ATLANAN = {"/auth/login", "/auth/refresh", "/auth/logout", "/auth/me", "/health"}

#: Çok proje LİSTE kapıları (ana rol VEYA ekip rolü; satırlar proje başına süzülür): `p1` ekip
#: rolüyle açar. Yeni bir `multi_project=True` uç eklenirse buraya yazılır; sayısı bekçilidir.
MULTI_PROJE_LISTELERI = {
    ("GET", "/projects"),
    ("GET", "/projects/timeline"),
    ("GET", "/sites"),
    ("GET", "/progress-payments"),
    ("GET", "/subcontractor-progress-payments"),
    ("GET", "/subcontractor-progress-payments/summary"),
    ("GET", "/contracts"),
    ("GET", "/subcontractor-contracts"),
    ("GET", "/documents"),
}

#: Kule kimlikleriyle de 403 verebilen (YALNIZ Sistem Yöneticisi / `admin` düzeyi) uçlar: pozitif
#: kontrol dışı. DELETE'ler SIL hattında yalnız Sistem Yöneticisi'dir (K4).
KULE_403_MESRU_ONEK = ("DELETE",)
#: Kapısı Görür (VIEW) olan POST'lar: `zayif` (Görür) Köprü'de de kapıyı GEÇER, kayıt görünürlüğü
#: servise kalır. (a) EV önizleme = VIEW kapılı hesap; (b) belge BAĞLAMA uçları kullanıcı kararıyla
#: `<sahip>:view` kapılıdır (2026-09-05, "gördüğün kayda yüklemeye yetkin varsa bağlarsın").
OKUMA_GIBI_POST = {
    ("POST", "/sites/{site_id}/earned-value/budget/preview"),
    ("POST", "/sections/{owner_id}/documents"),
    ("POST", "/subcontractor-contracts/{owner_id}/documents"),
    ("POST", "/units/{owner_id}/documents"),
}
KULE_403_MESRU_YOLLAR = {
    "/progress-payments/{payment_id}/unapprove",
    "/subcontractor-progress-payments/{payment_id}/unapprove",
}


def _kapi_var(dependant, gorulen: set[int] | None = None) -> bool:
    gorulen = gorulen if gorulen is not None else set()
    for sub in dependant.dependencies:
        if id(sub) in gorulen:
            continue
        gorulen.add(id(sub))
        fn = sub.call
        if hasattr(fn, "__code__"):
            serbest = set(fn.__code__.co_freevars)
            if serbest & {"module_key", "gates", "page_key", "page_keys"}:
                return True
            if fn.__qualname__.startswith("require_system_admin."):
                return True
        if _kapi_var(sub, gorulen):
            return True
    return False


def _proje_sayfali(dependant, gorulen: set[int] | None = None) -> bool:
    """Rotanın kapılarından biri PROJE İÇİ sayfa çifti taşıyor mu (kayıt savunması bu sınıfı sınar:
    şirket geneli sayfaya bağlı kapı bağlama çift yazmaz, proje rolü söz sahibi değildir)."""
    from app.core.gate_context import project_pairs
    from app.core.page_gate import gate_flags

    gorulen = gorulen if gorulen is not None else set()
    for sub in dependant.dependencies:
        if id(sub) in gorulen:
            continue
        gorulen.add(id(sub))
        fn = sub.call
        if hasattr(fn, "__code__"):
            hucreler = (x.cell_contents for x in fn.__closure__ or ())
            c = dict(zip(fn.__code__.co_freevars, hucreler, strict=True))
            ciftler: tuple = ()
            if "module_key" in c and "min_level" in c and "page_keys" not in c:
                ciftler = gate_flags(c["module_key"], c["min_level"])
            elif "gates" in c:
                ciftler = tuple(p for m, lv in c["gates"] for p in gate_flags(m, lv))
            elif "page_keys" in c and "flag" in c:
                ciftler = tuple((k, c["flag"]) for k in c["page_keys"])
            elif "page_key" in c and "flag" in c:
                ciftler = ((c["page_key"], c["flag"]),)
            if project_pairs(ciftler):
                return True
        if _proje_sayfali(sub, gorulen):
            return True
    return False


def _rotalar() -> list[tuple[str, str, bool]]:
    sonuc: list[tuple[str, str, bool]] = []
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute) or ctx.path in ATLANAN:
            continue
        kapi = _kapi_var(ctx.dependant or ctx.original_route.dependant)
        sonuc += [(m, ctx.path, kapi) for m in sorted(ctx.methods) if m != "OPTIONS"]
        if _proje_sayfali(ctx.dependant or ctx.original_route.dependant):
            PROJE_SAYFALI.update((m, ctx.path) for m in ctx.methods)
    return sorted(sonuc, key=lambda r: (r[1], r[0]))


PROJE_SAYFALI: set[tuple[str, str]] = set()


def _onek(yol: str) -> str:
    return "/" + yol.lstrip("/").split("/", 1)[0]


def _url(yol: str, proje: EkipProje | None) -> tuple[str, bool]:
    """Yolu doldurur. Döner: (url, bir parametre `proje`nin GERÇEK varlığıyla dolduruldu mu)."""
    dolu = False

    def doldur(m: re.Match[str]) -> str:
        nonlocal dolu
        ad = m.group(1)
        if proje is not None and (_onek(yol), ad) in proje.doldurma:
            dolu = True
            return proje.doldurma[(_onek(yol), ad)]
        if ad == "day":
            return "2026-05-04"
        if ad == "rev_no":
            return "1"
        return str(uuid.uuid4())

    return PARAM.sub(doldur, yol), dolu


class Dunya:
    kule: EkipProje
    kopru: EkipProje
    p1: dict[str, str]
    p2: dict[str, str]


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def dunya(client: AsyncClient, seeded_db, user_factory, project_factory) -> Dunya:
    from app.modules.roles import seed_data

    await seed_data.seed_izn_reference_data(seeded_db)
    hicbiri = await rol_kur(seeded_db, "izn_hicbiri", PageLevel.none)
    guclu = await rol_kur(seeded_db, "izn_guclu", PageLevel.edit, approve=True)
    zayif = await rol_kur(seeded_db, "izn_zayif", PageLevel.view)
    p1 = await user_factory(email="p1@izn.co", password=PASSWORD, role_key="izn_hicbiri")
    p2 = await user_factory(email="p2@izn.co", password=PASSWORD, role_key="izn_guclu")
    d = Dunya()
    d.kule = await proje_kur(seeded_db, project_factory, "KULE", "Kule", p2.id)
    d.kopru = await proje_kur(seeded_db, project_factory, "KOPRU", "Köprü", p2.id)
    await ekibe_ekle(seeded_db, p1, d.kule.project.id, guclu.id)
    await ekibe_ekle(seeded_db, p2, d.kule.project.id, guclu.id)
    await ekibe_ekle(seeded_db, p2, d.kopru.project.id, zayif.id)
    assert hicbiri.id
    d.p1, d.p2 = await _giris(client, "p1@izn.co"), await _giris(client, "p2@izn.co")
    seeded_db.expunge_all()  # ortak test oturumu: gerçek istekte oturum ayrıdır
    return d


async def _istek(client: AsyncClient, yontem: str, url: str, baslik: dict[str, str]) -> int:
    govde = {"json": {}} if yontem in ("POST", "PUT", "PATCH") else {}
    try:
        return (await client.request(yontem, url, headers=baslik, **govde)).status_code
    except Exception as hata:  # noqa: BLE001 — 500/atılan istisna da bir İHLALdir, raporlanır
        return -1000 - hash(type(hata).__name__) % 1000


def test_tarama_bos_degil_ve_multi_liste_bekcisi() -> None:
    rotalar = _rotalar()
    assert len(rotalar) > 400, len(rotalar)
    mevcut = {(m, p) for m, p, _ in rotalar}
    assert MULTI_PROJE_LISTELERI <= mevcut, MULTI_PROJE_LISTELERI - mevcut


async def test_kopru_kimlikli_her_yazma_ve_gecis_ucu_kule_ekibine_kapali(
    client: AsyncClient, dunya: Dunya
) -> None:
    ihlal: list[str] = []
    kopru_rota = pozitif = 0
    for yontem, yol, _kapi in _rotalar():
        kopru_url, kopru_dolu = _url(yol, dunya.kopru)
        if not kopru_dolu:
            continue
        kopru_rota += 1
        kule_url, _ = _url(yol, dunya.kule)
        for ad, baslik in (("p1", dunya.p1), ("p2", dunya.p2)):
            if ad == "p2" and yontem == "GET":
                continue  # p2 Köprü'de ekipte ve Görür: okuma meşru
            durum = await _istek(client, yontem, kopru_url, baslik)
            if (yontem, yol) in OKUMA_GIBI_POST and ad == "p2":
                continue
            if durum not in (403, 404):
                ihlal.append(f"{ad} {yontem} {yol} → Köprü {durum}")
            if yontem == "GET":
                continue
            kule = await _istek(client, yontem, kule_url, baslik)
            mesru = yontem in KULE_403_MESRU_ONEK or yol in KULE_403_MESRU_YOLLAR
            if kule == 403 and not mesru:
                ihlal.append(f"{ad} {yontem} {yol} → Kule'de de 403 (pozitif kontrol kırık)")
            if kule != 403:
                pozitif += 1
    assert kopru_rota > 100, kopru_rota
    assert pozitif > 100, pozitif
    assert not ihlal, "\n".join(ihlal)


async def test_kayit_savunmasi_cozucu_kapaliyken_ana_rol_kopruyu_acamaz(
    client: AsyncClient, dunya: Dunya, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Çözücü KAPALI (kimliği çözülemeyen varlık): `p2` ana rolüyle kapıyı geçer; Köprü'yü yalnız
    kapının bağlama yazdığı çiftler + `visible_projects` korur. 422 = gövde doğrulaması (kapı geçti,
    görünürlük sorulmadı) → karar VERİLEMEZ; 2xx/409/5xx = SIZINTI."""
    monkeypatch.setattr(proje_baglami, "RESOLVERS", {})
    monkeypatch.setattr(proje_baglami, "BODY_RESOLVERS", {})
    ihlal: list[str] = []
    savunulan = 0
    for yontem, yol, _kapi in _rotalar():
        if yontem not in YAZMA or (yontem, yol) not in PROJE_SAYFALI:
            continue
        kopru_url, kopru_dolu = _url(yol, dunya.kopru)
        if not kopru_dolu:
            continue
        durum = await _istek(client, yontem, kopru_url, dunya.p2)
        if (yontem, yol) in OKUMA_GIBI_POST:
            continue
        savunulan += 1
        if durum not in (403, 404, 422):
            ihlal.append(f"p2 {yontem} {yol} → {durum}")
    assert savunulan > 40, savunulan
    assert not ihlal, "\n".join(ihlal)


async def test_sirket_geneli_uclar_ekip_rolu_ile_acilmaz(client: AsyncClient, dunya: Dunya) -> None:
    """Bağlamı çözülemeyen (şirket geneli) kapılı rota: ana rolü yetersiz ama Kule'de güçlü kişiye
    403. İstisna YALNIZ `MULTI_PROJE_LISTELERI` (açılması beklenir) ve kapısız uçlardır."""
    ihlal: list[str] = []
    acilan_multi = 0
    for yontem, yol, kapi in _rotalar():
        url, dolu = _url(yol, None)  # tüm parametreler rastgele: hiçbir proje bağlamı çözülmez
        assert not dolu
        durum = await _istek(client, yontem, url, dunya.p1)
        if (yontem, yol) in MULTI_PROJE_LISTELERI:
            acilan_multi += durum != 403
            continue
        if kapi and durum != 403:
            ihlal.append(f"p1 {yontem} {yol} → {durum} (kapılı uç ekip rolüyle açıldı)")
    assert acilan_multi == len(MULTI_PROJE_LISTELERI), acilan_multi
    assert not ihlal, "\n".join(ihlal)
