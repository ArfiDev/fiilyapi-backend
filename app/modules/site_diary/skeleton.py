"""GKS-B1 — günlük satır İSKELETİNİN saf kuralı (DB BİLMEZ).

İki küme bu fonksiyondan beslenir ve ayrışamaz: `service._build_lines` (POST iskeleti)
ve `GET /sites/{id}/diary/skeleton` (kaydetmeden önizleme). Bekçi:
`tests/site_diary/test_gks_b1_onizleme_post_bekcisi.py` (önizleme anahtarları == POST).

## Kural (A) — CEO kararı 2026-10-01 (G-b + G4)

* BÖLÜM SEÇİLİ: yalnız o bölüme tahsisli kalemler; satır `section_id` = bölüm,
  planlı = pay. Bölümsüz satır YOK.
* BÖLÜM SEÇİLİ DEĞİL:
  * tahsissiz ya da KISMEN tahsisli kalem → tek Bölümsüz satır, planlı = kota − Σpay;
  * TAMAMEN tahsisli kalem (Σpay ≥ kota) → Bölümsüz YOK (G4); tahsisli olduğu HER
    bölüm için bir satır, planlı = pay;
  * kısmen tahsisli kalemin bölüm satırları AÇILMAZ.
  * Her kalem en az bir satırla görünür kalır.
* Sıra: girdi kalem sırası (BOQ sırası); bir kalemin bölüm satırları bölüm sırasıyla.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal("0.000")


@dataclass(frozen=True, slots=True)
class SkeletonItem:
    """Kalem: kimlik + kota (BOQ miktarı). Sıra, dizideki konumdur."""

    id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class SkeletonLine:
    """İskelet satırının kimliği (kalem, bölüm | Bölümsüz) + planlı miktarı."""

    item_id: uuid.UUID
    section_id: uuid.UUID | None
    planned: Decimal

    @property
    def key(self) -> tuple[uuid.UUID, uuid.UUID | None]:
        return (self.item_id, self.section_id)


#: kalem → {bölüm → pay}
Allocations = Mapping[uuid.UUID, Mapping[uuid.UUID, Decimal]]


def group_allocations(
    rows: Mapping[tuple[uuid.UUID, uuid.UUID], Decimal],
) -> dict[uuid.UUID, dict[uuid.UUID, Decimal]]:
    """`repository.allocations_for_items` çıktısı ((kalem, bölüm) → pay) → kalem bazlı."""
    grouped: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    for (item_id, section_id), quantity in rows.items():
        grouped.setdefault(item_id, {})[section_id] = quantity
    return grouped


def skeleton_keys(
    items: Sequence[SkeletonItem],
    allocations: Allocations,
    section_id: uuid.UUID | None,
    section_order: Mapping[uuid.UUID, int] | None = None,
) -> list[SkeletonLine]:
    """Kural (A). `section_order`: bölüm → sıra (`sort_order`); eşitlikte/eksikte `id`."""
    order = section_order or {}
    lines: list[SkeletonLine] = []
    for item in items:
        shares = allocations.get(item.id, {})
        if section_id is not None:
            if section_id in shares:
                lines.append(SkeletonLine(item.id, section_id, shares[section_id]))
            continue
        allocated = sum(shares.values(), _ZERO)
        fully_allocated = bool(shares) and allocated >= item.quantity
        if not fully_allocated:
            lines.append(SkeletonLine(item.id, None, max(item.quantity - allocated, _ZERO)))
            continue
        for sid in sorted(shares, key=lambda s: (order.get(s, 0), str(s))):
            lines.append(SkeletonLine(item.id, sid, shares[sid]))
    return lines
