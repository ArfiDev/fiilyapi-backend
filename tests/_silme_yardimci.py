"""SIL-B1 test yardımcıları: Sistem Yöneticisi girişi + önizleme-sonra-sil akışı.

Silme motoru önizlemesiz çalışmaz (428). Eski testler `client.delete(...)` çağırırdı; artık
`sil_aile` / `sil_genel` önizlemeyi alır, belirteci DELETE'e taşır.
"""

from httpx import AsyncClient, Response

#: Aile uçları (`DELETE /sites/{id}` …) — genel uçla AYNI motoru ve AYNI belirteci kullanır.
AILE_YOLLARI = {
    "site": "/sites",
    "section": "/sections",
    "block": "/blocks",
    "unit": "/units",
    "progress_payment": "/progress-payments",
    "subcontractor_progress_payment": "/subcontractor-progress-payments",
    "invoice": "/invoices",
    "payment": "/payments",
    "journal_entry": "/journal-entries",
    "financial_instrument": "/financial-instruments",
}


async def sisyon_girisi(
    client: AsyncClient, user_factory, e_posta: str = "sisyon@silme.co"
) -> dict:
    """`system_admin` rolünde kullanıcı açar, giriş yapar, Authorization başlığını döner."""
    await user_factory(email=e_posta, password="parola1234", role_key="system_admin")
    yanit = await client.post("/auth/login", json={"email": e_posta, "password": "parola1234"})
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def rol_girisi(client: AsyncClient, user_factory, role_key: str) -> dict:
    """Herhangi bir rolde kullanıcı açar ve giriş başlığını döner."""
    e_posta = f"{role_key}@silme-rol.co"
    await user_factory(email=e_posta, password="parola1234", role_key=role_key)
    yanit = await client.post("/auth/login", json={"email": e_posta, "password": "parola1234"})
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def onizle(client: AsyncClient, headers: dict, kind: str, record_id) -> Response:
    return await client.get(f"/admin/silme/{kind}/{record_id}/onizleme", headers=headers)


async def _onizlemeli_sil(client: AsyncClient, headers: dict, kind: str, record_id, yol: str):
    yanit = await onizle(client, headers, kind, record_id)
    if yanit.status_code != 200:
        return yanit
    return await client.delete(
        yol, params={"preview_token": yanit.json()["preview_token"]}, headers=headers
    )


async def sil_genel(client: AsyncClient, headers: dict, kind: str, record_id) -> Response:
    """Önizleme al, sonra `DELETE /admin/silme/{kind}/{id}?preview_token=…`."""
    return await _onizlemeli_sil(
        client, headers, kind, record_id, f"/admin/silme/{kind}/{record_id}"
    )


async def sil_aile(client: AsyncClient, headers: dict, kind: str, record_id) -> Response:
    """Önizleme al, sonra aile ucu (`DELETE /sites/{id}?preview_token=…`)."""
    return await _onizlemeli_sil(
        client, headers, kind, record_id, f"{AILE_YOLLARI[kind]}/{record_id}"
    )
