"""TKL-B6.2 — `POST /offers/{id}/convert`: mutlu yol, port icerigi, on kosullar, dogrulamalar,
okuma turevi ve denetim satirlari. Izin: `test_convert_izin.py`; rollback:
`test_convert_rollback.py`; yaris: `test_convert_race.py`.

EV tarafi SAHTE kancayla taklit edilir (`tohum_kancasi`): gercek adaptor B6.3'tedir ve bu dosyanin
konusu degildir.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import contract_seed
from app.core.contract_seed import SeedWarning
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.offers.models import Offer
from app.modules.projects.models import PriceIndexType, Project, ProjectContract, ProjectType
from app.modules.sites.models import Site

from ._convert import D, Kazanilmis, govde, kalem_govdesi, kazanilmis_teklif, url
from ._offers import URL, detay, durum_yap, teklif

pytestmark = pytest.mark.usefixtures("tohum_kancasi")


@pytest.fixture
async def kz(client, admin, isveren, katalog) -> Kazanilmis:
    return await kazanilmis_teklif(client, admin, isveren, katalog, vat_pct="18")


async def _sayilar(session) -> tuple[int, int, int]:
    """(proje, sozlesme kalemi, BOQ satiri)."""
    return (
        await session.scalar(select(func.count()).select_from(Project)) or 0,
        await session.scalar(select(func.count()).select_from(EmployerContractItem)) or 0,
        await session.scalar(select(func.count()).select_from(BoqItem)) or 0,
    )


async def _proje(session, project_id: str) -> Project:
    project = await session.get(Project, uuid.UUID(project_id))
    assert project is not None
    return project


# ------------------------------------------------------------------------ mutlu yol


async def test_mutlu_yol_santiyesiz(client, admin, db_session, isveren, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)

    assert resp.status_code == 200, resp.text
    govde_ = resp.json()
    assert set(govde_) == {
        "project_id",
        "project_slug",
        "project_code",
        "site_id",
        "contract_item_count",
        "warnings",
    }
    assert govde_["site_id"] is None and govde_["contract_item_count"] == 4
    assert govde_["warnings"] == []
    project = await _proje(db_session, govde_["project_id"])
    assert project.code == govde_["project_code"] and project.slug == govde_["project_slug"]
    assert project.project_type is ProjectType.taahhut and project.is_draft is False
    assert project.employer_id == isveren.id and project.name == "A Blok Projesi"
    assert str(project.start_date) == "2026-11-01" and str(project.end_date) == "2027-10-31"
    assert project.city == "İstanbul"
    # sozlesme: bedel = Σ kalem tutari KDV HARIC (kurus); KDV teklif revizyonundan (18);
    # avans/teminat sozlesme semasi varsayilani (SO-39)
    contract = await db_session.get(ProjectContract, project.id)
    assert contract is not None
    assert contract.contract_no == "SZL-2026-01" and str(contract.signature_date) == "2026-10-15"
    assert contract.amount == Decimal("1729.95") and project.contract_amount == contract.amount
    assert contract.vat_pct == Decimal("18.00")
    assert (contract.advance_pct, contract.retainage_pct) == (Decimal("20"), Decimal("5"))
    assert contract.has_price_escalation is False and contract.index_type is None
    # santiye YOK, BOQ YOK
    assert await db_session.scalar(select(func.count()).select_from(Site)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BoqItem)) == 0
    # teklif arsivlendi
    offer = await db_session.get(Offer, uuid.UUID(kz.offer_id))
    assert offer is not None and offer.project_id == project.id
    assert offer.converted_at is not None and offer.converted_by_user_id is not None


async def test_sozlesme_gruplari_ve_kalemleri_govde_sirasiyla(
    client, admin, db_session, katalog, kz
) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)
    assert resp.status_code == 200, resp.text
    pid = uuid.UUID(resp.json()["project_id"])

    gruplar = list(
        await db_session.scalars(
            select(EmployerContractGroup)
            .where(EmployerContractGroup.project_id == pid)
            .order_by(EmployerContractGroup.sort_order)
        )
    )
    assert [(g.name, g.sort_order) for g in gruplar] == [("Kaba", 0), ("İnce", 1)]
    kalemler = list(
        await db_session.scalars(
            select(EmployerContractItem)
            .where(EmployerContractItem.project_id == pid)
            .order_by(EmployerContractItem.group_id, EmployerContractItem.sort_order)
        )
    )
    by_code = {k.code: k for k in kalemler}
    assert set(by_code) == {"B-01", "B-02", "I-01", "I-02"}
    beton = by_code["B-01"]
    # fiyat/miktar GOVDEDEN (teklif maliyeti 100 idi), katalog izi baglandi, kopya alanlar
    assert beton.unit_price == Decimal("120.50") and beton.quantity == Decimal("10")
    assert beton.catalog_item_id == katalog[0].id and beton.unit == "m3"
    assert beton.description == "Beton" and beton.price_changed_at is not None
    assert by_code["B-02"].unit_price == Decimal("99.99")
    assert by_code["I-02"].catalog_item_id == katalog[2].id  # katalogdan yeni eklenen de bagli
    assert {k.group_id for k in kalemler if k.code.startswith("B")} == {gruplar[0].id}
    assert {k.group_id for k in kalemler if k.code.startswith("I")} == {gruplar[1].id}
    assert [k.sort_order for k in kalemler if k.code.startswith("B")] == [0, 1]


async def test_mutlu_yol_santiyeli_tam_dagitim(client, admin, db_session, kz) -> None:
    resp = await client.post(
        url(kz.offer_id),
        json=govde(kz, open_site=True, site_name="A Blok Şantiyesi"),
        headers=admin,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    pid, site_id = uuid.UUID(body["project_id"]), uuid.UUID(body["site_id"])
    site = await db_session.get(Site, site_id)
    assert site is not None and site.project_id == pid and site.name == "A Blok Şantiyesi"
    assert site.code.startswith("SNT-")
    # BOQ: sozlesme grup ADLARIYLA, TUM kalemler TAM miktarla, sozlesme kalemine bagli
    boq_gruplar = list(
        await db_session.scalars(select(BoqGroup).where(BoqGroup.site_id == site_id))
    )
    assert sorted(g.name for g in boq_gruplar) == ["Kaba", "İnce"]
    satirlar = list(await db_session.scalars(select(BoqItem).where(BoqItem.site_id == site_id)))
    sozlesme = {
        k.id: k
        for k in await db_session.scalars(
            select(EmployerContractItem).where(EmployerContractItem.project_id == pid)
        )
    }
    assert len(satirlar) == 4
    for satir in satirlar:
        kalem = sozlesme[satir.contract_item_id]
        assert (satir.code, satir.quantity, satir.unit_price) == (
            kalem.code,
            kalem.quantity,
            kalem.unit_price,
        )
    grup_adi = {g.id: g.name for g in boq_gruplar}
    assert {s.code: grup_adi[s.group_id] for s in satirlar} == {
        "B-01": "Kaba",
        "B-02": "Kaba",
        "I-01": "İnce",
        "I-02": "İnce",
    }


async def test_santiye_adi_verilmezse_proje_adi(client, admin, db_session, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz, open_site=True), headers=admin)
    site = await db_session.get(Site, uuid.UUID(resp.json()["site_id"]))
    assert site is not None and site.name == "A Blok Projesi"


async def test_govdede_verilen_bedel_ve_oranlar_ezer(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["contract"].update(
        amount="5000.00",
        vat_pct="10",
        advance_pct="15",
        retainage_pct="3",
        late_penalty_daily="250",
    )
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    contract = await db_session.get(ProjectContract, uuid.UUID(resp.json()["project_id"]))
    assert contract is not None
    assert contract.amount == Decimal("5000.00") and contract.vat_pct == Decimal("10")
    assert (contract.advance_pct, contract.retainage_pct) == (Decimal("15"), Decimal("3"))
    assert contract.late_penalty_daily == Decimal("250")


async def test_bedel_kalem_tutarlari_kurusa_yuvarlanip_toplanir(
    client, admin, db_session, kz
) -> None:
    """0,5 x 0,01 = 0,005 → kalem basina 0,01 (HALF_UP); iki kalem → 0,02. Ham carpimlarin toplami
    (0,010) kurusa yuvarlansaydi 0,01 cikardi: bedel KALEM tutarlarinin toplamidir."""
    g = govde(kz)
    ornek = g["groups"][0]["items"][0]
    g["groups"] = [
        {
            "name": "Kaba",
            "items": [
                {**ornek, "code": f"K-{i}", "quantity": "0.5", "unit_price": "0.01"}
                for i in range(2)
            ],
        }
    ]
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    contract = await db_session.get(ProjectContract, uuid.UUID(resp.json()["project_id"]))
    assert contract is not None and contract.amount == Decimal("0.02")


async def test_fiyat_farki_endeksi_tuik_tekliften_varsayilan_govdeden_ezilir(
    client, admin, db_session, isveren, katalog
) -> None:
    tk = await kazanilmis_teklif(
        client, admin, isveren, katalog, price_escalation="tuik", price_index_type="ufe"
    )
    g = govde(tk)
    g["contract"].update(has_price_escalation=True, base_index_value="123.456")
    resp = await client.post(url(tk.offer_id), json=g, headers=admin)
    assert resp.status_code == 200, resp.text
    contract = await db_session.get(ProjectContract, uuid.UUID(resp.json()["project_id"]))
    assert contract is not None and contract.has_price_escalation is True
    assert contract.index_type is PriceIndexType.ufe  # teklifteki endeks
    assert contract.base_index_value == Decimal("123.456")

    ikinci = await kazanilmis_teklif(
        client, admin, isveren, katalog, price_escalation="tuik", price_index_type="ufe"
    )
    g2 = govde(ikinci)
    g2["contract"].update(has_price_escalation=True, base_index_value="100", index_type="tufe")
    resp2 = await client.post(url(ikinci.offer_id), json=g2, headers=admin)
    assert resp2.status_code == 200, resp2.text
    c2 = await db_session.get(ProjectContract, uuid.UUID(resp2.json()["project_id"]))
    assert c2 is not None and c2.index_type is PriceIndexType.tufe  # govde ezdi


# --------------------------------------------------------------------------- port


async def test_port_istegi_icerigi_santiyeli(
    client, admin, db_session, katalog, kz, tohum_kancasi
) -> None:
    g = govde(kz, open_site=True, group_disciplines={"Kaba": str(katalog[0].discipline_id)})
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    (req,) = tohum_kancasi.istekler
    assert str(req.project_id) == body["project_id"] and str(req.site_id) == body["site_id"]
    assert (str(req.start), str(req.end)) == ("2026-11-01", "2027-10-31")
    assert req.label == f"Rev.0 — {kz.offer_no}"
    assert req.actor_id is not None
    # kalemler govde sirasinda; her biri kendi sozlesme kalemine bagli
    sozlesme = {
        k.id: k.code
        for k in await db_session.scalars(
            select(EmployerContractItem).where(
                EmployerContractItem.project_id == uuid.UUID(body["project_id"])
            )
        )
    }
    assert [sozlesme[i.contract_item_id] for i in req.items] == ["B-01", "B-02", "I-01", "I-02"]
    assert [i.catalog_item_id for i in req.items] == [
        katalog[0].id,
        katalog[1].id,
        katalog[2].id,
        katalog[2].id,
    ]
    # adam-saat: Beton teklifte = katalog (1.5) → Katalog; Kalip teklifte 3 ≠ katalog 2 → TEKLIF;
    # Demir teklifte = katalog (0.75); YENI kalem (offer_item_id yok) → katalog standardi
    assert [(i.unit_mhr, i.rate_is_offer) for i in req.items] == [
        (D("1.5"), False),
        (D("3"), True),
        (D("0.75"), False),
        (D("0.75"), False),
    ]
    # grup ADI → yeni grup KIMLIGI
    kaba = await db_session.scalar(
        select(EmployerContractGroup.id).where(
            EmployerContractGroup.project_id == uuid.UUID(body["project_id"]),
            EmployerContractGroup.name == "Kaba",
        )
    )
    assert dict(req.group_disciplines) == {kaba: katalog[0].discipline_id}


async def test_port_santiyesiz_site_none_ve_disiplin_eslemesi_saklanmaz(
    client, admin, katalog, kz, tohum_kancasi
) -> None:
    g = govde(kz, group_disciplines={"Kaba": str(katalog[0].discipline_id)})
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    (req,) = tohum_kancasi.istekler
    assert req.site_id is None and len(req.items) == 4  # oran yuvasi santiyesiz de yazilir
    assert dict(req.group_disciplines) == {}  # SO-32
    (uyari,) = resp.json()["warnings"]
    assert uyari["code"] == "group_disciplines_ignored_without_site"
    assert uyari["group_name"] is None


async def test_port_uyarilari_yanita_gecer_grup_adiyla(
    client, admin, katalog, kz, tohum_kancasi
) -> None:
    tohum_kancasi.uyarilar = [SeedWarning("genel_uyari", "Genel uyarı")]
    tohum_kancasi.gruba_ozgu = True
    g = govde(kz, open_site=True, group_disciplines={"Kaba": str(katalog[0].discipline_id)})
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [
        {"code": "genel_uyari", "message": "Genel uyarı", "group_name": None},
        {"code": "grup_uyarisi", "message": "Grup uyarısı", "group_name": "Kaba"},
    ]


async def test_port_kayitsizsa_bos_uyari_ve_donusturme_yine_tamam(client, admin, kz) -> None:
    contract_seed.unregister_all()  # modulsuz kurulum (§2.7)
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []


# ----------------------------------------------------------------------- on kosullar


@pytest.mark.parametrize("durum", ["draft", "sent", "lost", "withdrawn"])
async def test_won_olmayan_teklif_409(client, admin, db_session, isveren, katalog, durum) -> None:
    kz_ = await kazanilmis_teklif(client, admin, isveren, katalog)
    o = await teklif(client, admin, isveren, title="Başka")
    await durum_yap(client, admin, o["id"], durum)
    kz_.offer_id = o["id"]  # govde baska teklifin kalemlerini tasir; 409 govdeden ONCE gelir

    resp = await client.post(url(o["id"]), json=govde(kz_), headers=admin)

    assert resp.status_code == 409, resp.text
    assert await _sayilar(db_session) == (0, 0, 0)


async def test_zaten_donusturulmus_409_ve_tek_proje(client, admin, db_session, kz) -> None:
    ilk = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)
    assert ilk.status_code == 200, ilk.text

    ikinci = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)

    assert ikinci.status_code == 409
    assert "zaten dönüştürüldü" in ikinci.text
    assert await _sayilar(db_session) == (1, 4, 0)


async def test_olmayan_teklif_404(client, admin, kz) -> None:
    resp = await client.post(url(str(uuid.uuid4())), json=govde(kz), headers=admin)
    assert resp.status_code == 404


# ---------------------------------------------------------------------- dogrulamalar


def _sil(g: dict, *yol: str) -> dict:
    node = g
    for anahtar in yol[:-1]:
        node = node[anahtar]
    del node[yol[-1]]
    return g


async def _reddedilir(client, admin, db_session, kz, g, beklenen: int, parca: str = "") -> str:
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)
    assert resp.status_code == beklenen, resp.text
    assert parca in resp.text, resp.text
    assert await _sayilar(db_session) == (0, 0, 0)  # HICBIR sey yazilmadi
    offer = await db_session.get(Offer, uuid.UUID(kz.offer_id))
    assert offer is not None and offer.project_id is None
    return resp.text


async def test_ayni_adli_iki_grup_422_SO30(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"][1]["name"] = "Kaba"
    await _reddedilir(client, admin, db_session, kz, g, 422, "Aynı adlı grup var (Kaba)")


async def test_tekrar_eden_kalem_kodu_422_SO29(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"][1]["items"][0]["code"] = "B-01"  # baska grupta da ayni kod
    await _reddedilir(client, admin, db_session, kz, g, 422, "Kalem kodu tekrar ediyor (B-01)")


async def test_birden_cok_hata_tek_gecista_toplu_mesaj(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"][1]["name"] = "Kaba"
    g["groups"][1]["items"][0]["code"] = "B-01"
    g["contract"].update(has_price_escalation=True)  # endeks + baz endeks eksik
    metin = await _reddedilir(client, admin, db_session, kz, g, 422, "Aynı adlı grup var")
    for parca in ("Kalem kodu tekrar ediyor", "contract.index_type", "contract.base_index_value"):
        assert parca in metin


async def test_olmayan_katalog_kalemi_404(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"][0]["items"][0]["catalog_item_id"] = str(uuid.uuid4())
    g["groups"][0]["items"][0]["offer_item_id"] = None
    await _reddedilir(client, admin, db_session, kz, g, 404, "Katalog")


async def test_baska_teklifin_kalemi_offer_item_id_422(
    client, admin, db_session, isveren, katalog, kz
) -> None:
    baska = await kazanilmis_teklif(client, admin, isveren, katalog)
    g = govde(kz)
    g["groups"][0]["items"][0]["offer_item_id"] = baska.kalemler["Beton"]["id"]
    await _reddedilir(client, admin, db_session, kz, g, 422, "son revizyonunda bulunamadı")


async def test_offer_item_id_ile_katalog_baglari_uyusmazsa_422(
    client, admin, db_session, katalog, kz
) -> None:
    g = govde(kz)
    g["groups"][0]["items"][0]["catalog_item_id"] = str(katalog[1].id)  # Beton satirina Kalip
    await _reddedilir(client, admin, db_session, kz, g, 422, "katalog bağı gövdedekiyle uyuşmuyor")


async def test_eski_revizyon_kalemi_offer_item_id_422(
    client, admin, db_session, isveren, katalog
) -> None:
    """`won` son durum: yeni revizyon acilamaz → bu durum yalniz baska teklif kalemiyle olusur
    (yukaridaki test). Burada kalem kimligi HIC olmayan UUID (silinmis kalem) → ayni 422."""
    kz_ = await kazanilmis_teklif(client, admin, isveren, katalog)
    g = govde(kz_)
    g["groups"][0]["items"][0]["offer_item_id"] = str(uuid.uuid4())
    await _reddedilir(client, admin, db_session, kz_, g, 422, "son revizyonunda bulunamadı")


@pytest.mark.parametrize(
    ("degisim", "parca"),
    [
        (lambda g: g.update(groups=[]), "groups"),
        (lambda g: g["groups"][0].update(items=[]), "items"),
        (lambda g: g["groups"][0]["items"][0].update(quantity="0"), "quantity"),
        (lambda g: g["groups"][0]["items"][0].update(quantity="-1"), "quantity"),
        (lambda g: g["groups"][0]["items"][0].update(quantity="1000000001"), "quantity"),
        (lambda g: g["groups"][0]["items"][0].update(unit_price="-0.01"), "unit_price"),
        (lambda g: g["groups"][0]["items"][0].update(unit_price="1000000000001"), "unit_price"),
        (lambda g: g["groups"][0]["items"][0].update(unit_price="1.234"), "unit_price"),
        (lambda g: g["groups"][0]["items"][0].update(code=" "), "code"),
        (lambda g: g["groups"][0].update(name="  "), "name"),
        (lambda g: g["project"].update(end_date="2026-10-31"), "Bitiş tarihi"),
        (lambda g: _sil(g, "project", "end_date"), "end_date"),
        (lambda g: _sil(g, "project", "city"), "city"),
        (lambda g: _sil(g, "contract", "has_price_escalation"), "has_price_escalation"),
        (lambda g: _sil(g, "contract", "contract_no"), "contract_no"),
        (lambda g: g.update(site_name="X"), "open_site"),
        (lambda g: g.update(bilinmeyen=1), "bilinmeyen"),
        (lambda g: g["contract"].update(vat_pct="101"), "vat_pct"),
    ],
)
async def test_gecersiz_govde_422_ve_hicbir_sey_yazilmaz(
    client, admin, db_session, kz, degisim, parca
) -> None:
    g = govde(kz)
    degisim(g)
    await _reddedilir(client, admin, db_session, kz, g, 422, parca)


async def test_fiyat_farki_acik_endeks_yoksa_422(client, admin, db_session, kz) -> None:
    g = govde(kz)  # teklif `fixed` → endeks turu varsayilani yok
    g["contract"].update(has_price_escalation=True, base_index_value="100")
    await _reddedilir(client, admin, db_session, kz, g, 422, "contract.index_type")


async def test_fiyat_farki_acik_baz_endeks_yoksa_422(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["contract"].update(has_price_escalation=True, index_type="ufe")
    await _reddedilir(client, admin, db_session, kz, g, 422, "contract.base_index_value")


async def test_fiyat_farki_kapaliyken_endeks_verilirse_422(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["contract"].update(index_type="ufe", base_index_value="100")
    await _reddedilir(client, admin, db_session, kz, g, 422, "Fiyat farkı kapalıyken")


async def test_gruptan_disiplin_eslemesi_gövdede_olmayan_grup_422(
    client, admin, db_session, katalog, kz
) -> None:
    g = govde(kz, group_disciplines={"Yok": str(katalog[0].discipline_id)})
    await _reddedilir(client, admin, db_session, kz, g, 422, "«Yok» adlı grup gövdede yok")


async def test_disiplin_esleme_anahtari_grup_adiyla_ayni_normalize_edilir_SO52(
    client, admin, db_session, katalog, kz, tohum_kancasi
) -> None:
    """O3: `" Kaba"` anahtari (bosluklu) `Kaba` grubuyla eslesir; 422 degil."""
    g = govde(kz, open_site=True, group_disciplines={" Kaba ": str(katalog[0].discipline_id)})
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)

    assert resp.status_code == 200, resp.text
    (req,) = tohum_kancasi.istekler
    kaba = await db_session.scalar(
        select(EmployerContractGroup.id).where(
            EmployerContractGroup.project_id == uuid.UUID(resp.json()["project_id"]),
            EmployerContractGroup.name == "Kaba",
        )
    )
    assert dict(req.group_disciplines) == {kaba: katalog[0].discipline_id}


async def test_normalize_sonrasi_cakisan_farkli_esleme_422(
    client, admin, db_session, katalog, kz
) -> None:
    g = govde(
        kz,
        open_site=True,
        group_disciplines={
            "Kaba": str(katalog[0].discipline_id),
            " Kaba": str(uuid.uuid4()),  # normalize sonrasi ayni anahtar, FARKLI deger
        },
    )
    await _reddedilir(client, admin, db_session, kz, g, 422, "birden çok eşleme")


async def test_olmayan_disiplin_404(client, admin, db_session, kz) -> None:
    g = govde(kz, group_disciplines={"Kaba": str(uuid.uuid4())})
    await _reddedilir(client, admin, db_session, kz, g, 404, "Disiplin")


# ----------------------------------------------------------------------- okuma turevi


async def test_okuma_turevi_won_not_converted_sonra_converted(
    client, admin, isveren, katalog, kz
) -> None:
    taslak = await teklif(client, admin, isveren, title="Taslak")
    liste = (await client.get(URL, headers=admin)).json()
    satir = {s["id"]: s for s in liste["items"]}
    assert satir[kz.offer_id]["conversion_state"] == "won_not_converted"
    assert satir[kz.offer_id]["project_id"] is None
    assert satir[taslak["id"]]["conversion_state"] is None
    assert liste["summary"]["won_not_converted_count"] == 1
    assert (await detay(client, admin, kz.offer_id))["conversion_state"] == "won_not_converted"

    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)
    assert resp.status_code == 200, resp.text

    liste = (await client.get(URL, headers=admin)).json()
    satir = {s["id"]: s for s in liste["items"]}
    assert satir[kz.offer_id]["conversion_state"] == "converted"
    assert satir[kz.offer_id]["project_id"] == resp.json()["project_id"]
    assert liste["summary"]["won_not_converted_count"] == 0
    d = await detay(client, admin, kz.offer_id)
    assert (d["conversion_state"], d["project_id"]) == ("converted", resp.json()["project_id"])
    assert d["status"] == "won"  # durum DEGISMEDI; donusturme iz olarak ayri


# ------------------------------------------------------------------------ denetim


async def test_denetim_satirlari_proje_olusturuldu_ve_teklif_donusturuldu(
    client, admin, db_session, kz
) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz, open_site=True), headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    site = await db_session.get(Site, uuid.UUID(body["site_id"]))
    assert site is not None

    rows = list(await db_session.scalars(select(AuditLog).order_by(AuditLog.occurred_at)))
    detaylar = {
        r.action: r.detail for r in rows if "dönüştürüldü" in r.detail or "proje" in r.detail
    }
    assert detaylar[AuditAction.create] == "Yeni proje oluşturuldu: A Blok Projesi"
    assert detaylar[AuditAction.update] == (
        f"Teklif projeye dönüştürüldü: {kz.offer_no} → {body['project_code']} "
        f"(4 kalem, şantiye {site.code})"
    )


async def test_denetim_santiyesiz_metin(client, admin, db_session, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)
    assert resp.status_code == 200, resp.text
    metinler = list(
        await db_session.scalars(
            select(AuditLog.detail).where(AuditLog.action == AuditAction.update)
        )
    )
    assert (
        f"Teklif projeye dönüştürüldü: {kz.offer_no} → {resp.json()['project_code']} (4 kalem)"
        in metinler
    )


# ------------------------------------------------------------------ sinirlar (O6: MA / MB)


def _kalemler(kz: Kazanilmis, adet: int, **over) -> list[dict]:
    k = kz.kalemler["Beton"]
    return [
        kalem_govdesi(k, f"X-{n:05d}", quantity="1", unit_price="1", **over) for n in range(adet)
    ]


def test_kalem_tavani_sinirda_kabul_asinca_sema_hatasi() -> None:
    """MA: tavan GERCEK sabittir (2000): 2000 kalem sema dogrulamasindan gecer, 2001 gecmez."""
    from pydantic import ValidationError

    from app.modules.offers.convert_schemas import CONVERT_MAX_ITEMS, ConvertRequest

    assert CONVERT_MAX_ITEMS == 2000

    def istek(adet: int) -> dict:
        satir = {
            "catalog_item_id": str(uuid.uuid4()), "code": "", "description": "x", "unit": "m3",
            "quantity": "1", "unit_price": "1",
        }  # fmt: skip
        kalemler = [{**satir, "code": f"X-{n:05d}"} for n in range(adet)]
        return {
            "project": {"name": "P", "city": "C", "start_date": "2026-01-01",
                        "end_date": "2026-12-31"},
            "contract": {"contract_no": "S", "signature_date": "2026-01-01",
                         "has_price_escalation": False},
            "groups": [{"name": "G1", "items": kalemler[:1000]},
                       {"name": "G2", "items": kalemler[1000:]}],
        }  # fmt: skip

    assert sum(len(g.items) for g in ConvertRequest.model_validate(istek(2000)).groups) == 2000
    with pytest.raises(ValidationError, match="en fazla 2000 kalem"):
        ConvertRequest.model_validate(istek(2001))


async def test_2001_kalem_422_hicbir_sey_yazilmaz(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"] = [
        {"name": "G1", "items": _kalemler(kz, 1000)},
        {
            "name": "G2",
            "items": [{**i, "code": f"Y-{n:05d}"} for n, i in enumerate(_kalemler(kz, 1001))],
        },
    ]
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)
    assert resp.status_code == 422, resp.text[:300]
    assert "en fazla 2000 kalem" in resp.text
    assert await _sayilar(db_session) == (0, 0, 0)


async def test_bedel_siniri_asilirsa_anlamli_422(client, admin, db_session, kz) -> None:
    """MB: Σ kalem tutari `Numeric(18,2)` sinirina (1e16) ESIT ya da USTUNDEYSE 422 + acik metin,
    yazma YOK (sinir dahil: `>=`)."""
    g = govde(kz)
    g["groups"] = g["groups"][:1]
    g["groups"][0]["items"] = g["groups"][0]["items"][:1]
    g["groups"][0]["items"][0].update(quantity="10000", unit_price="1000000000000")  # tam 1e16
    metin = await _reddedilir(
        client, admin, db_session, kz, g, 422, "Kalem toplamı sözleşme bedeli sınırını aşıyor"
    )
    assert "contract.amount" in metin


async def test_bedel_sinirin_hemen_altinda_kabul(client, admin, db_session, kz) -> None:
    g = govde(kz)
    g["groups"] = g["groups"][:1]
    g["groups"][0]["items"] = g["groups"][0]["items"][:1]
    g["groups"][0]["items"][0].update(quantity="10000", unit_price="999999999999.99")  # 1e16-100
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)
    assert resp.status_code == 200, resp.text[:300]


async def test_2000_kalem_sinirda_kabul_edilir(client, admin, db_session, kz) -> None:
    """MA (uctan uca): tavandaki 2000 kalem kabul edilir (gercek sabit, sahte kanca)."""
    g = govde(kz)
    g["groups"] = [
        {"name": "G1", "items": _kalemler(kz, 1000)},
        {
            "name": "G2",
            "items": [{**i, "code": f"Y-{n:05d}"} for n, i in enumerate(_kalemler(kz, 1000))],
        },
    ]
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["contract_item_count"] == 2000
