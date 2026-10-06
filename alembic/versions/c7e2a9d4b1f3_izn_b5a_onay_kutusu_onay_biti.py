"""IZN-B5a madde 13 — `genel.onay_kutusu` sayfasının işlevsiz Onaylar biti kalkar

Karar kaynağı: CEO (IZN-B5a). Onay Kutusu'nun Onaylar biti hiçbir kapıyı açmıyordu (`approvals`
modülünde onay eşikli uç yok); katalogda sayfanın onay sütunu kapandı (`onay_var=False`). Kayıtlı
`can_approve=true` hücreleri kalırsa Sayfa İzinleri ekranı rolü kaydederken 422 alır ("bu sayfada
onay eylemi yok") ve `GET /roles/{id}/pages` katalogla çelişen bir bayrak döner.

## Kural (upgrade)
`role_page_permissions` içinde `page_key = 'genel.onay_kutusu'` ve `can_approve = true` olan her
hücrede `can_approve = false`. Düzey (`level`) DEĞİŞMEZ. Etkilenen satır sayısı INFO basılır.
Yetki değişmez: bit hiçbir kapıyı açmıyordu (genişleme de daralma da yok).

## Downgrade
Bilinçli olarak İŞLEMSİZ: hangi hücrenin `true` olduğu bilgisi upgrade'de kaybolur ve bit hiçbir
kapıyı açmadığı için geri yazmak davranış değiştirmez. Eski katalog (`onay_var=True`) `false`
hücreyle de tutarlıdır.

Migration `app` IMPORT ETMEZ (sayfa anahtarı elle kopya).

Revision ID: c7e2a9d4b1f3
Revises: a1d6e4b8c2f7
Create Date: 2026-10-06

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e2a9d4b1f3"
down_revision: str | Sequence[str] | None = "a1d6e4b8c2f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: ELLE KOPYA: `app.core.sayfalar` katalog anahtarı.
ONAY_KUTUSU = "genel.onay_kutusu"


def upgrade() -> None:
    result = op.get_bind().execute(
        sa.text(
            "UPDATE role_page_permissions SET can_approve = false "
            "WHERE page_key = :page AND can_approve"
        ),
        {"page": ONAY_KUTUSU},
    )
    logger.info("IZN-B5a: %s onay kutusu Onaylar hücresi kapatıldı", result.rowcount)


def downgrade() -> None:
    """İşlemsiz (bkz. modül belgesi)."""
