"""Satinalma servisinin ORTAK cekirdegi — izin anahtari, kapsam ve metin kirpma.

Uc parcanin da BURADA durmasinin sebebi ayni: her biri **birden fazla** varlik
ailesinde (tedarikci · talep · teklif · siparis) kullaniliyor ve ikinci bir
kopya zamanla sapardi.

* `PERMISSION_MODULE` — router'in ve testlerin okudugu geriye donuk takma ad;
  TEK KOPYA `guards`tadir.
* `_visible_project_ids` — IKI KATMANLI korumanin KAPSAM katmani (`inventory/
  service.py` deseni): izni router verir, gorunur proje kumesini bu belirler.
* `_strip` — bosluklu degeri `None`a cevirir; PATCH govdelerinde "bosluk gonder,
  alani sil" davranisinin tek kaynagi.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.procurement import guards
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.users.models import User

PERMISSION_MODULE = guards.PERMISSION_MODULE
"""Izin anahtari — TEK KOPYA `guards.PERMISSION_MODULE`dedir (T3'te oraya
tasindi: `repository.actor_level` de ona ihtiyac duyar ve `repository → service`
ithalati donguye girerdi). Bu ad geriye donuk takma addir; router ve testler
`service.PERMISSION_MODULE` yazmaya devam eder."""


async def _visible_project_ids(
    session: AsyncSession, actor: User, *, liste: bool = False
) -> list[uuid.UUID]:
    """`liste=True`: şirket türü liste/özet ucu (IZN-B5f 23b) — satırlar kişinin O PROJEDEKİ
    rolünün `satınalma` sayfa izniyle süzülür (detay ucuyla aynı kural)."""
    ciftler = sirket_liste_ciftleri(PERMISSION_MODULE) if liste else ()
    return [p.id for p in await visible_projects(session, actor, sirket_ciftleri=ciftler)]


def _strip(deger: str | None) -> str | None:
    return None if deger is None else (deger.strip() or None)
