"""IZN-B2 ONARIM — `/auth/me.permissions` (TÜRETİLMİŞ) ile FRONTEND DÜĞME KARARI paritesi.

Frontend düğme kapılarını `useModulePermission(modül)` + `canView` (`level !== none`) · `canWrite`
(`≥ draft`) · `canDelete` (`admin`) · `hasAtLeast(level, eşik)` ile verir (`frontend/src/lib/auth`).
Karar yalnız `/auth/me.permissions[modül]` seviyesinden çıkar; sayfa bazlı modelde bu harita
SAYFA HÜCRELERİNDEN türetilir (`page_gate.display_level`). Test: 8 seed rol × FE'nin okuduğu her
(modül, eşik) için "FE kararı eski == yeni". Eski karar donmuş seed `MATRIX`ten, yeni karar gerçek
`GET /auth/me` yanıtından hesaplanır.

EŞİK LİSTESİ (`frontend/src` taramasından, `/usr/bin/grep` + betik; salt okuma):
* `useModulePermission("<modül>")` çağrıları: approvals, boq, contracts, documents, earned_value,
  equipment, inventory, payroll, personnel, progress_payments, projects, sales, site_diary, sites,
  timesheet, user_management; SABİT adla çağıranlar (`*_PERMISSION_MODULE`): accounting, invoicing,
  procurement, equipment, treasury, payroll, ai.
* Eşikler: `canView` (view), `canWrite` (draft), `hasAtLeast(·, "draft"|"approve"|"full"|"admin")`,
  `canDelete` (admin). Modül başına eşik kümesi `FE_ESIKLERI` sabitindedir.
"""

import pytest

from app.core.access import AccessLevel, satisfies
from app.modules.roles import seed_data

L = AccessLevel

#: FE'nin okuduğu (modül → eşikler): `useModulePermission` çağrıları + `hasAtLeast(·, "…")`
#: literalleri + `canView`/`canWrite`/`canDelete` kullanımı (dosya dosya tarandı):
#:   accounting: PeriodClosingView `full` (Kapat) · `admin` (Yeniden Aç) · canWrite · canView
#:   approvals: ApprovalRolesScreen `admin` (eşik düzenleme)
#:   payroll: PayrollRatesScreen `full` (oran) · `admin` (vergi dilimi) · canWrite
#:   contracts/projects: Offers* WRITE_LEVEL(`full`), PROJECTS_ADMIN_LEVEL(`admin`), EMPLOYER_ADD
#:   site_diary: SiteDiaryEntryView `admin` (Yeniden Aç) · canWrite
#:   personnel/boq/equipment/documents/stock: `full` (form + liste yazma kapıları)
#:   earned_value: submit/daily/revision `draft`,`approve` · katalog `full`,`admin`
#:   progress_payments: status-actions.ts `draft`, `approve` (onayla/ödendi), `admin` (geri al)
#:   timesheet/sales/sites/treasury/procurement/invoicing/user_management/ai: canView · canWrite
FE_ESIKLERI: dict[str, tuple[AccessLevel, ...]] = {
    "accounting": (L.view, L.draft, L.full, L.admin),
    "approvals": (L.view, L.draft, L.admin),
    "payroll": (L.view, L.draft, L.full, L.admin),
    "contracts": (L.view, L.draft, L.full, L.admin),
    "projects": (L.view, L.draft, L.full, L.admin),
    "site_diary": (L.view, L.draft, L.admin),
    "personnel": (L.view, L.draft, L.full),
    "boq": (L.view, L.draft, L.full),
    "equipment": (L.view, L.draft, L.full),
    "documents": (L.view, L.draft, L.full),
    "inventory": (L.view, L.draft, L.full),
    "earned_value": (L.view, L.draft, L.approve, L.full, L.admin),
    "progress_payments": (L.view, L.draft, L.approve, L.admin),
    "timesheet": (L.view, L.draft, L.full),
    "sales": (L.view, L.draft),
    "sites": (L.view, L.draft),
    "treasury": (L.view, L.draft),
    "procurement": (L.view, L.draft),
    "invoicing": (L.view, L.draft, L.admin),
    "user_management": (L.view, L.draft),
    "ai": (L.view,),
}


def _fe_karar(level: AccessLevel, esik: AccessLevel) -> bool:
    """FE'nin düğme kararı: `view` = `level !== none`; diğerleri `hasAtLeast` sıralaması."""
    if esik is L.view:
        return level is not L.none
    return satisfies(level, esik)


async def _me_permissions(client, user_factory, role_key: str) -> dict[str, AccessLevel]:
    email = f"{role_key}@fe-parite.co"
    await user_factory(email=email, password="parola1234", role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = (await client.get("/auth/me", headers=headers)).json()
    return {m: AccessLevel(v) for m, v in me["permissions"].items()}


@pytest.mark.parametrize("role_key", seed_data.ROLE_ORDER)
async def test_fe_dugme_karari_eski_esittir_yeni(client, user_factory, seeded_db, role_key) -> None:
    index = seed_data.ROLE_ORDER.index(role_key)
    eski = {module: cells[index][0] for module, cells in seed_data.MATRIX.items()}
    yeni = await _me_permissions(client, user_factory, role_key)
    farklar = []
    for module, esikler in FE_ESIKLERI.items():
        for esik in esikler:
            e = _fe_karar(eski[module], esik)
            y = _fe_karar(yeni.get(module, L.none), esik)
            if e != y:
                farklar.append((module, esik.value, eski[module].value, yeni.get(module)))
    assert farklar == [], f"{role_key}: FE kararı değişti (modül, eşik, eski, yeni): {farklar}"


async def test_fe_okudugu_moduller_haritada_var(client, user_factory, seeded_db) -> None:
    yeni = await _me_permissions(client, user_factory, "accounting")
    assert set(FE_ESIKLERI) <= set(yeni), "FE'nin okuduğu modül `/auth/me.permissions`ta yok"
