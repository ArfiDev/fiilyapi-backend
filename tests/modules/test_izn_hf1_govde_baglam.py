"""IZN-HF1 — istek bağlamının gövdeden manipülasyonu ÇEKİRDEKTE kapalı (`projects.context`).

İki kural: (1) gövdeden bağlam YALNIZ rotanın gövde modeli anahtarı bildiriyorsa (B4c onarımı),
(2) TAŞIMA: yol çözücüsü eşleştiyse gövde bağlamı DEĞİŞTİREMEZ (P ≠ Q / projesiz kayıt → `None`).
Aşağıda önce B4c'den gelen üç istismar + pozitif kontrol + birim testler, sonra `RESOLVERS` ve rota
tablosundan OTOMATİK KEŞFEDİLEN saldırı matrisi (A/B/C sınıfları) ve uçtan uca taşıma senaryoları.

HF1 çürütme onarımları: (1) içerik türü FastAPI'nin kuralıyla çözülür (`application/JSON`,
`+json`, parametreli); bağlam anahtarı bildiren rota için JSON okunamayan gövde → `None`;
(2) açık `"project_id": null` P doluyken taşımadır → `None`; (3) C sınıfı C-POST (oluşturma) ve
C-PATCH (BİLİNEN BORÇ: çözücüsüz PATCH/PUT, açık beyaz liste) diye ayrıldı.

IZN-B4c onarımı — gövdeden proje bağlamı YALNIZ rotanın gövde modeli anahtarı bildiriyorsa.

Çürütme bulgusu: `_body_project` ham JSON'daki `project_id`/`site_id`'yi, ucun gövde modeli o alanı
TAŞIMASA bile okuyordu. Pydantic fazlalığı yok sayar ama bağlam değişirdi: ana rolü `maas_kisisel`
gizleyen, X projesinde ekip rolü `hr_manager` olan kişi `PATCH /personnel/{id}` gövdesine
`"project_id": X` ekleyip maskeyi, yazma kapısını ve (approve ucunda) izin kapısını atlatıyordu.

(a) üç istismar kapalı · (b) POZİTİF KONTROL: gövde modeli `project_id` bildiren gerçek ucun
(satınalma talebi oluşturma) bağlamı HÂLÂ gövdeden çözülür · (c) birim: model alanı bildirmiyorsa
`_body_project` `None`.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi import Body, FastAPI, Request, params
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel
from sqlalchemy import select

from app.core.sayfalar import PageLevel
from app.main import app
from app.modules.contracts.models import SubcontractorContract
from app.modules.personnel.models import Personnel, WorkerSource
from app.modules.projects import context as baglam
from app.modules.projects.context import (
    BODY_KEYS,
    RESOLVERS,
    _body_declared_keys,
    _body_project,
    request_project,
)
from app.modules.roles import seed_data
from app.modules.roles.models import Role
from tests._ekip_dunyasi import proje_kur, rol_kur
from tests._proje_ekibi import ekibe_ekle

PASSWORD = "parola1234"
GECERLI_IBAN = "TR330006100519786457841326"
ESKI_IBAN = "TR000000000000000000000000"


@pytest.fixture
async def iki_proje_ekip(seeded_db, user_factory, project_factory):
    await seed_data.seed_izn_reference_data(seeded_db)
    olusturan = await user_factory(email="olusturan@hf1.co", password=PASSWORD, role_key="patron")
    kule = await proje_kur(seeded_db, project_factory, "KULE", "Kule", olusturan.id)
    kopru = await proje_kur(seeded_db, project_factory, "KOPRU", "Köprü", olusturan.id)
    return kule, kopru


async def _giris(client, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _rol(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


@pytest.fixture
async def istismarci(seeded_db, user_factory, project_factory, client):
    """Ana rol her sayfayı yalnız GÖRÜR (düzenlemez, onaylamaz) · X projesinde ekip rolü
    `hr_manager` (personel/bordro düzenler + onaylar)."""
    await seed_data.seed_izn_reference_data(seeded_db)
    proje = await project_factory("GB-X", name="X Projesi")
    await rol_kur(seeded_db, "ana_gorur", PageLevel.view)
    kisi = await user_factory(email="istismar@gb.co", password=PASSWORD, role_key="ana_gorur")
    await ekibe_ekle(seeded_db, kisi, proje.id, (await _rol(seeded_db, "hr_manager")).id)
    personel = Personnel(
        full_name="Ayşe Demir", source=WorkerSource.company, iban=ESKI_IBAN, is_draft=True
    )
    seeded_db.add(personel)
    await seeded_db.flush()
    baslik = await _giris(client, "istismar@gb.co")
    return proje, personel, baslik


# --- (a) istismarlar kapalı (yalnız durum kodu + yazılmadı: B4c etiketlerine bağlı DEĞİL) ----


async def test_govdeye_eklenen_project_id_yazma_iznini_YUKSELTMEZ(
    client, db_session, istismarci
) -> None:
    """Ana rol personeli yalnız görür → PATCH 403. Gövdeye `project_id` (modelde YOK) eklemek
    ekip rolünün düzenleme iznini açmamalı; veri yazılmamalı."""
    proje, personel, baslik = istismarci
    yol = f"/personnel/{personel.id}"
    govdesiz = await client.patch(yol, json={"iban": GECERLI_IBAN}, headers=baslik)
    assert govdesiz.status_code == 403, govdesiz.text
    eklemeli = await client.patch(
        yol, json={"iban": GECERLI_IBAN, "project_id": str(proje.id)}, headers=baslik
    )
    assert eklemeli.status_code == 403, eklemeli.text
    await db_session.refresh(personel)
    assert personel.iban == ESKI_IBAN


async def test_govdeye_eklenen_project_id_onay_iznini_YUKSELTMEZ(client, istismarci) -> None:
    """Gövdesiz approve 403; gövdeye `project_id` eklemek ekip rolünü (payroll onay) açmamalı.
    (Satır yok: kapı geçilseydi 404 dönerdi.)"""
    proje, _personel, baslik = istismarci
    satir = uuid.uuid4()
    govdesiz = await client.post(f"/payroll/lines/{satir}/approve", headers=baslik)
    assert govdesiz.status_code == 403, govdesiz.text
    eklemeli = await client.post(
        f"/payroll/lines/{satir}/approve", json={"project_id": str(proje.id)}, headers=baslik
    )
    assert eklemeli.status_code == 403, eklemeli.text


# --- (b) pozitif kontrol: model alanı bildiren uç gövdeden çözer ----------------------------


async def test_govde_modeli_project_id_bildiren_uc_baglami_govdeden_cozer(
    client, seeded_db, user_factory, project_factory
) -> None:
    """`PurchaseRequestCreate.project_id` bildirilir: ana rol Görmez, X'te ekip rolü Düzenler →
    gövdedeki X ile 201; ekipte OLUNMAYAN Y ile 403 (bağlam gövdeden çözüldü, ana role düştü)."""
    x = await project_factory("GB-PX", name="X")
    y = await project_factory("GB-PY", name="Y")
    await rol_kur(seeded_db, "ana_hicbiri", PageLevel.none)
    ekip_rolu = await rol_kur(seeded_db, "ekip_duzenler", PageLevel.edit)
    kisi = await user_factory(email="talep@gb.co", password=PASSWORD, role_key="ana_hicbiri")
    await ekibe_ekle(seeded_db, kisi, x.id, ekip_rolu.id)
    baslik = await _giris(client, "talep@gb.co")

    x_yanit = await client.post(
        "/purchase-requests", json={"project_id": str(x.id)}, headers=baslik
    )
    assert x_yanit.status_code == 201, x_yanit.text
    y_yanit = await client.post(
        "/purchase-requests", json={"project_id": str(y.id)}, headers=baslik
    )
    assert y_yanit.status_code == 403, y_yanit.text


# --- (c) birim: `_body_project` ---------------------------------------------------------------


class _ModelBildirmez(BaseModel):
    name: str | None = None


class _ModelBildirir(BaseModel):
    project_id: uuid.UUID | None = None


def _istek(endpoint, govde: object, yol: str = "/x", *, rota: bool = True) -> Request:
    uygulama = FastAPI()
    uygulama.add_api_route(yol, endpoint, methods=["POST"])
    route = next(r for r in uygulama.routes if isinstance(r, APIRoute))
    ham = json.dumps(govde).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": ham, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": yol,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    if rota:
        scope["route"] = route
    return Request(scope, receive)


async def _a(veri: _ModelBildirmez) -> None: ...
async def _b(veri: _ModelBildirir) -> None: ...
async def _c(veri: _ModelBildirir | None = None) -> None: ...
async def _d(veri: dict) -> None: ...
async def _e() -> None: ...
async def _f(project_id: uuid.UUID = Body(), veri: int = Body()) -> None: ...  # noqa: B008
async def _g(veri: _ModelBildirmez, ek: int = Body()) -> None: ...  # noqa: B008


async def test_model_alani_bildirmiyorsa_govde_project_id_yok_sayilir() -> None:
    proje = str(uuid.uuid4())
    istek = _istek(_a, {"name": "x", "project_id": proje})
    assert await _body_project(None, istek) is None  # type: ignore[arg-type]


async def test_model_alani_bildiriyorsa_govdeden_cozulur() -> None:
    proje = uuid.uuid4()
    for uc in (_b, _c):
        istek = _istek(uc, {"project_id": str(proje)})
        assert await _body_project(None, istek) == proje, uc  # type: ignore[arg-type]


async def test_govde_parametresi_yoksa_ya_da_model_degilse_none() -> None:
    proje = str(uuid.uuid4())
    for uc in (_d, _e):
        istek = _istek(uc, {"project_id": proje})
        assert await _body_project(None, istek) is None, uc  # type: ignore[arg-type]


async def test_rota_yoksa_none() -> None:
    istek = _istek(_b, {"project_id": str(uuid.uuid4())}, rota=False)
    assert await _body_project(None, istek) is None  # type: ignore[arg-type]


async def test_gomulu_govdede_ust_duzey_parametre_adi_bildirilmis_sayilir() -> None:
    proje = uuid.uuid4()
    assert await _body_project(None, _istek(_f, {"project_id": str(proje), "veri": 1})) == proje  # type: ignore[arg-type]
    # Gömülü gövdede modelin alanı ÜST DÜZEY anahtar DEĞİLDİR (`{"veri": {...}}` sarmalı).
    istek = _istek(_g, {"veri": {}, "ek": 1, "project_id": str(uuid.uuid4())})
    assert await _body_project(None, istek) is None  # type: ignore[arg-type]


# --- (d) OTOMATİK KEŞİF: RESOLVERS + rota tablosu → saldırı matrisi ----------------------------
#
# Elle liste YOK: her POST/PUT/PATCH rotası `app` rota tablosundan alınır; (önek, parametre) yol
# eşleşmesi `RESOLVERS`tan, bildirilen gövde anahtarları `_body_declared_keys`ten okunur. Yeni
# uç / yeni çözücü eklenince matris kendiliğinden genişler.

_PARAM = re.compile(r"{(\w+)(?::\w+)?}")
_YAZMA = ("POST", "PUT", "PATCH")
_YOL_PROJESI = uuid.UUID("11111111-1111-4111-8111-111111111111")  # P
_GOVDE_PROJESI = uuid.UUID("22222222-2222-4222-8222-222222222222")  # Q


_JSON_TURLERI = [
    "application/json",
    "application/JSON",
    "application/json; charset=utf-8",
    "application/vnd.x+json",
    "APPLICATION/VND.X+JSON",
]
_JSON_DEGIL = ["text/plain", "application/x-www-form-urlencoded", "application/jsonx", ""]


def _istek_kur(
    method: str,
    sablon: str,
    route: APIRoute,
    govde: object,
    *,
    tur: str = "application/json",
    ham: bytes | None = None,
) -> Request:
    """Rota tablosundaki bir rota için sahte istek: yol parametreleri doldurulmuş, gövde JSON
    (`ham` verilirse gövde baytları olduğu gibi; `tur` boşsa içerik türü başlığı YOK)."""
    parametreler = {ad: str(uuid.uuid4()) for ad in _PARAM.findall(sablon)}
    yol = _PARAM.sub(lambda m: parametreler[m.group(1)], sablon)
    if ham is None:
        ham = json.dumps(govde).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": ham, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "scheme": "http",
        "server": ("test", 80),
        "path": yol,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", tur.encode())] if tur else [],
        "path_params": parametreler,
        "route": route,
    }
    return Request(scope, receive)


class _Uc:
    """Keşfedilen yazma ucu: yöntem, yol şablonu, bildirilen gövde anahtarları, yol eşleşmesi."""

    def __init__(self, method: str, sablon: str, route: APIRoute) -> None:
        self.method, self.sablon, self.route = method, sablon, route
        self.bildirilen = _body_declared_keys(_istek_kur(method, sablon, route, {})) & set(
            BODY_KEYS
        )
        paramlar = _PARAM.findall(sablon)
        onek = "/" + sablon.lstrip("/").split("/", 1)[0]
        self.cozuculu = any((onek, p) in RESOLVERS for p in paramlar)
        self.form = any(isinstance(p.field_info, params.Form) for p in route.dependant.body_params)

    @property
    def kimlik(self) -> str:
        return f"{self.method} {self.sablon}"


def _yazma_uclari() -> list[_Uc]:
    sonuc: list[_Uc] = []
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        sonuc += [
            _Uc(yontem, ctx.path, ctx.original_route)
            for yontem in sorted(ctx.methods)
            if yontem in _YAZMA
        ]
    return sonuc


UCLAR = _yazma_uclari()
#: (A) gövde modeli anahtarlardan en az birini BİLDİRMİYOR → o anahtar enjekte edilebilir.
SINIF_A = [u for u in UCLAR if set(BODY_KEYS) - u.bildirilen]
#: (B) RESOLVERS'lı + gövde modeli bağlam anahtarı BİLDİRİYOR: kayıt taşıma / yol-gövde çelişkisi.
#: Form/multipart uçlar (alanlar JSON değil) dışarıda: bağlama hiç girmezler (`SINIF_FORM`).
SINIF_B = [u for u in UCLAR if u.cozuculu and u.bildirilen and not u.form]
SINIF_B_TASIMA = [u for u in SINIF_B if u.method in ("PUT", "PATCH")]
#: (C-POST) çözücüsüz + bağlam bildiren OLUŞTURMA uçları: gövde bağlamı geçerli kalır (doğru).
SINIF_C_POST = [
    u for u in UCLAR if not u.cozuculu and u.bildirilen and not u.form and u.method == "POST"
]
#: (C-PATCH) çözücüsüz + bağlam bildiren PATCH/PUT = BİLİNEN BORÇ (taşıma kuralı uygulanamaz).
SINIF_C_PATCH = [
    u
    for u in UCLAR
    if not u.cozuculu and u.bildirilen and not u.form and u.method in ("PUT", "PATCH")
]
#: Form/multipart bağlam bildiren uçlar: JSON gövde bağlamı hiç değiştirmez (eski davranış).
SINIF_FORM = [u for u in UCLAR if u.bildirilen and u.form]
#: Açık beyaz liste: yeni PATCH/PUT ucu eklenir ya da bir uç çözücü kazanırsa test KIRMIZI olur,
#: liste BİLİNÇLİ güncellenir. Saha (`/equipment/*`) ve satınalma (`/purchase-requests/*`)
#: çözücüleri IZN-B4d'de eklenecek. `/financial-instruments/{instrument_id}` HF1'de çözücü aldı.
BILINEN_COZUCUSUZ_PATCH = frozenset(
    {
        ("PATCH", "/purchase-requests/{request_id}"),
        ("PATCH", "/equipment/{equipment_id}"),
        ("PATCH", "/equipment/work-logs/{log_id}"),
        ("PATCH", "/equipment/fuel-logs/{log_id}"),
        ("PATCH", "/equipment/rental-invoices/{invoice_id}"),
    }
)


def _kimlikler(uclar: list[_Uc]) -> list[str]:
    return [u.kimlik for u in uclar]


def _cozucu_yerine_koy(monkeypatch, yol_projesi: uuid.UUID | None) -> None:
    """Çözücüleri sahte çözücüyle değiştirir (anahtarlar GERÇEK `RESOLVERS`tan): yol çözücüsü hep
    `yol_projesi`ni, gövde çözücüsü gövdedeki kimliği (= proje) döndürür."""

    async def yol(session, ref):
        return yol_projesi

    async def govde(session, ref):
        return ref if isinstance(ref, uuid.UUID) else None

    monkeypatch.setattr(baglam, "RESOLVERS", {anahtar: yol for anahtar in RESOLVERS})
    monkeypatch.setattr(baglam, "BODY_RESOLVERS", {anahtar: govde for anahtar in BODY_KEYS})


def test_matris_kesfi_bos_degil_ve_bilinen_tasima_uclarini_icerir() -> None:
    """Keşif sessiz-boş olursa matris hiçbir şeyi bekçilemez: sayı tabanı + bilinen ucun varlığı."""
    assert len(UCLAR) > 150, len(UCLAR)
    assert len(SINIF_A) > 100, len(SINIF_A)
    assert len(SINIF_B_TASIMA) >= 3, _kimlikler(SINIF_B_TASIMA)
    bilinen = {
        "PATCH /invoices/{invoice_id}",
        "PATCH /subcontractor-contracts/{contract_id}",
        "PATCH /financial-instruments/{instrument_id}",
    }
    assert bilinen <= set(_kimlikler(SINIF_B_TASIMA)), _kimlikler(SINIF_B_TASIMA)
    assert SINIF_C_POST, "gövdeden bağlam çözen oluşturma ucu bulunamadı"


@pytest.mark.parametrize("tur", _JSON_TURLERI)
@pytest.mark.parametrize("uc", SINIF_A, ids=_kimlikler(SINIF_A))
async def test_A_beyansiz_anahtar_enjeksiyonu_baglami_DEGISTIRMEZ(
    monkeypatch, uc: _Uc, tur: str
) -> None:
    """Gövde modeli bildirmediği `project_id`/`site_id`'yi gövdeye eklemek sonucu oynatmaz:
    yol çözücülü uçta yol projesi (P), çözücüsüz uçta `None` — govdesiz istekle AYNI (her JSON
    içerik türü çeşitlemesinde: büyük harf, parametre, `+json`)."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    enjekte = {k: str(_GOVDE_PROJESI) for k in set(BODY_KEYS) - uc.bildirilen}
    temel = await request_project(None, _istek_kur(uc.method, uc.sablon, uc.route, {}, tur=tur))  # type: ignore[arg-type]
    saldiri = await request_project(
        None,  # type: ignore[arg-type]
        _istek_kur(uc.method, uc.sablon, uc.route, enjekte, tur=tur),
    )
    assert saldiri == temel, uc.kimlik
    assert temel == (_YOL_PROJESI if uc.cozuculu else None), uc.kimlik


@pytest.mark.parametrize("tur", _JSON_TURLERI)
@pytest.mark.parametrize("uc", SINIF_B, ids=_kimlikler(SINIF_B))
@pytest.mark.parametrize("yol_projesi", [_YOL_PROJESI, None], ids=["P-dolu", "P-projesiz"])
async def test_B_tasi_yaz_yol_ile_govde_celisirse_baglam_COZULMEZ(
    monkeypatch, uc: _Uc, yol_projesi: uuid.UUID | None, tur: str
) -> None:
    """Yol çözücüsü eşleşti (P ya da projesiz kayıt) VE gövde başka bir projeyi (Q) gösteriyor →
    `None` (maske birleşim, kapı ana rol): kayıt Q'ya "taşınıyormuş" gibi Q'daki rolle okunamaz /
    yazılamaz. Bildirilen HER anahtar için ve her JSON içerik türü çeşitlemesinde ölçülür."""
    _cozucu_yerine_koy(monkeypatch, yol_projesi)
    for anahtar in sorted(uc.bildirilen):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, {anahtar: str(_GOVDE_PROJESI)}, tur=tur)
        assert await request_project(None, istek) is None, (uc.kimlik, anahtar)  # type: ignore[arg-type]


@pytest.mark.parametrize("uc", SINIF_B, ids=_kimlikler(SINIF_B))
async def test_B_acik_null_P_doluyken_projesize_tasima_baglami_COZMEZ(monkeypatch, uc: _Uc) -> None:
    """P dolu + gövdede bildirilmiş anahtar AÇIKÇA `null` ("hedef = projesiz") → `None`: proje
    rolüyle kayıt şirket geneline taşınamaz. P projesiz + `null` → P == Q (ikisi de projesiz):
    `None` (zaten bağlam yok). Anahtar başına ölçülür."""
    for yol_projesi in (_YOL_PROJESI, None):
        _cozucu_yerine_koy(monkeypatch, yol_projesi)
        for anahtar in sorted(uc.bildirilen):
            istek = _istek_kur(uc.method, uc.sablon, uc.route, {anahtar: None})
            assert await request_project(None, istek) is None, (uc.kimlik, anahtar, yol_projesi)  # type: ignore[arg-type]


@pytest.mark.parametrize("uc", SINIF_B, ids=_kimlikler(SINIF_B))
@pytest.mark.parametrize("gecersiz", [7, 1.5, True, {"a": 1}, ["x"]], ids=repr)
@pytest.mark.parametrize("yol_projesi", [_YOL_PROJESI, None], ids=["P-dolu", "P-projesiz"])
async def test_B_gecersiz_deger_fail_closed(
    monkeypatch, uc: _Uc, yol_projesi: uuid.UUID | None, gecersiz: object
) -> None:
    """Dize/null olmayan bildirilmiş değer (sayı, nesne, liste, bool) → bağlam `None`."""
    _cozucu_yerine_koy(monkeypatch, yol_projesi)
    for anahtar in sorted(uc.bildirilen):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, {anahtar: gecersiz})
        assert await request_project(None, istek) is None, (uc.kimlik, anahtar)  # type: ignore[arg-type]


@pytest.mark.parametrize("uc", SINIF_B, ids=_kimlikler(SINIF_B))
async def test_C_ayni_projeye_isaret_eden_govde_yol_baglaminda_calisir(
    monkeypatch, uc: _Uc
) -> None:
    """P == Q (gövde kaydın KENDİ projesini yazar): bağlam P; govdesiz istek de P (yol aynen)."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    for anahtar in sorted(uc.bildirilen):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, {anahtar: str(_YOL_PROJESI)})
        assert await request_project(None, istek) == _YOL_PROJESI, (uc.kimlik, anahtar)  # type: ignore[arg-type]
    govdesiz = _istek_kur(uc.method, uc.sablon, uc.route, {})
    assert await request_project(None, govdesiz) == _YOL_PROJESI, uc.kimlik  # type: ignore[arg-type]


@pytest.mark.parametrize("uc", SINIF_C_POST, ids=_kimlikler(SINIF_C_POST))
async def test_C_POST_yol_cozucusuz_olusturma_ucu_baglami_govdeden_cozer(
    monkeypatch, uc: _Uc
) -> None:
    """Yol çözücüsü HİÇ eşleşmiyorsa (oluşturma uçları) gövde bağlamı geçerli kalır (doğru).
    Açık `null` bildirilmemiş gibi yok sayılır (oluşturmada hedef = projesiz = bağlam yok)."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    for anahtar in sorted(uc.bildirilen):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, {anahtar: str(_GOVDE_PROJESI)})
        assert await request_project(None, istek) == _GOVDE_PROJESI, (uc.kimlik, anahtar)  # type: ignore[arg-type]
    if uc.bildirilen == set(BODY_KEYS):
        karma = {"project_id": None, "site_id": str(_GOVDE_PROJESI)}
        istek = _istek_kur(uc.method, uc.sablon, uc.route, karma)
        assert await request_project(None, istek) == _GOVDE_PROJESI, uc.kimlik  # type: ignore[arg-type]


def test_C_PATCH_cozucusuz_uclar_acik_beyaz_listeyle_ESIT() -> None:
    """BİLİNEN BORÇ: yol çözücüsü EŞLEŞMEYEN ama gövde bağlamı bildiren PATCH/PUT uçları taşıma
    kuralına giremez (gövdeden bağlam alır). Küme beyaz listeyle birebir eşit olmalı: yeni uç
    eklenirse ya da bir uç çözücü kazanırsa test kırmızı olur, liste bilinçli güncellenir."""
    bulunan = {(u.method, u.sablon) for u in SINIF_C_PATCH}
    assert bulunan == BILINEN_COZUCUSUZ_PATCH, (
        sorted(bulunan - BILINEN_COZUCUSUZ_PATCH),
        sorted(BILINEN_COZUCUSUZ_PATCH - bulunan),
    )


@pytest.mark.parametrize("uc", SINIF_FORM, ids=_kimlikler(SINIF_FORM))
async def test_FORM_uclarda_json_govde_baglami_DEGISTIRMEZ(monkeypatch, uc: _Uc) -> None:
    """Form/multipart uçlarda `project_id`/`site_id` form alanıdır (JSON değil): fail-closed kuralı
    onları kırmaz (meşru akış) ve JSON gövde bağlamı oynatmaz — gövdesiz istekle AYNI."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    govde = {k: str(_GOVDE_PROJESI) for k in uc.bildirilen}
    temel = await request_project(None, _istek_kur(uc.method, uc.sablon, uc.route, {}))  # type: ignore[arg-type]
    saldiri = await request_project(None, _istek_kur(uc.method, uc.sablon, uc.route, govde))  # type: ignore[arg-type]
    assert saldiri == temel == (_YOL_PROJESI if uc.cozuculu else None), uc.kimlik


async def test_C_govde_anahtarlari_birbiriyle_celisirse_baglam_COZULMEZ(monkeypatch) -> None:
    """`project_id` ve `site_id` ikisi de bildirilmiş ve FARKLI projelere çözülüyorsa `None`."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    uc = next(u for u in SINIF_C_POST if u.bildirilen == set(BODY_KEYS))
    govde = {"project_id": str(_GOVDE_PROJESI), "site_id": str(uuid.uuid4())}
    istek = _istek_kur(uc.method, uc.sablon, uc.route, govde)
    assert await request_project(None, istek) is None  # type: ignore[arg-type]


# --- (d2) içerik türü + okunamayan gövde (FastAPI kuralıyla AYNI çözüm) --------------------------


def _bildirir_ucu() -> _Uc:
    return next(u for u in SINIF_C_POST if u.bildirilen == set(BODY_KEYS))


@pytest.mark.parametrize("tur", _JSON_TURLERI)
async def test_CT_json_icerik_turu_cesitlemeleri_govdeden_cozer(monkeypatch, tur: str) -> None:
    """`application/JSON`, parametreli, `+json` ve büyük harfli `+JSON`: FastAPI gövdeyi JSON çözer,
    bağlam da AYNI biçimde okur (ayrışma = kapı atlatma: HF1 çürütmesi)."""
    _cozucu_yerine_koy(monkeypatch, None)
    uc = _bildirir_ucu()
    istek = _istek_kur(uc.method, uc.sablon, uc.route, {"project_id": str(_GOVDE_PROJESI)}, tur=tur)
    assert await request_project(None, istek) == _GOVDE_PROJESI  # type: ignore[arg-type]


@pytest.mark.parametrize("tur", _JSON_DEGIL)
async def test_CT_json_olmayan_icerik_turu_bildiren_rotada_baglami_COZMEZ(
    monkeypatch, tur: str
) -> None:
    """Rota bağlam anahtarı bildiriyor ama içerik türü JSON değil (ya da yok): FastAPI gövdeyi
    JSON okumaz; bağlam da `None` (fail-closed). Yol çözücülü uçta P'ye düşmez."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    for uc in (_bildirir_ucu(), SINIF_B_TASIMA[0]):
        istek = _istek_kur(
            uc.method, uc.sablon, uc.route, {"project_id": str(_GOVDE_PROJESI)}, tur=tur
        )
        assert await request_project(None, istek) is None, (uc.kimlik, tur)  # type: ignore[arg-type]


@pytest.mark.parametrize("ham", [b"", b"{bozuk", b"[1, 2]", b"null", b'"metin"', b"\xff\xfe"])
async def test_CT_okunamayan_govde_bildiren_rotada_baglami_COZMEZ(monkeypatch, ham: bytes) -> None:
    """Boş / bozuk / dict olmayan / UTF-8 olmayan gövde: bildiren rota için bağlam `None`."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    for uc in (_bildirir_ucu(), SINIF_B_TASIMA[0]):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, None, ham=ham)
        assert await request_project(None, istek) is None, (uc.kimlik, ham)  # type: ignore[arg-type]


async def test_CT_bildirmeyen_rotada_okunamayan_govde_baglami_OYNATMAZ(monkeypatch) -> None:
    """Rota bildirmiyorsa gövde zaten okunmaz: bozuk gövde / JSON olmayan tür yol bağlamını
    (P) bozmaz — fail-closed YALNIZ bildiren rotalar içindir."""
    _cozucu_yerine_koy(monkeypatch, _YOL_PROJESI)
    uc = next(u for u in SINIF_A if u.cozuculu and not u.bildirilen)
    for tur, ham in (("text/plain", b"x"), ("application/json", b"{bozuk")):
        istek = _istek_kur(uc.method, uc.sablon, uc.route, None, tur=tur, ham=ham)
        assert await request_project(None, istek) == _YOL_PROJESI, uc.kimlik  # type: ignore[arg-type]


# --- (e) uçtan uca: kayıt TAŞIMA ile bağlam değiştirilemez (HTTP) -------------------------------


async def _sozlesme(session, kimlik: uuid.UUID) -> SubcontractorContract:
    session.expire_all()
    return (
        await session.execute(
            select(SubcontractorContract).where(SubcontractorContract.id == kimlik)
        )
    ).scalar_one()


async def test_E2E_sozlesme_baska_projenin_santiyesine_tasinarak_yazilamaz(
    client, seeded_db, db_session, user_factory, iki_proje_ekip
) -> None:
    """Kişi: ana rol HİÇBİR ŞEY, Kule'de Düzenler. Kule'nin sözleşmesini `site_id` ile Köprü'ye
    "taşıyarak" yazmak 403 (taşıma = yalnız ana rol; ekip rolü yetmez), sözleşme DEĞİŞMEZ.
    POZİTİF KONTROL: kendi şantiyesini yazmak (P == Q) ve gövdesiz PATCH kapıyı GEÇER."""
    kule, kopru = iki_proje_ekip
    await rol_kur(seeded_db, "tasi_hicbiri", PageLevel.none)
    duzenler = await rol_kur(seeded_db, "tasi_duzenler", PageLevel.edit)
    kisi = await user_factory(email="tasi@hf1.co", password=PASSWORD, role_key="tasi_hicbiri")
    await ekibe_ekle(seeded_db, kisi, kule.project.id, duzenler.id)
    baslik = await _giris(client, "tasi@hf1.co")
    sozlesme_id, kopru_site, kule_site = kule.sub_contract.id, str(kopru.site.id), str(kule.site.id)
    ilk_site = (await _sozlesme(db_session, sozlesme_id)).site_id
    yol = f"/subcontractor-contracts/{sozlesme_id}"

    tasima = await client.patch(yol, json={"site_id": kopru_site}, headers=baslik)
    assert tasima.status_code == 403, tasima.text
    assert (await _sozlesme(db_session, sozlesme_id)).site_id == ilk_site

    kendi = await client.patch(yol, json={"site_id": kule_site}, headers=baslik)
    assert kendi.status_code != 403, kendi.text  # P == Q: yol bağlamı (Kule'de Düzenler)
    govdesiz = await client.patch(yol, json={"work_category": "Kaba"}, headers=baslik)
    assert govdesiz.status_code != 403, govdesiz.text
