"""ÖLÇÜM (kayıt 2 + 3, TEARDOWN-B1 sonrası): `get_db` teardown commit'i
patladığında istemci NE görür.

Bu dosya ÜRETİM `DbSession` takma adının (`app/core/db.py`,
`Depends(get_db, scope="function")`) kapsamını gerçek HTTP davranışıyla ölçer.
Rotalar takma adı kullanır, `get_db` `dependency_overrides` ile commit'te
patlayan bir taklitle değiştirilir (bkz. `_teardown_commit_asgi_app.py`).

## Tarihçe

Eskiden `get_db` çıplak `Depends(get_db)` ile kullanılırdı; FastAPI generator
bağımlılığı hesaplanmış `scope="request"` ile ele alır (`fastapi/dependencies/
models.py` `_get_computed_scope`), teardown `await response(...)`ten SONRA
koşardı: istemci `200 {"ok": true}` görür, sunucu `Exception in ASGI
application` loglardı ve hiçbir exception handler çağrılmazdı. Bu dosya o
ayrışmayı ölçüyordu. Takma ad `scope="function"` olunca ölçüm TERSİNE döndü.

## Şimdi ölçülenler

1. Gerçek soket (ayrı süreç uvicorn) + teardown `RuntimeError` → istemci
   **500** görür, gövde `{"ok": true}` DEĞİLDİR.
2. Gerçek soket + teardown `IntegrityError` → **409**: hata, kayıtlı
   `_integrity_error_handler`a ULAŞIR (function kapsamı, yanıt yazılmadan önce).
3. İç süreç (`httpx.ASGITransport`, `raise_app_exceptions=False`): 500 / 409.
4. NEGATİF KONTROL: açıkça `Depends(get_db, scope="request")` kullanan rota
   HÂLÂ 200 döner (yazılan yanıt zaten gitmiştir). Request kapsamının neden
   YALNIZ yazmasız akış ucuna (`documents/deps.py`) izinli olduğunu belgeler;
   yapısal bekçi: `test_getdb_kapsam_bekcisi.py`.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest

from tests.core._teardown_commit_asgi_app import (
    TEARDOWN_MESAJI,
    app_integrity,
    app_runtime,
)


def _bos_port_bul() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gercek_soketten_istek(uygulama_adi: str, yol: str) -> tuple[httpx.Response, str]:
    """Ayrı süreçte uvicorn başlatır, `POST yol` yapar, (yanıt, sunucu stderr) döner."""
    port = _bos_port_bul()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            f"tests.core._teardown_commit_asgi_app:{uygulama_adi}",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                httpx.get(f"{base_url}/openapi.json", timeout=0.2)
                break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            pytest.fail("uvicorn alt süreci zamanında ayağa kalkmadı")

        response = httpx.post(f"{base_url}{yol}", timeout=5)
    finally:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate(timeout=5)
    return response, stderr


async def _ic_surec_istek(uygulama, yol: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=uygulama, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(yol)


def test_GERCEK_soket_teardown_RuntimeError_istemciye_500_doner() -> None:
    """🔴 ASIL ÖLÇÜM. Function kapsamında teardown hatası yanıttan ÖNCE koşar:
    istemci 500 görür, `{"ok": true}` GÖRMEZ (eski davranış: 200)."""
    response, stderr = _gercek_soketten_istek("app_runtime", "/kaydet")

    assert response.status_code == 500, (
        "Teardown commit hatası istemciye 500 olarak ulaşmalıydı; 200 görülüyorsa "
        "`DbSession` takma adı yeniden request kapsamına düşmüş demektir "
        f"(başarısız yazma sessizce başarı görünür): {response.status_code} {response.text}"
    )
    assert response.text != '{"ok":true}', response.text
    assert TEARDOWN_MESAJI in stderr, stderr


def test_GERCEK_soket_teardown_IntegrityError_409_doner() -> None:
    """Function kapsamında teardown `IntegrityError`ı KAYITLI
    `_integrity_error_handler`a ulaşır → 409 (request kapsamında ulaşamazdı)."""
    response, _ = _gercek_soketten_istek("app_integrity", "/kaydet")

    assert response.status_code == 409, (
        "IntegrityError kayıtlı handler'a ulaşıp 409 dönmeliydi: "
        f"{response.status_code} {response.text}"
    )
    assert response.json() == {"detail": "Veri bütünlüğü hatası"}, response.text


async def test_INPROCESS_teardown_RuntimeError_500_doner() -> None:
    """İç süreç: hata sunucu sınırında 500'e çevrilir (`raise_app_exceptions=False`)."""
    response = await _ic_surec_istek(app_runtime, "/kaydet")

    assert response.status_code == 500, f"{response.status_code} {response.text}"


async def test_INPROCESS_teardown_IntegrityError_409_doner() -> None:
    """İç süreç: `IntegrityError` kayıtlı handler'a ulaşır → 409."""
    response = await _ic_surec_istek(app_integrity, "/kaydet")

    assert response.status_code == 409, f"{response.status_code} {response.text}"
    assert response.json() == {"detail": "Veri bütünlüğü hatası"}, response.text


def test_NEGATIF_KONTROL_acik_request_kapsami_hala_200_doner() -> None:
    """Açıkça `Depends(get_db, scope="request")` kullanan rota teardown
    `RuntimeError`ında bile istemciye 200 + gövde döner: yanıt soketa yazıldıktan
    SONRA patlar. Bu yüzden request kapsamı YALNIZ yazmasız akış ucuna izinlidir."""
    response, stderr = _gercek_soketten_istek("app_runtime", "/kaydet-request")

    assert response.status_code == 200, f"{response.status_code} {response.text}"
    assert response.json() == {"ok": True}, response.text
    assert "Exception in ASGI application" in stderr, stderr
    assert TEARDOWN_MESAJI in stderr, stderr


async def test_NEGATIF_KONTROL_INPROCESS_request_kapsami_hatayi_ceteveir() -> None:
    """İç süreç aynası: request kapsamında yanıt ZATEN başlamıştır; Starlette
    handler'ı çağıramaz ("response already started") ve hata çağırana fırlar —
    409 ÜRETİLEMEZ."""
    transport = httpx.ASGITransport(app=app_integrity)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="response already started"):
            await client.post("/kaydet-request")
