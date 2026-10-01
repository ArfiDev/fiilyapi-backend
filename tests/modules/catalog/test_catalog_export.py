"""TKL-B5.2 — fiyatli Is Kalemi Katalogu Excel ciktisi.

Sahte son fiyat saglayicisi `app.core.last_price` portuna kayitlidir; cagri sayaci toplu
`latest` (tek cagri) kuralini olcer, SQL sayaci sorgu sayisinin kalem sayisindan bagimsizligini.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from sqlalchemy import event, select

from app.core import last_price
from app.core.access import AccessLevel, Scope
from app.core.last_price import LastPrice
from app.modules.catalog.export import COLUMN_HEADERS
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from app.modules.catalog.service import next_poz_no
from app.modules.earned_value.models import UserDiscipline
from app.modules.users.models import User

from .._boq import _auth, _login_with_access, _set_permission

URL = "/catalog/items"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class FakeLastPrice:
    def __init__(self) -> None:
        self.veri: dict[uuid.UUID, LastPrice] = {}
        self.cagrilar: list[list[uuid.UUID]] = []

    async def __call__(self, session, ids):  # noqa: ANN001
        self.cagrilar.append(list(ids))
        return {i: self.veri[i] for i in ids if i in self.veri}


@pytest.fixture
def son_fiyat():
    foto = last_price.registered()
    last_price.unregister_all()
    sahte = FakeLastPrice()
    last_price.register_provider("SZL", sahte)
    yield sahte
    last_price.restore(foto)


@pytest.fixture
async def admin(client, db_session, user_factory, seeded_db):
    token = await _login_with_access(
        client, db_session, user_factory, "system_admin", f"a.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


async def _kalem(db, disiplin: EvDiscipline, ad: str, **over) -> EvCatalogItem:
    kalem = EvCatalogItem(
        poz_no=await next_poz_no(db, disiplin),
        discipline_id=disiplin.id,
        name=ad,
        uom="m3",
        standard_unit_mhr=over.pop("mhr", Decimal("1.5")),
        default_contractor_type=over.pop("tur", ContractorType.OWN),
        **over,
    )
    db.add(kalem)
    await db.flush()
    return kalem


@pytest.fixture
async def disiplin(seeded_db) -> EvDiscipline:
    d = EvDiscipline(
        code="KEX", name="Kaba Yapı", color="#2563eb", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(d)
    await seeded_db.flush()
    return d


def _satirlar(resp) -> list[tuple]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(XLSX)
    sayfa = openpyxl.load_workbook(BytesIO(resp.content)).active
    return [tuple(c.value for c in satir) for satir in sayfa.iter_rows()]


@contextmanager
def _sayac():
    from tests.conftest import test_engine

    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


async def test_basliklar_sabit_dosya_adi_ve_satirlar(
    client, admin, seeded_db, disiplin, son_fiyat
) -> None:
    beton = await _kalem(
        seeded_db,
        disiplin,
        "Beton",
        ref_price=Decimal("100.00"),
        price_updated_at=datetime(2026, 2, 3, 9, 0, tzinfo=UTC),
        mhr=Decimal("2.5"),
    )
    kalip = await _kalem(seeded_db, disiplin, "Kalıp", tur=ContractorType.SUBCON)
    son_fiyat.veri[beton.id] = LastPrice(
        Decimal("123.45"), datetime(2026, 3, 1, 9, 0, tzinfo=UTC), "SZL", "PRJ-A", None
    )
    resp = await client.get(f"{URL}/export", headers=admin)
    satirlar = _satirlar(resp)
    assert COLUMN_HEADERS == (
        "Poz No",
        "Disiplin",
        "İş Kalemi",
        "Birim",
        "Referans Fiyat",
        "Fiyat Güncelleme",
        "Son Fiyat",
        "Kaynak",
        "Belge",
        "Tarih",
        "Adam-saat/birim",
        "Varsayılan Yüklenici",
    )
    assert satirlar[0] == COLUMN_HEADERS
    assert satirlar[1][:5] == (beton.poz_no, "Kaba Yapı", "Beton", "m3", "100.00")
    assert satirlar[1][5:10] == ("03.02.2026", "123.45", "SZL", "PRJ-A", "01.03.2026")
    assert satirlar[1][11] == "Kendi"
    assert satirlar[2][0] == kalip.poz_no
    assert satirlar[2][4:10] == (None,) * 6  # fiyatsiz / son fiyatsiz kalem: BOS hucre
    assert satirlar[2][11] == "Taşeron"
    disp = resp.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert "filename*=UTF-8''%C4%B0%C5%9F%20Kalemi%20Katalo%C4%9Fu.xlsx" in disp
    # FLOAT-YASAK: her dolu hucre str
    assert all(isinstance(v, str) for s in satirlar for v in s if v is not None)
    # API yanitiyla birebir
    api = (await client.get(URL, headers=admin)).json()["items"]
    assert [a["poz_no"] for a in api] == [s[0] for s in satirlar[1:]]
    assert api[0]["last_price"]["price"] == satirlar[1][6]
    assert api[0]["ref_price"] == satirlar[1][4]
    assert api[0]["standard_unit_mhr"] == satirlar[1][10]


async def test_limited_kapsamda_fiyat_hucreleri_bos(
    client, db_session, user_factory, seeded_db, disiplin, son_fiyat
) -> None:
    kalem = await _kalem(
        seeded_db,
        disiplin,
        "Beton",
        ref_price=Decimal("100.00"),
        price_updated_at=datetime(2026, 2, 3, 9, 0, tzinfo=UTC),
    )
    son_fiyat.veri[kalem.id] = LastPrice(
        Decimal("123.45"), datetime(2026, 3, 1, tzinfo=UTC), "SZL", "PRJ-A", None
    )
    await _set_permission(db_session, "accounting", "contracts", AccessLevel.view, Scope.limited)
    token = await _login_with_access(
        client, db_session, user_factory, "accounting", f"lim.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    satir = _satirlar(await client.get(f"{URL}/export", headers=_auth(token)))[1]
    assert satir[:4] == (kalem.poz_no, "Kaba Yapı", "Beton", "m3")
    assert satir[4:10] == (None,) * 6
    api = (await client.get(URL, headers=_auth(token))).json()["items"][0]
    assert satir[10] == api["standard_unit_mhr"] and satir[10] is not None  # adam-saat gorunur


@pytest.mark.parametrize("role_key", ["site_chief", "field_engineer"])
async def test_contracts_yok_roller_403_ve_kimliksiz_401(
    client, db_session, user_factory, role_key
) -> None:
    assert (await client.get(f"{URL}/export")).status_code == 401
    token = await _login_with_access(
        client, db_session, user_factory, role_key, f"{role_key}.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    assert (await client.get(f"{URL}/export", headers=_auth(token))).status_code == 403


async def test_suzgecler_liste_ucuyla_ayni(client, admin, seeded_db, disiplin, son_fiyat) -> None:
    await _kalem(seeded_db, disiplin, "Beton")
    await _kalem(seeded_db, disiplin, "Demir")
    satirlar = _satirlar(await client.get(f"{URL}/export?q=demir", headers=admin))
    assert [s[2] for s in satirlar[1:]] == ["Demir"]
    yabanci = uuid.uuid4()
    satirlar = _satirlar(await client.get(f"{URL}/export?discipline_id={yabanci}", headers=admin))
    assert len(satirlar) == 1  # yalniz baslik


async def test_son_fiyat_TEK_toplu_cagri_ve_sorgu_sayisi_kalem_sayisindan_bagimsiz(
    client, admin, seeded_db, disiplin, son_fiyat
) -> None:
    await _kalem(seeded_db, disiplin, "A")
    with _sayac() as az:
        assert (await client.get(f"{URL}/export", headers=admin)).status_code == 200
    assert len(son_fiyat.cagrilar) == 1
    for ad in ("B", "C", "D", "E"):
        await _kalem(seeded_db, disiplin, ad)
    son_fiyat.cagrilar.clear()
    with _sayac() as cok:
        yanit = await client.get(f"{URL}/export", headers=admin)
    assert len(_satirlar(yanit)) == 6  # baslik + 5 kalem
    assert len(son_fiyat.cagrilar) == 1 and len(son_fiyat.cagrilar[0]) == 5
    assert len(cok) == len(az)


async def test_disiplin_kisitli_kullanici_yalniz_kendi_disiplinini_indirir(
    client, db_session, user_factory, seeded_db, disiplin, son_fiyat
) -> None:
    diger = EvDiscipline(
        code="KEY", name="Mekanik", color="#2563eb", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(diger)
    await seeded_db.flush()
    await _kalem(seeded_db, disiplin, "Beton")
    await _kalem(seeded_db, diger, "Boru")
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "pm.kisitli.export@tkl.co"
    )
    uid = (
        await db_session.execute(select(User.id).where(User.email == "pm.kisitli.export@tkl.co"))
    ).scalar_one()
    db_session.add(UserDiscipline(user_id=uid, discipline_id=diger.id))
    await db_session.flush()
    satirlar = _satirlar(await client.get(f"{URL}/export", headers=_auth(token)))
    assert [s[2] for s in satirlar[1:]] == ["Boru"]
