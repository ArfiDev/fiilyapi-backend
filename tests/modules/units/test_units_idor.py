"""B10 — IDOR negatif setinin TAMAMI (spec §11.4'un 14 satiri, birebir).

GUVENLIK SINIFI. P2'de tam bu noktada bir IDOR acigi yakalandi: kimligi YUKARI
cozumleyen uclar (`/units/{id}`, `/blocks/{id}`) proje gorunurlugunu atlayabilir
ve baska bir kullanicinin kaydini dondurebilir. Bu dosya, o sinifin her uc icin
kapali kaldigini kilitler ve `units` modulunun REGRESYON AGIDIR.

Uc kural bu dosyanin tamamina hakimdir:

1. **Gorunmeyen kayit 404'tur, 403 DEGIL.** 403 "bu kayit var ama senin degil"
   demektir ve varligin kendisini sizdirir.
2. **404 govdesi de ayirt edici olmamalidir.** Var olmayan UUID ile baskasina
   ait UUID AYNI mesaji doner (`test_idor_unknown_uuid_same_message_as_invisible`).
3. **Izin, gorunurlukten ONCE gelir.** `projects` izni `none`/`view` olan bir
   kullanici projeyi gorse bile yazamaz (403) — 404'e hic ulasmaz.

Roller seed matrisinden (`roles/seed_data.py`) gelir ve UYDURULMAZ:
`system_admin`=admin, `patron`=full, `site_chief`=view, `procurement`=none.
`patron` erisim satiri VERILMEDEN kullanildiginda hicbir projeyi goremez —
"gorunmez proje" senaryolarinin dayanagi budur.
"""

import io
import uuid

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.modules.units.models import Block, Unit, UnitOwnerSide
from tests._silme_yardimci import sil_aile
from tests.modules.units._units_api import (
    _auth,
    _block,
    _login,
    _login_with_access,
    _site,
    _unit,
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# P3.1 T11: `Oda Tipi` ve `Brüt m²` ZORUNLU sutun oldu (EI 161, spec §6.5).
_HEADERS = ["Blok", "Ünite No", "Tür", "Oda Tipi", "Brüt m²"]

_PROJECT_MISSING = "Proje bulunamadı"
_BLOCK_MISSING = "Blok bulunamadı"
_UNIT_MISSING = "Ünite bulunamadı"

# Gorunmeyen kayit senaryolarinda govdenin TASIYABILECEGI tek anahtar.
_ALLOWED_ERROR_KEYS = {"detail"}


def _xlsx(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _file(content: bytes) -> dict:
    return {"file": ("uniteler.xlsx", content, _XLSX_MIME)}


async def _call(client, method: str, url: str, headers: dict[str, str] | None = None):
    """Parametrik uc listelerinin TEK cagri noktasi.

    `client.get`/`client.delete` `json=` kabul ETMEZ (httpx), `client.request`
    ise dort yontemi de ayni imzayla kabul eder. Yontem basina dallanmak yerine
    govde YALNIZ yazma yontemlerinde eklenir — bu sayede ayni parametre tablosu
    401/403 testlerinin ikisinde de aynen kullanilabilir.
    """
    kwargs: dict = {"headers": headers or {}}
    if method in ("post", "patch", "put"):
        kwargs["json"] = {}
    return await client.request(method.upper(), url, **kwargs)


async def _assigned_sides(session, project_id: uuid.UUID) -> dict[uuid.UUID, str | None]:
    """Projedeki her unitenin payi — atomikligin oncesi/sonrasi karsilastirmasi."""
    result = await session.execute(select(Unit).where(Unit.project_id == project_id))
    return {
        unit.id: unit.owner_side.value if unit.owner_side is not None else None
        for unit in result.scalars().all()
    }


async def _block_count(session, project_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Block).where(Block.project_id == project_id)
    )
    return int(result.scalar_one())


def _assert_no_leak(resp, *secrets: str) -> None:
    """Govde kayit KIMLIGI/ADI/SAYISI tasimamalidir (spec §11.4 son cumlesi).

    Sizinti "kayit yok" demekle "kayit var ama senin degil" demek arasindaki
    farktir; ikincisi bir UUID'yi elinde tutan saldirgana kesin bilgi verir.
    """
    assert set(resp.json()) <= _ALLOWED_ERROR_KEYS, resp.json()
    body = resp.text
    for secret in secrets:
        assert secret not in body, f"govde sizdirdi: {secret}"


async def _fixture_project_with_unit(db_session, project_factory, code: str, **kwargs):
    project = await project_factory(code, **kwargs)
    site = await _site(db_session, project)
    block = await _block(db_session, project, site)
    unit = await _unit(db_session, project, block)
    return project, block, unit


# --- §11.4-1 / §11.4-2: okuma uclari, gorunmeyen proje ---


async def test_idor_get_units_invisible_project_404(client, user_factory, project_factory):
    """§11.4-1. Erisim satiri olmayan `patron`: 403 DEGIL 404."""
    project = await project_factory("IDOR-1")
    token = await _login(client, user_factory, "patron")

    resp = await client.get(f"/projects/{project.id}/units", headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), project.code, project.name)


async def test_idor_get_blocks_invisible_project_404(client, user_factory, project_factory):
    """§11.4-2."""
    project = await project_factory("IDOR-2")
    token = await _login(client, user_factory, "patron")

    resp = await client.get(f"/projects/{project.id}/blocks", headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), project.code, project.name)


# --- §11.4-3: proje baglamli YAZMA uclari, gorunmeyen proje → 404 (403 DEGIL) ---


@pytest.mark.parametrize("endpoint", ["units", "blocks", "units/bulk", "units/import"])
async def test_idor_post_units_blocks_bulk_import_invisible_404(
    client, db_session, user_factory, project_factory, endpoint
):
    """§11.4-3. DORT uc birden: `patron` YAZMA iznine sahiptir (`projects`=full),
    dolayisiyla 403 uretecek bir izin engeli YOKTUR — geriye yalniz gorunurluk
    kalir ve o da 404 vermek ZORUNDADIR. Bu satir, izin engeliyle gorunurluk
    engelinin karistirilmadigini kanitlar."""
    project, block, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-3")
    token = await _login(client, user_factory, "patron")
    url = f"/projects/{project.id}/{endpoint}"

    if endpoint == "units/import":
        resp = await client.post(
            url, files=_file(_xlsx([["A Blok", "9", "Daire", "3+1", 120]])), headers=_auth(token)
        )
    elif endpoint == "units/bulk":
        payload = {
            "block_id": str(block.id),
            "unit_kind": "apartment",
            "start_floor": 1,
            "end_floor": 1,
            "units_per_floor": 2,
        }
        resp = await client.post(url, json=payload, headers=_auth(token))
    elif endpoint == "blocks":
        resp = await client.post(url, json={"name": "Z Blok"}, headers=_auth(token))
    else:
        payload = {"block_id": str(block.id), "unit_no": "99", "unit_kind": "apartment"}
        resp = await client.post(url, json=payload, headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), str(block.id), project.code)


# --- §11.4-4 / §11.4-5: kimligi YUKARI cozumleyen tekil uclar ---


async def test_idor_patch_unit_invisible_404(client, db_session, user_factory, project_factory):
    """§11.4-4. Unite → proje → gorunurluk. P2'de kirilan tam nokta budur:
    `/units/{id}` yolunda proje kimligi YOKTUR, servis onu kayittan cozer."""
    _, _, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-4")
    token = await _login(client, user_factory, "patron")

    resp = await client.patch(f"/units/{unit.id}", json={"unit_no": "7"}, headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _UNIT_MISSING
    _assert_no_leak(resp, str(unit.id), unit.unit_no)


async def test_idor_patch_block_invisible_404(client, db_session, user_factory, project_factory):
    """§11.4-5."""
    _, block, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-5")
    token = await _login(client, user_factory, "patron")

    resp = await client.patch(f"/blocks/{block.id}", json={"name": "Z"}, headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _BLOCK_MISSING
    _assert_no_leak(resp, str(block.id), block.name)


# --- §11.4-6: DELETE uclari ---


@pytest.mark.parametrize("kind", ["unit", "block"])
async def test_idor_delete_unit_and_block_full_level_403_leaks_nothing(
    client, db_session, user_factory, project_factory, kind
):
    """§11.4-6, KULLANICI KARARI 2026-07-30 ile guncellendi.

    Silme artik `projects:admin` ister. `full` seviyeli `patron` yetki kapisinda
    durdugu icin yanit 404 DEGIL 403'tur — ve bu bir gerileme DEGILDIR: 403
    tamamen kayittan BAGIMSIZDIR (var olmayan UUID icin de birebir aynidir,
    `test_units_api::test_delete_unit_invisible_returns_403_...`), dolayisiyla
    varlik yine sizmaz. Silme, gorunurlugu atlarsa VERI KAYBI uretir; asil kanit
    kaydin GERCEKTEN durmasidir ve sayimla dogrulanir.
    """
    project, block, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-6")
    token = await _login(client, user_factory, "patron")
    target = f"/units/{unit.id}" if kind == "unit" else f"/blocks/{block.id}"

    resp = await client.delete(target, headers=_auth(token))

    assert resp.status_code == 403
    _assert_no_leak(resp, str(unit.id), str(block.id), block.name)
    assert await _block_count(db_session, project.id) == 1
    assert len(await _assigned_sides(db_session, project.id)) == 1


# --- §11.4-7: var olmayan UUID ile gorunmeyen kayit AYIRT EDILEMEZ ---


@pytest.mark.parametrize(
    ("method", "root", "payload", "message"),
    [
        ("patch", "units", {"unit_no": "7"}, _UNIT_MISSING),
        ("patch", "blocks", {"name": "Z"}, _BLOCK_MISSING),
        # DELETE satirlari 2026-07-30 karariyla DUSTU: silme `projects:admin`
        # ister ve `admin` gorunurluk suzgecini zaten atlar (`visible_projects`),
        # yani "gorunmeyen kayit" senaryosu silme ucunda ARTIK KURULAMAZ. Kapiyi
        # gecemeyen aktorun 403'u ise kayittan bagimsizdir (yukaridaki §11.4-6
        # testi). Gorunmez→404 esitligi PATCH satirlarinda aynen durur; silme
        # ucunda var olmayan UUID'nin 404 mesaji `test_units_api`de kilitlidir.
    ],
)
async def test_idor_unknown_uuid_same_message_as_invisible(
    client, db_session, user_factory, project_factory, method, root, payload, message
):
    """§11.4-7. IKI istek atilir ve yanitlar BIREBIR karsilastirilir.

    "Ayni mesaj" iddiasini iki ayri testte iki ayri sabite bakarak dogrulamak
    yetmez: birisi degistiginde digeri sessizce yesil kalirdi. Bu yuzden ayni
    test ICINDE gorunmeyen kaydin ve var olmayan UUID'nin yanitlari esitlenir.
    """
    project, block, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-7")
    token = await _login(client, user_factory, "patron")
    existing_id = unit.id if root == "units" else block.id
    kwargs = {"headers": _auth(token)}
    if payload is not None:
        kwargs["json"] = payload

    invisible = await getattr(client, method)(f"/{root}/{existing_id}", **kwargs)
    unknown = await getattr(client, method)(f"/{root}/{uuid.uuid4()}", **kwargs)

    assert invisible.status_code == unknown.status_code == 404
    assert invisible.json() == unknown.json() == {"detail": message}
    _assert_no_leak(invisible, str(existing_id), str(project.id))


# --- §11.4-8: paylasim ucu, listede BASKA projenin unitesi ---


async def test_idor_allocation_with_other_project_unit_404_and_atomic(
    client, db_session, user_factory, project_factory
):
    """§11.4-8. En sinsi satir: proje A GORUNUR, izin TAM, govde neredeyse
    gecerli — tek yabanci `unit_id` tum istegi dusurmeli VE A'nin hicbir satiri
    degismemelidir.

    Durum kodu tek basina KANIT DEGILDIR: 404 donerken A'nin ilk 3 unitesi
    yazilmis olabilirdi. Bu yuzden oncesi/sonrasi pay HARITASI karsilastirilir.
    """
    project_a = await project_factory("IDOR-8A", project_type="kat_karsiligi")
    site_a = await _site(db_session, project_a)
    block_a = await _block(db_session, project_a, site_a)
    units_a = [
        await _unit(db_session, project_a, block_a, unit_no=str(i), sort_order=i) for i in range(3)
    ]
    project_b = await project_factory("IDOR-8B", project_type="kat_karsiligi")
    site_b = await _site(db_session, project_b, code="SANTIYE-B")
    block_b = await _block(db_session, project_b, site_b, name="B Blok")
    unit_b = await _unit(db_session, project_b, block_b, unit_no="1")
    token = await _login(client, user_factory, "system_admin")
    before = await _assigned_sides(db_session, project_a.id)
    items = [{"unit_id": str(u.id), "owner_side": "contractor"} for u in units_a]
    items.append({"unit_id": str(unit_b.id), "owner_side": "landowner"})

    resp = await client.patch(
        f"/projects/{project_a.id}/units/allocation",
        json={"items": items},
        headers=_auth(token),
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == _UNIT_MISSING
    _assert_no_leak(resp, str(unit_b.id), str(project_b.id), project_b.code)
    assert await _assigned_sides(db_session, project_a.id) == before
    assert all(side is None for side in before.values())
    # B'ye de dokunulmamis olmali.
    assert await _assigned_sides(db_session, project_b.id) == {unit_b.id: None}


# --- §11.4-9: govdedeki `block_id` baska projeye ait ---


async def test_idor_post_unit_with_foreign_block_404(
    client, db_session, user_factory, project_factory
):
    """§11.4-9. Yol proje A'yi gosterir, GOVDE B'nin blogunu. Uc, gorunur bir
    proje uzerinden BASKA projenin blogunu yazmaya izin VERMEZ — ve yabanci
    blogun varligini da sizdirmaz (404, 422 degil)."""
    project_a = await project_factory("IDOR-9A")
    await _site(db_session, project_a)
    project_b, block_b, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-9B")
    token = await _login(client, user_factory, "system_admin")
    payload = {"block_id": str(block_b.id), "unit_no": "99", "unit_kind": "apartment"}

    resp = await client.post(f"/projects/{project_a.id}/units", json=payload, headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _BLOCK_MISSING
    _assert_no_leak(resp, str(block_b.id), block_b.name, str(project_b.id))
    assert await _block_count(db_session, project_a.id) == 0


# --- §11.4-10: Excel'deki blok adi baska projenin bloguyla ayni ---


async def test_idor_import_block_name_collision_creates_in_own_project(
    client, db_session, user_factory, project_factory
):
    """§11.4-10. Blok adlari GLOBAL benzersiz DEGILDIR (`uq_blocks_project_name`
    kapsami projedir). Ice aktarma, ayni adi tasiyan yabanci bloga BAGLANMAZ;
    kendi projesinde YENI blok acar ve digerine dokunmaz."""
    project_b, block_b, unit_b = await _fixture_project_with_unit(
        db_session, project_factory, "IDOR-10B"
    )
    project_a = await project_factory("IDOR-10A")
    await _site(db_session, project_a)
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project_a.id}/units/import",
        files=_file(_xlsx([[block_b.name, "1", "Daire", "3+1", 120]])),
        headers=_auth(token),
    )

    assert resp.status_code == 200
    # BILEREK DEGISTI (P3.1 T12, spec §6.3): `errors` alani kaldirildi, yerini
    # `summary`/`rows`/`skipped` aldi. IDDIA degismedi: bir unite ve bir blok.
    assert (resp.json()["created"], resp.json()["blocks_created"]) == (1, 1)
    assert resp.json()["skipped"] == 0
    result = await db_session.execute(
        select(Block).where(Block.project_id == project_a.id, Block.name == block_b.name)
    )
    created = result.scalar_one()
    assert created.id != block_b.id
    # B projesi DOKUNULMAMIS: tek blogu ve tek unitesi aynen duruyor.
    assert await _block_count(db_session, project_b.id) == 1
    assert list(await _assigned_sides(db_session, project_b.id)) == [unit_b.id]


# --- P3.1 T15 (spec §12.6): P3.1'in YENI YUZEYLERI ---
#
# Bu bolumun konusu, P3.1'in actigi uc yeni saldiri yuzeyidir:
#   1. `POST .../units/bulk/preview` — hicbir sey YAZMAYAN, ama yazma
#      mantiginin TAMAMINI (fiyat uretimi, cakisma tespiti) calistiran uc.
#      "Yazmiyor" olmasi onu zararsiz YAPMAZ: gorunmeyen bir projenin blok
#      kimligiyle cagrilirsa o blogun VARLIGINI dogrular.
#   2. `POST .../units/import/validate` — ayni sinif, dosya uzerinden.
#   3. `site_id` FORM ALANI — hem `import` hem `validate` govdesinde tasinan,
#      yol disindan gelen IKINCI bir kimlik. Yol proje A'yi gosterirken
#      `site_id` B'nin santiyesini gosterebilir (`block_id` tuzaginin ikizi).
#
#   4. `GET .../units/import/template` — P3.1 T14'te acilan sablon ucu. Izni
#      `view`'dir (spec §6.2 karari), yani BU DILIMDEKI TEK "view yeter" yeni
#      uctur; gorunurluk kapisi ise diger uclarla AYNIDIR (I3 vs I6).


async def test_idor_preview_invisible_project_404(
    client, db_session, user_factory, project_factory
):
    """Spec §12.6/I1. `patron` YAZMA iznine sahiptir (`projects`=full), dolayisiyla
    403 uretecek bir izin engeli yoktur — geriye yalniz gorunurluk kalir ve o da
    404 vermek ZORUNDADIR (403 "bu proje var" demek olurdu)."""
    project, block, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-P1")
    token = await _login(client, user_factory, "patron")

    resp = await client.post(
        f"/projects/{project.id}/units/bulk/preview",
        json={
            "block_id": str(block.id),
            "unit_kind": "apartment",
            "start_floor": 1,
            "end_floor": 1,
            "units_per_floor": 2,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), str(block.id), project.code, block.name)


async def test_idor_validate_invisible_project_404(
    client, db_session, user_factory, project_factory
):
    """Spec §12.6/I2. `validate` HICBIR SEY YAZMAZ; yine de gorunurluk kapisi
    `import` ile AYNIDIR — "yazmayan uc" gorunurluk muafiyeti DEGILDIR."""
    project, _, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-P2")
    token = await _login(client, user_factory, "patron")

    resp = await client.post(
        f"/projects/{project.id}/units/import/validate",
        files=_file(_xlsx([["A Blok", "9", "Daire", "3+1", 120]])),
        headers=_auth(token),
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), project.code)


async def test_idor_preview_with_foreign_block_404(
    client, db_session, user_factory, project_factory
):
    """Spec §12.6/I4. `test_idor_post_unit_with_foreign_block_404`'un onizleme
    ikizi: yol proje A'yi, GOVDE B'nin blogunu gosterir.

    Onizleme icin AYRICA onemlidir: yanit satirlari yabanci blogun kat/fiyat
    duzenini ele verirdi — hicbir sey YAZILMASA DA sizinti tamdir."""
    project_a = await project_factory("IDOR-P4A")
    await _site(db_session, project_a)
    project_b, block_b, _ = await _fixture_project_with_unit(
        db_session, project_factory, "IDOR-P4B"
    )
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project_a.id}/units/bulk/preview",
        json={
            "block_id": str(block_b.id),
            "unit_kind": "apartment",
            "start_floor": 1,
            "end_floor": 1,
            "units_per_floor": 2,
        },
        headers=_auth(token),
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == _BLOCK_MISSING
    _assert_no_leak(resp, str(block_b.id), block_b.name, str(project_b.id))


async def test_idor_template_invisible_project_404(client, user_factory, project_factory):
    """Spec §12.6/I3. Sablon ucu `view` izniyle acilir (I6) ama GORUNURLUK
    muafiyeti YOKTUR: gorunmeyen bir projenin sablonu 404 doner.

    Ayrimi kacirmak kolaydir — "sablonda proje verisi yok, kapiya ne gerek var"
    denirse yol bir PROJE VARLIK ORAKULUNE doner: elinde UUID olan kullanici
    200/404 farkindan projenin var oldugunu okur.
    """
    project = await project_factory("IDOR-I3")
    token = await _login(client, user_factory, "patron")

    resp = await client.get(f"/projects/{project.id}/units/import/template", headers=_auth(token))

    assert resp.status_code == 404
    assert resp.json()["detail"] == _PROJECT_MISSING
    _assert_no_leak(resp, str(project.id), project.code, project.name)


async def test_idor_template_with_view_permission_200(
    client, db_session, user_factory, project_factory
):
    """Spec §12.6/I6 + §6.2 karari. `site_chief` (`projects`=view) sablonu
    INDIREBILIR.

    Bu, dosyadaki TEK "view yeter" yeni uctur ve bilinclidir: sablon bos bir
    baslik satiridir, proje verisi tasimaz; `full`'a kapatmak veri GIRECEK
    kullaniciyi akisin ilk adimindan mahrum birakirdi. Ayni rol `preview` ve
    `validate`'te 403 alir (I5) — fark ASAGIDAKI `view` tablosunda kilitli.
    """
    project, _, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-I6")
    token = await _login_with_access(client, db_session, user_factory, "site_chief")

    resp = await client.get(f"/projects/{project.id}/units/import/template", headers=_auth(token))

    assert resp.status_code == 200


@pytest.mark.parametrize("endpoint", ["units/import", "units/import/validate"])
async def test_idor_site_id_from_other_project_404(
    client, db_session, user_factory, project_factory, endpoint
):
    """P3.1 T15 — `site_id` HEDEFI (spec §12.5/47c, §6.2 karar 3).

    `site_id` yoldan DEGIL govdeden gelir ve `block_id` tuzaginin ikizidir:
    gorunur proje A uzerinden B'nin santiyesine blok actirabilirdi. IKI ucta
    da kapali olmasi gerekir — `validate` yazmasa bile yabanci santiyenin
    varligini dogrulamamalidir.
    """
    project_a = await project_factory("IDOR-P5A")
    await _site(db_session, project_a)
    project_b = await project_factory("IDOR-P5B")
    foreign_site = await _site(db_session, project_b, code="SANTIYE-B")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.post(
        f"/projects/{project_a.id}/{endpoint}",
        files=_file(_xlsx([["Z Blok", "1", "Daire", "3+1", 120]])),
        data={"site_id": str(foreign_site.id)},
        headers=_auth(token),
    )

    assert resp.status_code == 404
    _assert_no_leak(resp, str(foreign_site.id), str(project_b.id), project_b.code)
    assert await _block_count(db_session, project_a.id) == 0


# --- §11.4-11: token yok → 401 (TUM uclar) ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/projects/{project}/blocks"),
        ("post", "/projects/{project}/blocks"),
        ("patch", "/blocks/{block}"),
        ("delete", "/blocks/{block}"),
        ("get", "/projects/{project}/units"),
        ("post", "/projects/{project}/units"),
        ("patch", "/units/{unit}"),
        ("delete", "/units/{unit}"),
        ("post", "/projects/{project}/units/bulk"),
        ("post", "/projects/{project}/units/import"),
        ("patch", "/projects/{project}/units/allocation"),
        # P3.1 T15 (spec §12.6/I8): iki yeni uc. Kimlik dogrulama, uclarin
        # "yazip yazmadigina" BAKMAZ — onizleme de token ister.
        ("post", "/projects/{project}/units/bulk/preview"),
        ("post", "/projects/{project}/units/import/validate"),
        # P3.1 T14: sablon ucu. Izni `view`'dir (I6) ama KIMLIK yine sarttir —
        # "herkese acik sablon" diye bir kapi acilmaz.
        ("get", "/projects/{project}/units/import/template"),
    ],
)
async def test_idor_no_token_401(client, db_session, user_factory, project_factory, method, path):
    """§11.4-11 + spec §12.6/I8. P3.1 sonrasi ON DORT ucun HEPSI. Yeni bir uc
    eklenip bu listeye yazilmazsa eksik oldugu B12 openapi karsilastirmasinda
    gorulur."""
    project, block, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-11")
    url = path.format(project=project.id, block=block.id, unit=unit.id)

    resp = await _call(client, method, url)

    assert resp.status_code == 401


# --- §11.4-12: `projects` izni `none` → 403 ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/projects/{project}/blocks"),
        ("get", "/projects/{project}/units"),
        ("post", "/projects/{project}/units"),
        ("patch", "/units/{unit}"),
        ("patch", "/projects/{project}/units/allocation"),
        # P3.1 T15 (spec §12.6/I7): iki yeni uc de `none` izniyle 403.
        ("post", "/projects/{project}/units/bulk/preview"),
        ("post", "/projects/{project}/units/import/validate"),
        # P3.1 T14: sablon `view` ISTER — `none` yetmez (spec §12.6/I7 "ucu de").
        ("get", "/projects/{project}/units/import/template"),
    ],
)
async def test_idor_projects_permission_none_403(
    client, db_session, user_factory, project_factory, method, path
):
    """§11.4-12. `procurement` rolunde `projects`=`none` (seed matrisi) —
    projeye erisimi OLSA BILE 403; izin denetimi gorunurlukten ONCE calisir."""
    project, block, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-12")
    token = await _login_with_access(client, db_session, user_factory, "procurement")
    url = path.format(project=project.id, block=block.id, unit=unit.id)

    resp = await _call(client, method, url, _auth(token))

    assert resp.status_code == 403


# --- §11.4-13: `view` izni TUM yazma uclarini reddeder → 403 ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/projects/{project}/blocks"),
        ("patch", "/units/{unit}"),
        ("delete", "/units/{unit}"),
        ("post", "/projects/{project}/units/bulk"),
        ("post", "/projects/{project}/units/import"),
        ("patch", "/projects/{project}/units/allocation"),
        # P3.1 T15 (spec §12.6/I5): onizleme ve dogrulama da `full` ister.
        # HICBIR SEY YAZMAMALARI onlari okuma ucu YAPMAZ: ikisi de yazma
        # akisinin parcasidir ve `view` kullanicisina fiyat uretim kurallarini
        # (kat artisi, slot sablonu) ACMAZ (spec §5.4 son paragraf).
        ("post", "/projects/{project}/units/bulk/preview"),
        ("post", "/projects/{project}/units/import/validate"),
    ],
)
async def test_idor_view_permission_rejects_all_writes_403(
    client, db_session, user_factory, project_factory, method, path
):
    """§11.4-13 + spec §12.6/I5. SEKIZ yazma ucu (spec §8: yazma `full` ister). `site_chief`
    projeyi GORUR (`projects`=view + erisim satiri) ama yazamaz — okuma izniyle
    yazma izninin ayni dependency'ye baglanmadigi burada kilitlenir."""
    project, block, unit = await _fixture_project_with_unit(db_session, project_factory, "IDOR-13")
    token = await _login_with_access(client, db_session, user_factory, "site_chief")
    url = path.format(project=project.id, block=block.id, unit=unit.id)
    read = await client.get(f"/projects/{project.id}/units", headers=_auth(token))
    assert read.status_code == 200, "on kosul: bu rol projeyi GORMELI"

    resp = await _call(client, method, url, _auth(token))

    assert resp.status_code == 403


# --- §11.4-14: `projects`=admin gorunurluk suzgecini atlar ---


async def test_idor_admin_role_bypasses_visibility_200(
    client, db_session, user_factory, project_factory
):
    """§11.4-14. P1 KILITLENME KORUMASI: erisim vermek icin tum projeleri
    gorebilmek gerekir. `system_admin` erisim satiri OLMADAN 200 alir —
    `patron` ayni durumda 404 alirdi (§11.4-1) ve fark TAM OLARAK budur."""
    project, _, _ = await _fixture_project_with_unit(db_session, project_factory, "IDOR-14")
    token = await _login(client, user_factory, "system_admin")

    resp = await client.get(f"/projects/{project.id}/units", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["totals"]["counts"]["total"] == 1


# --- §11.4 son cumlesi: hicbir negatif govde kayit varligini sizdirmaz ---


async def test_idor_error_bodies_do_not_leak_record_existence(
    client, db_session, user_factory, project_factory
):
    """Spec §11.4 son cumlesi, TEK yerde toplu kanit.

    Yukaridaki testler kendi senaryolarinda `_assert_no_leak` cagirir; bu test
    negatif yanitlarin TAMAMINI yan yana koyar ve hicbirinin kayit kimligi, adi
    veya SAYISI tasimadigini gosterir. "Bu blokta 24 unite var" gibi bir mesaj
    da sizintidir: kullanicinin goremedigi bir kaydin buyuklugunu bildirir.
    """
    project, block, unit = await _fixture_project_with_unit(
        db_session, project_factory, "IDOR-15", project_type="kat_karsiligi"
    )
    await _unit(db_session, project, block, unit_no="2", owner_side=UnitOwnerSide.landowner)
    token = await _login(client, user_factory, "patron")
    secrets = (str(project.id), str(block.id), str(unit.id), project.code, block.name)

    responses = [
        await client.get(f"/projects/{project.id}/units", headers=_auth(token)),
        await client.get(f"/projects/{project.id}/blocks", headers=_auth(token)),
        await client.post(
            f"/projects/{project.id}/blocks", json={"name": "Z"}, headers=_auth(token)
        ),
        await client.patch(f"/blocks/{block.id}", json={"name": "Z"}, headers=_auth(token)),
        await client.patch(f"/units/{unit.id}", json={"unit_no": "9"}, headers=_auth(token)),
        await client.patch(
            f"/projects/{project.id}/units/allocation",
            json={"items": [{"unit_id": str(unit.id), "owner_side": "contractor"}]},
            headers=_auth(token),
        ),
        # P3.1 T15: iki yeni uc de ayni suzgecten gecer. Onizleme/dogrulama
        # yanitlari normalde SATIR SATIR veri tasir; negatif dalda o govdenin
        # hicbir kirintisi kalmamalidir.
        await client.post(
            f"/projects/{project.id}/units/bulk/preview",
            json={
                "block_id": str(block.id),
                "unit_kind": "apartment",
                "start_floor": 1,
                "end_floor": 1,
                "units_per_floor": 2,
            },
            headers=_auth(token),
        ),
        await client.post(
            f"/projects/{project.id}/units/import/validate",
            files=_file(_xlsx([["A Blok", "9", "Daire", "3+1", 120]])),
            headers=_auth(token),
        ),
    ]
    # Silme uclari 2026-07-30 karariyla `projects:admin` ister: `patron` icin
    # yanit 404 degil 403'tur. Sizinti olcutu DEGISMEZ — ayni `_assert_no_leak`
    # suzgecinden gecerler, yalnizca beklenen kod farklidir.
    delete_responses = [
        await sil_aile(client, _auth(token), "block", block.id),
        await sil_aile(client, _auth(token), "unit", unit.id),
    ]

    assert [r.status_code for r in responses] == [404] * len(responses)
    assert [r.status_code for r in delete_responses] == [403, 403]
    for resp in responses + delete_responses:
        _assert_no_leak(resp, *secrets)
        # Adet sizintisi: govdede rakam GECMEZ (mesajlar sabit Turkce metinlerdir).
        assert not any(char.isdigit() for char in resp.json()["detail"])
