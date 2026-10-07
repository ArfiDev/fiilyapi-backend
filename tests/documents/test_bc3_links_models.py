"""BC-3 — model katmanı bekçileri (DB'siz): şekil, kısıtlar, tek enum, seed parite.

Her bekçinin yanında bir POZİTİF KONTROL vardır (K-IKIZ1): "kısıt VAR" iddiası
metadata'dan okunur, kısıtın METNİ de ölçülür — adı var gövdesi yanlış bir
CHECK bu testleri yeşil geçiremez.
"""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.core.db import Base
from app.modules.documents import link_owners
from app.modules.documents.models import (
    ENTITY_DOCUMENT_SCOPE,
    EntityDocumentScope,
    EntityDocumentType,
    SectionDocument,
    SubcontractorContractDocument,
    UnitDocument,
    UnitSaleDocument,
)
from tests.documents.conftest import load_bc3_migration

LINK_TABLES = {
    "section_documents": ("section_id", "sections", "section"),
    "unit_documents": ("unit_id", "units", "unit"),
    "unit_sale_documents": ("unit_sale_id", "unit_sales", "unit_sale"),
    "subcontractor_contract_documents": (
        "subcontractor_contract_id",
        "subcontractor_contracts",
        "subcontractor_contract",
    ),
}


def test_bes_tablo_metadata_da_kayitli() -> None:
    for tablo in ("entity_document_types", *LINK_TABLES):
        assert tablo in Base.metadata.tables, tablo


def test_TEK_enum_nesnesi_bes_tabloda_paylasilir() -> None:
    """`JOURNAL_SOURCE_TYPE` kanonu: beş kolon AYNI `Enum` nesnesini taşır —
    ayrı nesneler `create_all`de "type already exists" ile küme düşürürdü."""
    tipler = {
        id(Base.metadata.tables[t].c.scope.type) for t in ("entity_document_types", *LINK_TABLES)
    }
    assert tipler == {id(ENTITY_DOCUMENT_SCOPE)}
    assert ENTITY_DOCUMENT_SCOPE.name == "entity_document_scope"


def test_katalog_iki_UNIQUE_tasir_id_scope_bilesik_FKnin_hedefidir() -> None:
    tablo = Base.metadata.tables["entity_document_types"]
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in tablo.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert uniques["uq_entity_document_types_scope_code"] == ("scope", "code")
    assert uniques["uq_entity_document_types_id_scope"] == ("id", "scope")


def _fks(tablo):
    return [c for c in tablo.constraints if isinstance(c, ForeignKeyConstraint)]


def test_her_bag_tablosu_ucu_FK_ve_scope_CHECK_tasir() -> None:
    for tablo_adi, (sahip_kolonu, sahip_tablosu, scope) in LINK_TABLES.items():
        tablo = Base.metadata.tables[tablo_adi]
        fk_map = {
            tuple(col.name for col in fk.columns): (fk.referred_table.name, fk.ondelete)
            for fk in _fks(tablo)
        }
        # sahip: CASCADE · arşiv: SET NULL · (type_id, scope): bileşik, RESTRICT
        assert fk_map[(sahip_kolonu,)] == (sahip_tablosu, "CASCADE"), tablo_adi
        assert fk_map[("document_id",)] == ("documents", "SET NULL"), tablo_adi
        assert fk_map[("type_id", "scope")] == ("entity_document_types", "RESTRICT"), tablo_adi

        checks = {
            c.name: str(c.sqltext) for c in tablo.constraints if isinstance(c, CheckConstraint)
        }
        assert checks[f"ck_{tablo_adi}_scope"] == f"scope = '{scope}'", tablo_adi
        assert tablo.c.document_id.nullable is True, "SET NULL nullable kolon ister"
        assert tablo.c.type_id.nullable is False, "slot sabittir, XOR yok"


def test_POZITIF_KONTROL_bilesik_FK_hedef_kolonlari_katalogun_UNIQUE_ciftidir() -> None:
    """Bileşik FK yanlış kolona (örn. yalnız `id`) baksaydı yukarıdaki test
    `("type_id","scope")` anahtarını bulamazdı; burada hedefin BİREBİR `(id, scope)`
    olduğu da ölçülür."""
    tablo = Base.metadata.tables["section_documents"]
    fk = next(f for f in _fks(tablo) if f.name == "fk_section_documents_type_scope")
    assert [e.column.name for e in fk.elements] == ["id", "scope"]


def test_her_scope_uyesinin_bir_sahibi_var_ve_kokler_ayrik() -> None:
    assert set(link_owners.SPEC_BY_SCOPE) == set(EntityDocumentScope)
    kokler = [s.route_root for s in link_owners.OWNER_SPECS]
    assert len(kokler) == len(set(kokler)) == 4
    modeller = {
        link_owners.SECTION.link_model: SectionDocument,
        link_owners.UNIT.link_model: UnitDocument,
        link_owners.UNIT_SALE.link_model: UnitSaleDocument,
        link_owners.SUBCONTRACTOR_CONTRACT.link_model: SubcontractorContractDocument,
    }
    assert all(k is v for k, v in modeller.items())


def test_izin_anahtari_MEVCUT_modullerdir_yeni_modul_ACILMADI() -> None:
    from app.core.sayfalar import MODUL_ANAHTARLARI

    tohumlu = set(MODUL_ANAHTARLARI)
    for spec in link_owners.OWNER_SPECS:
        assert spec.permission_module in tohumlu, spec.key
    assert {s.permission_module for s in link_owners.OWNER_SPECS} == {
        "sites",
        "projects",
        "sales",
        "contracts",
    }


# --- migration ↔ model parite ---


def test_migration_b4d7e1c9f2a3_uzerine_tek_revizyon() -> None:
    migration = load_bc3_migration()
    assert migration.revision == "c5d8e2f1a4b7"
    assert migration.down_revision == "b4d7e1c9f2a3"
    assert migration.ENUM_NAME == ENTITY_DOCUMENT_SCOPE.name
    assert set(migration.SCOPES) == {s.value for s in EntityDocumentScope}


def test_seed_18_satir_3_3_6_6_ve_dort_zorunlu_mockuptan_birebir() -> None:
    """Ölçüm (2026-09-04): Bolum Ekle 3 · Unite Ekle 3 · Daire Satisi 6 (2 `*`) ·
    Sözleşme Oluştur 6 (2 `*`). Zorunlular: Satış Sözleşmesi · Alıcı Kimlik ·
    İmzalı Sözleşme · SGK Borcu Yoktur Yazısı."""
    seed = load_bc3_migration().SLOT_SEED
    assert len(seed) == 18
    sayim: dict[str, int] = {}
    for scope, _code, _name, _req, _ord in seed:
        sayim[scope] = sayim.get(scope, 0) + 1
    assert sayim == {"section": 3, "unit": 3, "unit_sale": 6, "subcontractor_contract": 6}
    zorunlu = {code for _s, code, _n, req, _o in seed if req}
    assert zorunlu == {"sales_contract", "buyer_id", "signed_contract", "sgk_clearance"}
    # (scope, code) tekil; sort_order bölme içinde 1..n
    assert len({(s, c) for s, c, *_ in seed}) == 18
    for scope in sayim:
        assert sorted(o for s, *_r, o in seed if s == scope) == list(range(1, sayim[scope] + 1))


def test_seed_tablosu_katalog_modeliyle_ayni_kolonlari_doldurur() -> None:
    """Seed'in beş alanı modelin beş kolonuna düşer; kolon adı değişirse burası kırılır."""
    kolonlar = set(EntityDocumentType.__table__.c.keys())
    assert {"scope", "code", "name", "is_required", "sort_order"} <= kolonlar
