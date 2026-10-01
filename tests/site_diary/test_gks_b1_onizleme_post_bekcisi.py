"""GKS-B1 BEKÇİ — önizleme anahtarları == POST'un yazdığı satır anahtarları.

İki küme AYNI saf fonksiyondan (`skeleton.skeleton_keys`) beslenir; bu dosya ayrışmayı
yakalar. Karışık kurulum (tahsissiz · kısmi · tam çok bölümlü · tam tek bölümlü) ve iki bölüm
seçeneği (yok · S1 · S2) ile; anahtar = (kalem, bölüm) + planlı, SIRA DAHİL. DB'deki satırlar da
(ham `site_diary_lines`) kıyaslanır: yanıt süzmesi bekçiyi maskelemesin.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine
from tests.site_diary._gks_b1 import GUN, anahtarlar, olustur, onizleme
from tests.site_diary.conftest import KarisikSantiye

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("bolum_adi", [None, "s1", "s2"])
async def test_onizleme_anahtarlari_POST_iskeletiyle_birebir(
    client: AsyncClient,
    admin_headers,
    karisik_santiye: KarisikSantiye,
    seeded_db: AsyncSession,
    bolum_adi,
) -> None:
    ks = karisik_santiye
    bolum = None if bolum_adi is None else getattr(ks, bolum_adi)

    onizleme_yaniti = await onizleme(client, admin_headers, ks.site.id, GUN, bolum)
    kayit = await olustur(client, admin_headers, ks.site.id, GUN, bolum)

    assert onizleme_yaniti.status_code == 200 and kayit.status_code == 201, kayit.text
    onizleme_anahtarlari = anahtarlar(onizleme_yaniti.json()["lines"])
    assert onizleme_anahtarlari  # boş kümeyle sahte yeşil olmasın
    assert onizleme_anahtarlari == anahtarlar(kayit.json()["lines"])

    # Ham tablo: (kalem, bölüm) kümesi (sıra ilişki order_by'ına bağlı → küme olarak).
    satirlar = (
        (
            await seeded_db.execute(
                select(SiteDiaryLine)
                .join(SiteDiaryEntry, SiteDiaryEntry.id == SiteDiaryLine.entry_id)
                .where(SiteDiaryEntry.site_id == ks.site.id)
            )
        )
        .scalars()
        .all()
    )
    yazilan = {(s.boq_item_id, s.section_id) for s in satirlar}
    beklenen = {
        (ks.items[kod].id, None if bolum_ad is None else (ks.s1 if bolum_ad == "S1" else ks.s2).id)
        for kod, bolum_ad, _ in (
            (s["code"], s["section_name"], 0) for s in onizleme_yaniti.json()["lines"]
        )
    }
    assert yazilan == beklenen
    assert all(s.quantity == Decimal("0") for s in satirlar)
