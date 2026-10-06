"""IZN-B2 — KAPI PARİTE TESTİ (en önemli bekçi): "eski kapı kararı = yeni kapı kararı".

İLKE (CEO): geçişte kimsenin fiilî yetkisi DEĞİŞMEZ — ne genişleme ne daralma.

* ESKİ karar: B1 öncesi `role_permissions` modeli — rolün modül düzeyi (`seed_data.MATRIX` ya da
  test içinde kurulan hücre) kapının `(modül, düzey)`ini karşılıyor mu (`access.satisfies`).
* YENİ karar: gerçek kapı bağımlılıkları (`require_permission`/`require_any_permission`/
  `require_page`) DB'deki SAYFA HÜCRELERİ üzerinde çalıştırılır.
* Kapılar rota tablosundan (`iter_route_contexts`) kapanış değişkenlerinden okunur; yani
  "hangi uç hangi kapıyı taşıyor" sorusu elle yazılmış bir listeden DEĞİL uygulamadan gelir.
  Zincir adımı ikamesi (`require_permission_or_chain_step`) modül kapısı yönünden değerlendirilir
  (ikame OK-1C bekçilerindedir).

KAPSAM: 8 seed rol × TÜM rotalar (DELETE'ler SIL-B1 sonrası `require_system_admin` kapısındadır:
parite DIŞI, ayrı karar testiyle — yalnız Sistem Yöneticisi geçer) + her modül × her düzey için
"ekrandan değiştirilmiş" özel roller + çok modüllü kombinasyonlar. Kasıtlı farklar AŞAĞIDAKİ
LİSTEDEDİR ve test o listeyi BİREBİR bekler: listede olmayan her fark KIRMIZI.

KASITLI FARKLAR (hepsi `admin` düzeyi / silme anlamlı; seed rollerinde SIFIR fark):
1. `POST /progress-payments/{id}/unapprove`, `POST /subcontractor-progress-payments/{id}/unapprove`
   ("Onayı Geri Al"): `progress_payments=admin` verilmiş ÖZEL rol artık geçmez; yalnız Sistem
   Yöneticisi (CEO kararı 1). Seed rollerinde fark yok (admin yalnız Sistem Yöneticisi'nde).
2. `PATCH /users/{id}/password` ve `PUT /roles/{id}/permissions/{module}` (410): `user_management=
   admin` verilmiş özel rol artık geçmez (parola sıfırlama yalnız Sistem Yöneticisi; plan §2.4 KUL-D
   önerisi parite için uygulanmadı — CEO'ya soruldu).
3. SİL (KESİNLEŞTİ, SIL-B1): her DELETE ucu `require_system_admin` kapısındadır (yeni kapı türü
   `sa`): YALNIZ Sistem Yöneticisi geçer (KARARLAR §1.7 K4, istisna yok). Eski `(modül, düzey)`
   kararıyla KARŞILAŞTIRILMAZ; "eski ≠ yeni" farkı bilinçlidir ve `test_her_delete_ucu_*`
   testleri kapıyı doğrudan sınar (yalnız `system_admin` geçer, diğer 7 seed rol 403).
"""

import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.access import AccessLevel, Scope, satisfies
from app.core.page_gate import effective_level, gate_ok, pages_ok
from app.main import app
from app.modules.roles import seed_data
from app.modules.roles.models import Role
from app.modules.roles.schemas import RoleCreate
from app.modules.roles.service import create_custom_role
from app.modules.users.models import User
from tests._legacy_permission_yardimcisi import update_role_permission

L = AccessLevel
MODULES = [m["key"] for m in seed_data.MODULES]
SEED_ROLES = list(seed_data.ROLE_ORDER)

#: `require_page` kapılarının ESKİ karşılığı: bayrağın yerine geçtiği `(modül, düzey)` VE'si.
#: Yeni bir `require_page` kapısı eklenirse BURAYA eski kapı yazılmadan test kırmızıdır.
_PV = frozenset(  # projects:view sayfaları
    {
        "genel.projeler",
        "genel.proje_takvimi",
        "mali.satis_blok",
        "mali.satis_unite",
        "mali.satis_excel",
        "mali.satis_paylasim",
        "proje.ozet",
        "proje.paylasim_tablosu",
    }
)
_B5C_BOLUM = frozenset(
    {
        "bolum.detay",
        "bolum.is_kalemleri",
        "bolum.puantaj",
        "bolum.malzeme",
        "bolum.hakedis",
        "bolum.gunluk_kayit",
        "bolum.gunluk_kayit_detay",
    }
)
_B5C_SITE_GORUR = frozenset(
    {
        "proje.santiyeler",
        "santiye.bolumler",
        "santiye.is_kalemleri",
        "santiye.puantaj",
        "santiye.stok",
        "santiye.hakedisler",
        "santiye.gunluk_kayit",
        "santiye.belgeler",
        "santiye.bolum_dagilimi",
        "santiye.gunluk_ozet",
        "santiye.gunluk_planlama",
        "santiye.adam_saat_butcesi",
        "santiye.planlama_paneli",
        "santiye.gunluk_ilerleme_raporu",
        "santiye.haftalik_qurr",
    }
    | _B5C_BOLUM
)
_B5C_SECTION_GORUR = frozenset({"santiye.bolumler", "santiye.gunluk_kayit"} | _B5C_BOLUM)
_B5C_SECTIONS_LISTE_GORUR = frozenset(
    {
        "santiye.bolumler",
        "bolum.detay",
        "santiye.stok",
        "santiye.puantaj",
        "santiye.gunluk_planlama",
    }
)
_HAKEDIS_ISVEREN = frozenset(
    {"mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler"}
)
# IZN-B5b madde 6 (CEO kararı 2): `santiye.hakedisler` yalnız işveren ailesi.
_HAKEDIS_TASERON = frozenset({"mali.hakedis_taseron", "proje.taseron_hakedis"})
_EV_BUTCE = frozenset({"planlama.adam_saat_butcesi", "santiye.adam_saat_butcesi"})
PAGE_GATE_OLD: dict[tuple[frozenset[str], str], list[tuple[str, AccessLevel]]] = {
    (frozenset({"mali.donem_kapanisi"}), "approve"): [("accounting", L.admin)],
    (frozenset({"ayarlar.onay_rolleri"}), "edit"): [("approvals", L.admin)],
    # IZN-B3 onarımı: yeniden aç kapısı kök sayfa + proje içi ikizleri (73, 88); hücreler aynı.
    (
        frozenset({"saha.gunluk_kayit", "santiye.gunluk_kayit", "bolum.gunluk_kayit_detay"}),
        "approve",
    ): [("site_diary", L.admin)],
    (frozenset({"teklif.teklif_hazirlama"}), "approve"): [
        ("projects", L.admin),
        ("contracts", L.full),
    ],
    (frozenset({"ayarlar.bordro_oranlari"}), "edit"): [("payroll", L.admin)],
    (frozenset({"genel.projeler"}), "edit"): [("projects", L.admin)],
    (frozenset({"ayarlar.rol_yonetimi"}), "edit"): [("user_management", L.admin)],
    (frozenset({"ayarlar.sayfa_izinleri"}), "edit"): [("user_management", L.admin)],
    # IZN-B5a madde 15: rol ayrıntı uçları (GET) yalnız rol ekranlarının Görür'üyle açılır; eski
    # kapı `user_management=view`di (bilinçli DARALMA, aşağıdaki B5A_KASITLI_ROTALAR).
    (frozenset({"ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"}), "view"): [
        ("user_management", L.view)
    ],
    # Onay eylemi uçları (IZN-B2 onarım): sayfa ONAYLAR bayrağı ↔ eski modül kapısı.
    (frozenset({"ik.izin_yonetimi"}), "approve"): [("personnel", L.full)],
    (frozenset({"mali.yevmiye"}), "approve"): [("accounting", L.full)],
    (frozenset({"mali.fatura"}), "approve"): [("invoicing", L.full)],
    (frozenset({"mali.bordro"}), "approve"): [("payroll", L.full)],
    (frozenset({"mali.sgk_bildirimi"}), "approve"): [("payroll", L.full)],
    (frozenset({"mali.cek_odeme"}), "approve"): [("treasury", L.full)],
    (frozenset({"mali.satis"}), "approve"): [("sales", L.full)],
    (frozenset({"saha.makine_kira"}), "approve"): [("equipment", L.full)],
    (frozenset({"stok.teklif_karsilastirma"}), "approve"): [("procurement", L.full)],
    (frozenset({"stok.satinalma_talepleri"}), "approve"): [("procurement", L.approve)],
    # --- IZN-B5b madde 7 (C): sözleşme/teklif/katalog yazmaları sayfa başına (eski: contracts=full)
    (frozenset({"teklif.teklif_hazirlama"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.sablonlar"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.is_kalemi_katalogu"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.taseron_firmalar"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.taseron_sozlesme"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.taseron_firmalar", "teklif.sozlesmeler"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.sozlesmeler", "teklif.taseron_sozlesme"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.isveren_sozlesme", "proje.is_kalemleri"}), "edit"): [("contracts", L.full)],
    (frozenset({"teklif.poz_dagilimi", "proje.is_kalemleri"}), "edit"): [("contracts", L.full)],
    # --- IZN-B5b (A): satış sekmeleri · /costs · muhasebe · makine
    (frozenset({"mali.satis_blok"}), "edit"): [("projects", L.full)],
    (frozenset({"mali.satis_unite"}), "edit"): [("projects", L.full)],
    (frozenset({"mali.satis_toplu_uretim"}), "edit"): [("projects", L.full)],
    (frozenset({"mali.satis_excel"}), "edit"): [("projects", L.full)],
    (frozenset({"mali.satis_paylasim"}), "edit"): [("projects", L.full)],
    (
        frozenset(
            {"genel.projeler", "genel.proje_takvimi", "proje.ozet", "proje.paylasim_tablosu"}
        ),
        "view",
    ): [("projects", L.view)],
    (frozenset({"mali.hesap_plani"}), "edit"): [("accounting", L.full)],
    (frozenset({"mali.yevmiye"}), "edit"): [("accounting", L.full)],
    (frozenset({"mali.donem_kapanisi"}), "edit"): [("accounting", L.full)],
    (frozenset({"saha.makine_ekipman"}), "edit"): [("equipment", L.full)],
    (frozenset({"saha.makine_calisma"}), "edit"): [("equipment", L.full)],
    (frozenset({"saha.makine_yakit"}), "edit"): [("equipment", L.full)],
    (frozenset({"saha.makine_kira"}), "edit"): [("equipment", L.full)],
    # belge bağlama yazmaları (B5b yan bulgu): eski kapı `sales:view` (bilinçli daralma, aşağıda)
    (frozenset({"mali.satis"}), "edit"): [("sales", L.view)],
    # --- IZN-B5b (B): hakediş aileleri · planlama
    (_HAKEDIS_ISVEREN, "edit"): [("progress_payments", L.draft)],
    (_HAKEDIS_TASERON, "edit"): [("progress_payments", L.draft)],
    (_EV_BUTCE, "edit"): [("earned_value", L.draft)],
    (frozenset({"ayarlar.planlama"}), "edit"): [("earned_value", L.draft)],
    (frozenset({"planlama.disiplin_yonetimi"}), "edit"): [("earned_value", L.full)],
    (frozenset({"planlama.birim_oran_katalogu"}), "edit"): [("earned_value", L.full)],
    (_HAKEDIS_ISVEREN, "approve"): [("progress_payments", L.approve)],
    (_HAKEDIS_TASERON, "approve"): [("progress_payments", L.approve)],
    (
        frozenset({"planlama.adam_saat_butcesi", "santiye.adam_saat_butcesi"}),
        "approve",
    ): [("earned_value", L.approve)],
    (
        frozenset({"planlama.gunluk_rapor", "santiye.gunluk_ilerleme_raporu"}),
        "approve",
    ): [("earned_value", L.approve)],
    (
        frozenset(
            {
                "planlama.gunluk_rapor",
                "santiye.gunluk_ilerleme_raporu",
                "planlama.adam_saat_butcesi",
                "santiye.adam_saat_butcesi",
            }
        ),
        "approve",
    ): [("earned_value", L.approve)],
    # --- IZN-B5c: sites yazmaları sayfa başına (eski: sites=full) · madde 16 · dar görme
    (frozenset({"proje.santiyeler"}), "edit"): [("sites", L.full)],
    (frozenset({"santiye.bolumler"}), "edit"): [("sites", L.full)],
    (frozenset({"bolum.detay"}), "edit"): [("sites", L.full)],
    (frozenset({"santiye.bolumler", "bolum.detay"}), "edit"): [("sites", L.full)],
    (frozenset({"proje.santiyeler", "santiye.bolumler", "bolum.detay"}), "view"): [
        ("sites", L.view)
    ],
    (_PV | {"proje.santiyeler"}, "view"): [("projects", L.view)],
    (_B5C_SITE_GORUR, "view"): [("sites", L.view)],
    (_B5C_SECTION_GORUR, "view"): [("sites", L.view)],
    (_B5C_SECTIONS_LISTE_GORUR, "view"): [("sites", L.view)],
    # IZN-B5d: panel ucu (kart kümeleri servis içi, `test_izn_b5d_panel_kartlari.py`).
    (frozenset({"genel.gosterge_paneli"}), "view"): [("dashboard", L.view)],
}

#: Kasıtlı fark listesi (yukarıdaki docstring). `(yöntem, yol)` kümeleri. DELETE'ler listede YOK:
#: SIL-B1 sonrası `sa` kapısı parite dışıdır (ayrı karar testi).
UNAPPROVE_ROTALARI = {
    ("POST", "/progress-payments/{payment_id}/unapprove"),
    ("POST", "/subcontractor-progress-payments/{payment_id}/unapprove"),
}
ADMIN_USER_MGMT_ROTALARI = {
    ("PATCH", "/users/{user_id}/password"),
    ("PUT", "/roles/{role_id}/permissions/{module_key}"),
}


#: IZN-B5a BİLİNÇLİ FARKLAR (CEO onaylı sızıntı onarımı): eski kapıyla karşılaştırılmaz, kendi
#: davranış testleri `tests/modules/test_izn_b5a_hizli_a.py`dedir.
#: * madde 15: `GET /roles` · `/modules` · `/roles/{id}/permissions` · `/roles/{id}/pages` artık
#:   `user_management=view` (= `ayarlar.kullanicilar` Görür) ile AÇILMAZ (daralma).
#: * madde 8: `PUT /payroll/rates/{year}/{source}` artık `payroll=full` değil
#:   `ayarlar.bordro_oranlari` Düzenler ister (vergi dilimi ucuyla aynı kapı).
B5A_KASITLI_ROTALAR = frozenset(
    {
        ("GET", "/roles"),
        ("GET", "/modules"),
        ("GET", "/roles/{role_id}/permissions"),
        ("GET", "/roles/{role_id}/pages"),
        ("PUT", "/payroll/rates/{year}/{source}"),
    }
)

#: IZN-B5b BİLİNÇLİ DARALMALAR (CEO onaylı, API düzeyi; kendi davranış testleri `test_izn_b5b_*`):
#: * madde 1-A: `PATCH /projects/{id}` artık `projects:full` değil `genel.projeler` Düzenler ister
#:   (seed'de yalnız `patron` ve `project_manager` kaybeder; FE'de çağıran ekran yok).
#: * yan bulgu: belge bağlama/künye yazmaları artık `<sahip>:view` değil sahibin ANA sayfasının
#:   Düzenler'i (`mali.satis_unite` · `mali.satis` · `teklif.taseron_sozlesme`). Bu tablo bu
#:   uçların eski kapısını ana sayfa komşusunun kapısıyla eşlediği için yalnız `sales` ucu fark
#:   olarak GÖRÜNÜR (`sales:view` → `mali.satis` Düzenler); diğerleri aynı gerekçeyle listededir.
B5B_KASITLI_ROTALAR = frozenset(
    {
        ("PATCH", "/projects/{project_id}"),
        ("POST", "/sales/{owner_id}/documents"),
        ("PATCH", "/sales/documents/{link_id}"),
        ("POST", "/units/{owner_id}/documents"),
        ("PATCH", "/units/documents/{link_id}"),
        ("POST", "/subcontractor-contracts/{owner_id}/documents"),
        ("PATCH", "/subcontractor-contracts/documents/{link_id}"),
    }
)

#: IZN-B5c BİLİNÇLİ FARKLAR (CEO onaylı; kendi davranış testleri `test_izn_b5c.py`):
#: * bölüm belgesi bağlama/künye yazması artık `sites:view` değil `bolum.detay` Düzenler'i
#:   (seed'de 10 rol daralır; anahtar `sites:full`'a eşlendiği için fark burada görünmez, gerekçeyle
#:   listededir — B5b belge bağlama kayıtlarıyla aynı).
#: * `POST /projects/{id}/sites` ve `PATCH /sites/{id}` artık `sites:full` değil `proje.santiyeler`
#:   Düzenler'i ister (özel rol kombinasyonu `sites=full ∧ projects=none` daralır; seed 0).
#: * dar görme genişlemesi: `GET /sites/{id}` · `/sections/{id}` · `/sites/{id}/sections` artık iç
#:   sayfa Görür'ü ile de açılır (seed'de yalnız `warehouse_keeper` kazanır; `test_izn_b5c.py`).
B5C_KASITLI_ROTALAR = frozenset(
    {
        ("POST", "/sections/{owner_id}/documents"),
        ("PATCH", "/sections/documents/{link_id}"),
        ("POST", "/projects/{project_id}/sites"),
        ("PATCH", "/sites/{site_id}"),
        ("GET", "/sites/{site_id}"),
        ("GET", "/sections/{section_id}"),
        ("GET", "/sites/{site_id}/sections"),
    }
)


# ---------------------------------------------------------------------------
# Rota tablosundan kapıları oku
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    kind: str  # perm | chain | any | page | sa (SIL-B1: yalnız Sistem Yöneticisi)
    spec: tuple  # perm/chain: (modül, düzey) · any: ((modül, düzey), ...) · page: (sayfa, bayrak)
    fn: object = None  # çağrılabilir kapı (perm/any/page) — chain'de None
    multi_project: bool = False  # yalnız page: `require_page(..., multi_project=True)` (B5b)

    @property
    def key(self) -> tuple:
        return (self.kind, self.spec, self.multi_project)


def _closure(fn) -> dict:
    names = fn.__code__.co_freevars
    cells = fn.__closure__ or ()
    return {name: cell.cell_contents for name, cell in zip(names, cells, strict=True)}


def _walk(dependant, out: list[Gate], seen: set[int]) -> None:
    for sub in dependant.dependencies:
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        fn = sub.call
        if hasattr(fn, "__code__"):
            c = _closure(fn)
            if fn.__qualname__.startswith("require_system_admin."):
                out.append(Gate("sa", ("system_admin",), fn))
            elif "module_key" in c and "min_level" in c and "document_type" in c:
                out.append(Gate("chain", (frozenset(c["page_keys"]), "approve")))
            elif "module_key" in c and "min_level" in c:
                out.append(Gate("perm", (c["module_key"], c["min_level"]), fn))
            elif "gates" in c:
                out.append(Gate("any", tuple(c["gates"]), fn))
            elif "page_key" in c and "flag" in c:
                out.append(
                    Gate(
                        "page",
                        (frozenset({c["page_key"]}), c["flag"]),
                        fn,
                        bool(c.get("multi_project", False)),
                    )
                )
            elif "page_keys" in c and "flag" in c:
                out.append(
                    Gate(
                        "page",
                        (frozenset(c["page_keys"]), c["flag"]),
                        fn,
                        bool(c.get("multi_project", False)),
                    )
                )
        _walk(sub, out, seen)


def route_gates() -> dict[tuple[str, str], list[Gate]]:
    table: dict[tuple[str, str], list[Gate]] = {}
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant = ctx.dependant or ctx.original_route.dependant
        gates: list[Gate] = []
        _walk(dependant, gates, set())
        for method in ctx.methods:
            table[(method, ctx.path)] = gates
    return table


ROTALAR = route_gates()
DELETE_ROTALARI = sorted(r for r in ROTALAR if r[0] == "DELETE")


# ---------------------------------------------------------------------------
# Eski karar (donmuş modül matrisi) ↔ yeni karar (sayfa hücreleri)
# ---------------------------------------------------------------------------


def old_gate(levels: dict[str, AccessLevel], gate: Gate) -> bool:
    if gate.kind == "sa":
        raise AssertionError("`sa` kapısı parite DIŞIDIR (route_decisions atlar)")
    if gate.kind == "perm":
        module, level = gate.spec
        return satisfies(levels.get(module, L.none), level)
    if gate.kind == "any":
        return any(satisfies(levels.get(m, L.none), lv) for m, lv in gate.spec)
    # page + chain (zincir adımı ikamesi hariç: sayfa ONAYLAR kolu) → eski modül kapısı
    return all(satisfies(levels.get(m, L.none), lv) for m, lv in PAGE_GATE_OLD[gate.spec])


async def new_gate(session, user: User, gate: Gate) -> bool:
    if gate.kind == "chain":
        pages, flag = gate.spec
        return await pages_ok(session, user, tuple(pages), flag)
    try:
        await gate.fn(user=user, session=session)  # type: ignore[operator]
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


async def route_decisions(session, user: User, levels: dict[str, AccessLevel]):
    """Her rota için (eski, yeni) karar; kapı başına sonuç önbelleklenir."""
    cache: dict[tuple, bool] = {}
    farklar: list[tuple[str, str]] = []
    for route, gates in ROTALAR.items():
        eski = True
        yeni = True
        for gate in gates:
            if gate.key not in cache:
                cache[gate.key] = await new_gate(session, user, gate)
            if gate.kind == "sa":
                continue  # SIL-B1: eski kararla karşılaştırılmaz; ayrı karar testi sınar
            eski = eski and old_gate(levels, gate)
            yeni = yeni and cache[gate.key]
        if (
            eski != yeni
            and route not in B5A_KASITLI_ROTALAR | B5B_KASITLI_ROTALAR | B5C_KASITLI_ROTALAR
        ):
            farklar.append(route)
    return farklar


async def _kullanici(session, role_key: str, email: str) -> User:
    role = (await session.execute(select(Role).where(Role.key == role_key))).scalar_one()
    from app.core.security import hash_password
    from app.modules.users.models import UserStatus

    session.add(
        User(
            email=email,
            password_hash=hash_password("x"),
            full_name="Parite",
            role_id=role.id,
            status=UserStatus.active,
        )
    )
    await session.flush()
    return (
        await session.execute(
            select(User)
            .options(joinedload(User.role))
            .where(User.email == email)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _seed_levels(role_key: str) -> dict[str, AccessLevel]:
    index = seed_data.ROLE_ORDER.index(role_key)
    return {module: cells[index][0] for module, cells in seed_data.MATRIX.items()}


async def _ozel_rol(session, levels: dict[str, AccessLevel], tag: str) -> User:
    role = await create_custom_role(
        session, RoleCreate(key=tag, name=tag, emoji="", description="")
    )
    for module, level in levels.items():
        await update_role_permission(session, role.id, module, level, Scope.all)
    return await _kullanici(session, tag, f"{tag}@parite.co")


# ---------------------------------------------------------------------------
# Yapısal testler
# ---------------------------------------------------------------------------


def test_rota_tablosu_okundu_ve_kapi_turleri_tanindi() -> None:
    # IZN-B3: +2 (`GET`/`PUT /users/{user_id}/access`) → 464
    assert len(ROTALAR) == 464
    turler = {g.kind for gates in ROTALAR.values() for g in gates}
    assert turler == {"perm", "chain", "any", "page", "sa"}
    sayfa_kapilari = {
        g.spec for gates in ROTALAR.values() for g in gates if g.kind in ("page", "chain")
    }
    assert sayfa_kapilari == set(PAGE_GATE_OLD), "her require_page kapısının ESKİ karşılığı tabloda"


def test_her_delete_ucu_yalniz_sistem_yoneticisi_kapisini_tasir() -> None:
    """SIL-B1: DELETE uçları `sa` kapısındadır ve başka MODÜL kapısı taşımaz (karar kesinleşti)."""
    assert len(DELETE_ROTALARI) >= 47
    for route in DELETE_ROTALARI:
        turler = {g.kind for g in ROTALAR[route]}
        assert "sa" in turler, f"DELETE ucu Sistem Yöneticisi kapısız: {route}"
        assert not turler & {"perm", "any", "page", "chain"}, (route, turler)


@pytest.mark.parametrize("role_key", SEED_ROLES)
async def test_delete_kapisinda_yalniz_sistem_yoneticisi_gecer(seeded_db, role_key) -> None:
    user = await _kullanici(seeded_db, role_key, f"{role_key}@sa-kapi.co")
    for route in DELETE_ROTALARI:
        for gate in ROTALAR[route]:
            if gate.kind == "sa":
                assert await new_gate(seeded_db, user, gate) is (role_key == "system_admin"), (
                    role_key,
                    route,
                )


def test_admin_duzeyi_genel_kapida_yalniz_sistem_yoneticisi_bayraksiz() -> None:
    from app.core.page_gate import gate_flags

    for module in MODULES:
        assert gate_flags(module, L.admin) == (), module


def test_her_kullanilan_modul_duzey_kapisinin_bayragi_var_ya_da_admin() -> None:
    """Uygulamada kullanılan HER `(modül, düzey)` kapısı: bayrağı olmalı (admin hariç)."""
    from app.core.page_gate import gate_flags

    kullanilan: set[tuple[str, AccessLevel]] = set()
    for gates in ROTALAR.values():
        for g in gates:
            if g.kind == "perm":
                kullanilan.add(g.spec)
            elif g.kind == "any":
                kullanilan.update(g.spec)
    bayraksiz = {(m, lv) for m, lv in kullanilan if lv is not L.admin and not gate_flags(m, lv)}
    # SIL-B1: bütçe taslağı sil artık `sa` kapısında; bayraksız modül kapısı KALMADI.
    assert bayraksiz == set(), sorted(bayraksiz)


# ---------------------------------------------------------------------------
# PARİTE: 8 seed rol × tüm rotalar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_key", SEED_ROLES)
async def test_seed_rolu_tum_rotalarda_eski_karar_eşittir_yeni_karar(seeded_db, role_key) -> None:
    user = await _kullanici(seeded_db, role_key, f"{role_key}@parite.co")
    farklar = await route_decisions(seeded_db, user, _seed_levels(role_key))
    assert farklar == [], f"{role_key}: eski ≠ yeni kapı kararı: {farklar}"


# ---------------------------------------------------------------------------
# PARİTE: ekrandan değiştirilmiş hücreler (her modül × her düzey)
# ---------------------------------------------------------------------------

#: "admin" dışındaki düzeylerde TÜM rotalarda SIFIR fark; admin'de yalnız kasıtlı fark listesi.
SEVIYELER = [L.none, L.view, L.draft, L.request, L.approve, L.full, L.admin]


def _kasitli_fark_beklenen(module: str, level: AccessLevel) -> set[tuple[str, str]]:
    """Bu senaryoda (modül=level, diğerleri none) YALNIZ beklenen farklar (DELETE hariç)."""
    if level is not L.admin:
        return set()
    beklenen: set[tuple[str, str]] = set()
    if module == "progress_payments":
        beklenen |= UNAPPROVE_ROTALARI
    if module == "user_management":
        beklenen |= ADMIN_USER_MGMT_ROTALARI
    return beklenen


@pytest.mark.parametrize("module", MODULES)
async def test_ekrandan_degistirilmis_hucre_her_duzeyde_parite(seeded_db, module) -> None:
    for level in SEVIYELER:
        tag = f"sc_{module}_{level.value}"
        user = await _ozel_rol(seeded_db, {module: level}, tag)
        farklar = await route_decisions(seeded_db, user, {module: level})
        # DELETE'ler `sa` kapısındadır: parite dışı (route_decisions atlar), fark üretmez.
        assert set(farklar) == _kasitli_fark_beklenen(module, level), (
            module,
            level,
            sorted(farklar),
        )


async def test_cok_modullu_kombinasyonlarda_parite_ve_donustur_kapisi(seeded_db) -> None:
    senaryolar = {
        "donustur_acik": {"projects": L.admin, "contracts": L.full},
        "donustur_projects_full": {"projects": L.full, "contracts": L.full},
        "donustur_sadece_projects_admin": {"projects": L.admin},
        "mali_tam": {"accounting": L.full, "invoicing": L.full, "treasury": L.full},
        "saha": {"site_diary": L.full, "timesheet": L.full, "earned_value": L.approve},
        "satinalma": {"procurement": L.approve, "inventory": L.full, "contracts": L.view},
        "hakedis_onay_ve_sozlesme": {"progress_payments": L.approve, "contracts": L.full},
    }
    for tag, levels in senaryolar.items():
        user = await _ozel_rol(seeded_db, levels, f"cm_{tag}")
        farklar = await route_decisions(seeded_db, user, levels)
        diger = {r for r in farklar if r not in ADMIN_USER_MGMT_ROTALARI}
        assert diger == set(), (tag, sorted(diger))
    # Dönüştür (teklif "Onaylar"): yalnız İKİ koşulun birleşimi.
    convert = ("POST", "/offers/{offer_id}/convert")
    for tag, beklenen in (
        ("donustur_acik", True),
        ("donustur_projects_full", False),
        ("donustur_sadece_projects_admin", False),
    ):
        user = await _kullanici(seeded_db, f"cm_{tag}", f"cm_{tag}@parite2.co")
        for gate in ROTALAR[convert]:
            if gate.kind == "page":
                assert await new_gate(seeded_db, user, gate) is beklenen, tag


# ---------------------------------------------------------------------------
# Servis içi okumalar: effective_level / gate_ok (get_permission'ın karşılığı)
# ---------------------------------------------------------------------------

#: Uygulamada servis içinde sorulan `(modül, düzey)` çiftleri (can_read/get_permission okuyanlar).
SERVIS_KAPILARI = [
    ("site_diary", L.view),
    ("progress_payments", L.view),
    ("projects", L.view),
    ("inventory", L.view),
    ("earned_value", L.view),
    ("payroll", L.view),
    ("earned_value", L.draft),
    ("procurement", L.full),
    ("procurement", L.request),
    ("progress_payments", L.admin),
    ("procurement", L.admin),
    ("personnel", L.admin),
    ("projects", L.admin),
]


@pytest.mark.parametrize("role_key", SEED_ROLES)
async def test_servis_ici_kapilar_seed_rollerinde_eski_kararla_ayni(seeded_db, role_key) -> None:
    user = await _kullanici(seeded_db, role_key, f"srv_{role_key}@parite.co")
    levels = _seed_levels(role_key)
    for module, level in SERVIS_KAPILARI:
        if (module, level) == ("projects", L.admin):
            # `visible_projects` admin istisnası: "Projeler Düzenler" bayrağı = eski admin eşiği.
            from app.core.page_gate import page_ok

            yeni = await page_ok(seeded_db, user, "genel.projeler", "edit")
        else:
            yeni = await gate_ok(seeded_db, user, module, level)
        assert yeni is satisfies(levels.get(module, L.none), level), (role_key, module, level)


@pytest.mark.parametrize("role_key", SEED_ROLES)
async def test_effective_level_esik_karsilastirmalari_eski_duzeyle_ayni(
    seeded_db, role_key
) -> None:
    """`procurement.actor_level` gibi `satisfies(seviye, X)` soran okumalar: X ∈ {view..full}."""
    user = await _kullanici(seeded_db, role_key, f"eff_{role_key}@parite.co")
    levels = _seed_levels(role_key)
    # Her modül için YALNIZ bayrağı olan düzeyler sorulur (ara düzeyler sayfa hücresinde ayrışmaz).
    # `effective_level` Onaylar bayrağını GÖSTERMEZ (onay uçları sayfa bayrağından geçer);
    # modül kapısına bayrak veren düzeyler sorulur.
    sorulan = {
        "procurement": (L.view, L.request, L.full),
        "progress_payments": (L.view, L.draft),
        "site_diary": (L.view, L.full),
        "earned_value": (L.view, L.draft, L.full),
    }
    for module, esikler in sorulan.items():
        yeni = await effective_level(seeded_db, user, module)
        for esik in esikler:
            assert satisfies(yeni, esik) is satisfies(levels[module], esik), (
                role_key,
                module,
                esik,
            )


# ---------------------------------------------------------------------------
# Kapsam (maske) hibriti: CEO kararı
# ---------------------------------------------------------------------------


async def test_kapsam_hibriti_eski_satirli_rolde_donmus_scope_satirsiz_rolde_tum_tutarlar(
    seeded_db,
) -> None:
    from app.modules.roles.models import Role
    from app.modules.roles.repository import derived_role_matrix

    async def actor_scope(session, user, modul):
        # IZN-B6a: `core.permissions.actor_scope` söküldü; aynı kuralı görüntü matrisi taşır.
        rol = await session.get(Role, user.role_id)
        matris = await derived_role_matrix(session, rol.id, rol.key)
        return next(scope for m, _lvl, scope in matris if m.key == modul)

    from app.modules.roles.models import HiddenCategory, RoleHiddenField

    muhasebe = await _kullanici(seeded_db, "accounting", "muh@scope.co")
    sef = await _kullanici(seeded_db, "site_chief", "sef@scope.co")
    # Muhasebe `finance` maskesi AYNEN (parite): donmuş eski satırdan.
    assert await actor_scope(seeded_db, muhasebe, "boq") is Scope.finance
    assert await actor_scope(seeded_db, sef, "boq") is Scope.limited
    assert await actor_scope(seeded_db, sef, "site_diary") is Scope.all

    # Eski satırı OLAN rolde kaydedilen gizli alan maskeyi DEĞİŞTİRMEZ.
    seeded_db.add(RoleHiddenField(role_id=muhasebe.role_id, category=HiddenCategory.tum_tutarlar))
    await seeded_db.flush()
    assert await actor_scope(seeded_db, muhasebe, "boq") is Scope.finance

    # Satırsız rol (özel/kopya): tum_tutarlar → limited, değilse all (fail-closed).
    kapali = await _ozel_rol(seeded_db, {}, "satirsiz_gizli")
    seeded_db.add(RoleHiddenField(role_id=kapali.role_id, category=HiddenCategory.tum_tutarlar))
    acik = await _ozel_rol(seeded_db, {}, "satirsiz_acik")
    await seeded_db.flush()
    assert await actor_scope(seeded_db, kapali, "boq") is Scope.limited
    assert await actor_scope(seeded_db, acik, "boq") is Scope.all


# ---------------------------------------------------------------------------
# Uçtan uca (HTTP) kablolama: kapılar GERÇEKTEN köprüden geçiyor
# ---------------------------------------------------------------------------


async def _giris(client, user_factory, role_key: str) -> dict:
    email = f"{role_key}.{uuid.uuid4().hex[:6]}@http.co"
    await user_factory(email=email, password="parola1234", role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_hakedis_onay_kapisi_muhasebe_gecer_sef_gecemez_unapprove_yalniz_sisyon(
    client, user_factory, seeded_db
) -> None:
    yok = uuid.uuid4()
    muh = await _giris(client, user_factory, "accounting")
    sef = await _giris(client, user_factory, "site_chief")
    admin = await _giris(client, user_factory, "system_admin")
    onay = f"/progress-payments/{yok}/approve"
    # Kapıdan geçen istek kayda ulaşır (404), kapıda kalan 403 alır.
    assert (await client.post(onay, json={}, headers=muh)).status_code == 404
    assert (await client.post(onay, json={}, headers=sef)).status_code == 403
    geri_al = f"/progress-payments/{yok}/unapprove"
    assert (await client.post(geri_al, headers=admin)).status_code == 404
    for rol in ("accounting", "project_manager", "patron"):
        basliklar = await _giris(client, user_factory, rol)
        assert (await client.post(geri_al, headers=basliklar)).status_code == 403, rol
    taseron = f"/subcontractor-progress-payments/{yok}/unapprove"
    assert (await client.post(taseron, headers=muh)).status_code == 403
    assert (await client.post(taseron, headers=admin)).status_code == 404


async def test_sayfa_ekranindan_yazilan_hucre_kapiya_aninda_yansir(
    client, user_factory, seeded_db
) -> None:
    """PUT /roles/{id}/pages ile Şef'e Hakedişler Onaylar verilince onay kapısı açılır, geri
    alınınca kapanır (hücre → kapı bağı uçtan uca)."""
    yok = uuid.uuid4()
    admin = await _giris(client, user_factory, "system_admin")
    sef = await _giris(client, user_factory, "site_chief")
    sef_rol = (await seeded_db.execute(select(Role).where(Role.key == "site_chief"))).scalar_one()
    onay = f"/progress-payments/{yok}/approve"
    assert (await client.post(onay, json={}, headers=sef)).status_code == 403

    mevcut = (await client.get(f"/roles/{sef_rol.id}/pages", headers=admin)).json()
    mevcut["pages"]["mali.hakedis_isveren"] = {"level": "edit", "approve": True}
    put = await client.put(f"/roles/{sef_rol.id}/pages", json=mevcut, headers=admin)
    assert put.status_code == 200, put.text
    assert (await client.post(onay, json={}, headers=sef)).status_code == 404

    mevcut["pages"]["mali.hakedis_isveren"] = {"level": "view", "approve": False}
    # Aynı sayfanın ikizleri (VEYA kuralı) hâlâ Düzenler+Onaylar: onları da kapat.
    for ikiz in ("proje.isveren_hakedis", "santiye.hakedisler"):
        mevcut["pages"][ikiz] = {"level": "view", "approve": False}
    assert (
        await client.put(f"/roles/{sef_rol.id}/pages", json=mevcut, headers=admin)
    ).status_code == 200
    assert (await client.post(onay, json={}, headers=sef)).status_code == 403


async def test_yeni_rol_atanabilir_ve_kapidan_gecer(client, user_factory, seeded_db) -> None:
    await seed_data.seed_izn_reference_data(seeded_db)
    admin = await _giris(client, user_factory, "system_admin")
    rol = (await seeded_db.execute(select(Role).where(Role.key == "finance_manager"))).scalar_one()
    olustur = await client.post(
        "/users",
        json={
            "email": "fm@http.co",
            "password": "parola1234",
            "full_name": "FM",
            "role_id": str(rol.id),
        },
        headers=admin,
    )
    assert olustur.status_code == 201, olustur.text
    login = await client.post("/auth/login", json={"email": "fm@http.co", "password": "parola1234"})
    fm = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Finans Müdürü = Muhasebe + mali onaylar: yevmiye listesi açık, kullanıcı yönetimi kapalı.
    assert (await client.get("/journal-entries", headers=fm)).status_code == 200
    assert (await client.get("/users", headers=fm)).status_code == 403


async def test_atama_kurali_kendi_sayfa_hucrelerini_asan_rolu_atayamaz(
    client, user_factory, seeded_db
) -> None:
    """`require_assignable_role`: Sistem Yöneticisi olmayan aktör, kendi hücrelerini (düzey +
    onay) aşan rolü atayamaz (eski modül karşılaştırmasının karşılığı)."""
    from tests._legacy_permission_yardimcisi import update_role_permission as yaz

    aktor_rol = await create_custom_role(
        seeded_db, RoleCreate(key="ik_yonetici", name="İK Yönetici", emoji="", description="")
    )
    for modul, seviye in (("user_management", L.full), ("accounting", L.view)):
        await yaz(seeded_db, aktor_rol.id, modul, seviye, Scope.all)
    aktor = await _giris(client, user_factory, "ik_yonetici")
    accounting = (
        await seeded_db.execute(select(Role).where(Role.key == "accounting"))
    ).scalar_one()  # tam yetkili (accounting full): aktörü AŞAR
    viewer_gibi = await create_custom_role(
        seeded_db, RoleCreate(key="dar_rol", name="Dar", emoji="", description="")
    )
    await yaz(seeded_db, viewer_gibi.id, "accounting", L.view, Scope.all)

    asan = await client.post(
        "/users",
        json={
            "email": "asan@http.co",
            "password": "parola1234",
            "full_name": "A",
            "role_id": str(accounting.id),
        },
        headers=aktor,
    )
    assert asan.status_code == 403, asan.text
    uygun = await client.post(
        "/users",
        json={
            "email": "uygun@http.co",
            "password": "parola1234",
            "full_name": "U",
            "role_id": str(viewer_gibi.id),
        },
        headers=aktor,
    )
    assert uygun.status_code == 201, uygun.text
