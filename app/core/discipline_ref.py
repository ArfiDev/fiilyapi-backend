"""Disiplin ozeti (rozet + renk) — ORTAK sema (DSC-B0b).

Cekirdek moduller planlamayi (EV) IMPORT ETMEZ (§2.7); `auth` (`/auth/me`) ve EV (`users/{id}/
disciplines`, katalog satiri) AYNI nesneyi buradan alir. Disiplin portu (`discipline_scope`)
`user_disciplines_detail` ile bu tipi doner; sema tek yerde tanimli oldugu icin OpenAPI'de tek
`DisciplineRef` bileseni vardir.
"""

import uuid

from pydantic import BaseModel, ConfigDict


class DisciplineRef(BaseModel):
    """Katalog satirina gomulu disiplin ozeti (rozet + renk)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    color: str
