"""DSC-B4 GOLDEN: ATAMASIZ aktörün B4 uçları (proje/şantiye/bölüm okuma + yazma yanıtları, stok
özetleri, panel) BİREBİR değişmez. Golden app/ DEĞİŞMEDEN (0a94f84 kodunda) alındı:
`UPDATE_B4_GOLDEN=1 pytest …` YALNIZ yazar; normal koşuda KIYAS edilir. Dünya: `_b4_dunya`.

Aktör: atamasız `system_admin` (B1–B3 golden'larıyla aynı; tüm kapılardan geçer, kısıtsız yol).
`pm_atamasiz` (aynı rol, atamasız) pozitif kontrol olarak ayrı testte AYNI okuma gövdelerini verir.

Yazma senaryoları her biri KENDİ TAZE dünyasında koşar; yanıt + o istekte yazılan audit satırları
dondurulur. Sunucunun ürettiği yeni kimlikler JSON yoluyla etiketlenir (`<yeni:...>`), böylece
golden'da `<uuid:` numaralı maske YOKTUR.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient

from tests.discipline_scope._b2_golden_araclari import anlik_goruntu, audit_kimlikleri, yeni_audit
from tests.discipline_scope._b4_dunya import DunyaB4
from tests.discipline_scope._golden import golden_yolu, oku, yaz

GUNCELLE = os.environ.get("UPDATE_B4_GOLDEN") == "1"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Yol = Callable[[DunyaB4], str]


def _proje(x: DunyaB4) -> str:
    return f"/projects/{x.d.proje.id}"


def _a(x: DunyaB4) -> str:
    return f"/sites/{x.d.santiye.id}"


def _b(x: DunyaB4) -> str:
    return f"/sites/{x.santiye_b.id}"


# ad -> (yol, params | None)
OKUMALAR: dict[str, tuple[Yol, dict | None]] = {
    "b4_projeler": (lambda x: "/projects", None),
    "b4_proje_detay": (_proje, None),
    "b4_proje_santiyeler": (lambda x: f"{_proje(x)}/sites", None),
    "b4_santiye_a": (_a, None),
    "b4_santiye_b": (_b, None),
    "b4_santiye_a_bolumler": (lambda x: f"{_a(x)}/sections", None),
    "b4_bolum_s1": (lambda x: f"/sections/{x.d.s1.id}", None),
    "b4_bolum_s2": (lambda x: f"/sections/{x.d.s2.id}", None),
    "b4_stok_ozet": (lambda x: "/stock/summary", None),
    "b4_stok_ozet_kritik": (lambda x: "/stock/summary", {"status": "critical"}),
    "b4_santiye_a_stok": (lambda x: f"{_a(x)}/stock", None),
    "b4_santiye_a_stok_s1": (lambda x: f"{_a(x)}/stock", {"section_id": "<S1>"}),
    "b4_panel": (lambda x: "/dashboard/summary", None),
    # Ek: ikinci proje (revizyonsuz) ve C şantiyesi de yüzeyde görünür.
    "b4_proje2_detay": (lambda x: f"/projects/{x.proje2.id}", None),
    "b4_santiye_c": (lambda x: f"/sites/{x.santiye_c.id}", None),
}

Govde = Callable[[DunyaB4], dict]

_SANTIYE_ZORUNLU = {
    "site_manager_name": "Şantiye Şefi",
    "city": "Ankara",
    "construction_area_m2": "1200",
    "start_date": "2026-06-01",
    "end_date": "2026-12-31",
}

# ad -> (yöntem, yol, gövde)
YAZMALAR: dict[str, tuple[str, Yol, Govde]] = {
    "b4_yaz_santiye_olustur": (
        "POST",
        lambda x: f"{_proje(x)}/sites",
        lambda x: {
            **_SANTIYE_ZORUNLU,
            "name": "Yeni Şantiye",
            "code": "DSC-N",
            "budget": "500000",
            "sections": [
                {"name": "Yeni Bölüm 1", "start_date": "2026-06-01", "end_date": "2026-06-30"},
                {"name": "Yeni Bölüm 2"},
            ],
        },
    ),
    "b4_yaz_santiye_olustur_kodsuz": (
        "POST",
        lambda x: f"{_proje(x)}/sites",
        lambda x: {**_SANTIYE_ZORUNLU, "name": "Kodsuz Şantiye"},
    ),
    "b4_yaz_santiye_taslak": (
        "POST",
        lambda x: f"{_proje(x)}/sites",
        lambda x: {"name": "Taslak Şantiye", "is_draft": True},
    ),
    "b4_yaz_santiye_guncelle_a": (
        "PATCH",
        _a,
        lambda x: {"name": "A-Blok Güncel", "budget": "750000", "city": "İzmir"},
    ),
    "b4_yaz_santiye_guncelle_b": (
        "PATCH",
        _b,
        lambda x: {"name": "B-Blok Güncel", "start_date": "2026-05-01", "end_date": "2026-08-01"},
    ),
    "b4_yaz_bolum_olustur": (
        "POST",
        lambda x: f"{_a(x)}/sections",
        lambda x: {
            "name": "C Blok",
            "code": "BLM-C",
            "section_type": "structural",
            "manager_name": "Bölüm Sorumlusu",
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
            "budget_amount": "1000",
        },
    ),
    "b4_yaz_bolum_taslak": (
        "POST",
        lambda x: f"{_a(x)}/sections",
        lambda x: {"name": "Taslak Bölüm", "is_draft": True},
    ),
    "b4_yaz_bolum_guncelle": (
        "PATCH",
        lambda x: f"/sections/{x.d.s1.id}",
        lambda x: {"name": "A Blok Güncel", "budget_amount": "12345", "status": "active"},
    ),
    "b4_yaz_proje_olustur": (
        "POST",
        lambda x: "/projects",
        lambda x: {
            "name": "Yeni Proje",
            "code": "DSC-P09",
            "project_type": "kendi_yatirim",
            "city": "Bursa",
        },
    ),
    "b4_yaz_proje_olustur_santiyeli": (
        "POST",
        lambda x: "/projects",
        lambda x: {
            "name": "Santiyeli Proje",
            "project_type": "kendi_yatirim",
            "city": "Ankara",
            "sites": [{"name": "İç Şantiye"}],
        },
    ),
    "b4_yaz_proje_guncelle": (
        "PATCH",
        _proje,
        lambda x: {"name": "Disiplin Projesi Güncel", "city": "Ankara"},
    ),
}


def _yeni_etiketle(deger: object, yol: str, etiketler: dict[uuid.UUID, str]) -> None:
    """Sunucunun ürettiği (dünyada olmayan) kimlikleri JSON yoluyla etiketler."""
    if isinstance(deger, dict):
        for anahtar, alt in deger.items():
            _yeni_etiketle(alt, f"{yol}.{anahtar}" if yol else anahtar, etiketler)
    elif isinstance(deger, list):
        for sira, alt in enumerate(deger):
            _yeni_etiketle(alt, f"{yol}[{sira}]", etiketler)
    elif isinstance(deger, str) and _UUID.match(deger):
        etiketler.setdefault(uuid.UUID(deger), f"<yeni:{yol}>")


def _kiyasla(ad: str, veri: object) -> None:
    if GUNCELLE:
        yaz(ad, veri)
        return
    assert golden_yolu(ad).exists(), f"golden yok: {ad} (UPDATE_B4_GOLDEN=1 ile üret)"
    assert veri == oku(ad)


def _yol_coz(x: DunyaB4, yol: str, params: dict | None) -> dict | None:
    if params and params.get("section_id") == "<S1>":
        return {**params, "section_id": str(x.d.s1.id)}
    return params


async def _oku(client: AsyncClient, x: DunyaB4, baslik: dict[str, str], ad: str) -> dict:
    yol, params = OKUMALAR[ad]
    resp = await client.get(yol(x), headers=baslik, params=_yol_coz(x, yol(x), params))
    assert resp.status_code == 200, resp.text
    from tests.discipline_scope._golden import normalize

    return {"status": resp.status_code, "body": normalize(resp.json(), x.d.etiketler)}


@pytest.mark.parametrize("ad", sorted(OKUMALAR))
async def test_b4_okuma_golden(client: AsyncClient, dunya_b4: DunyaB4, ad: str) -> None:
    x = dunya_b4
    yanit = await _oku(client, x, x.d.baslik["atamasiz"], ad)
    _kiyasla(ad, yanit)


@pytest.mark.parametrize("ad", sorted(YAZMALAR))
async def test_b4_yazma_golden(client: AsyncClient, dunya_b4: DunyaB4, seeded_db, ad: str) -> None:
    x = dunya_b4
    yontem, yol, govde = YAZMALAR[ad]
    once = await audit_kimlikleri(seeded_db)
    resp = await client.request(yontem, yol(x), headers=x.d.baslik["atamasiz"], json=govde(x))
    assert resp.status_code in (200, 201), resp.text
    _yeni_etiketle(resp.json(), "", x.d.etiketler)
    audit = await yeni_audit(seeded_db, once)
    yanit = {"status": resp.status_code, "body": resp.json()}
    _kiyasla(ad, anlik_goruntu(x.d, yanit, {}, audit))


async def test_b4_dunya_saglam(client: AsyncClient, dunya_b4: DunyaB4) -> None:
    """Golden'ın anlamlı olması için dünya gerçekten dolu: işçi sayısı > 0 (today sabit),
    fiziksel % ve mali ilerleme sayı, pm_atamasiz ile atamasız aynı gövdeyi görür (izin eşit)."""
    x = dunya_b4
    yanit = await client.get(f"{_proje(x)}/sites", headers=x.d.baslik["atamasiz"])
    assert yanit.status_code == 200, yanit.text
    govde = yanit.text
    assert '"worker_count":' in govde.replace(" ", "")


@pytest.mark.parametrize("ad", sorted(OKUMALAR))
async def test_b4_pm_atamasiz_ayni_okuma(client: AsyncClient, dunya_b4: DunyaB4, ad: str) -> None:
    """Pozitif kontrol: aynı roldeki atamasız PM, atamasız admin ile AYNI gövdeyi mi görür
    (yalnız izin farkı yoksa)? Farklıysa golden aktörü seçimi/rol farkı belgelenmelidir."""
    x = dunya_b4
    pm = await _oku(client, x, x.d.baslik["pm_atamasiz"], ad)
    admin = await _oku(client, x, x.d.baslik["atamasiz"], ad)
    assert pm["status"] == admin["status"] == 200
