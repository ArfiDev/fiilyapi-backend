from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.discipline_scope import user_disciplines_detail
from app.core.ratelimit import client_ip, limiter
from app.core.security import TokenError, create_access_token, create_refresh_token, decode_token
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.auth.schemas import LoginRequest, MeResponse, RefreshRequest, TokenPair
from app.modules.auth.service import AuthError, authenticate
from app.modules.roles.repository import get_role_matrix
from app.modules.users.models import User, UserStatus

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
@limiter.limit(settings.login_rate_limit)
async def login(
    request: Request,
    payload: LoginRequest,
    session: DbSession,
) -> TokenPair:
    try:
        user = await authenticate(session, payload.email, payload.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Kimlik bilgileri hatalı"
        ) from exc

    # Yalnizca basarili giris denetime yazilir; basarisiz denemeler kapsam disidir
    # (plan §Kapsam disi) — hiz siniri zaten kotuye kullanimi frenliyor.
    await record_audit(
        session,
        action=AuditAction.login,
        detail=messages.LOGIN_DETAIL,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )

    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=create_refresh_token(user.id, user.token_version),
    )


@router.post("/refresh", response_model=TokenPair)
@limiter.limit(settings.refresh_rate_limit)
async def refresh(
    request: Request,
    payload: RefreshRequest,
    session: DbSession,
) -> TokenPair:
    try:
        decoded = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Oturum süresi dolmuş"
        ) from exc

    # Kullanıcıyı yeniden yükleyip durumunu kontrol etmeden yeni token basmak,
    # pasife alınmış bir kullanıcının eski refresh token'ıyla 30 gün boyunca
    # yeni access token üretmeye devam etmesine izin verir. get_current_user ile
    # aynı kuralı burada da uyguluyoruz. Kullanıcı yok ile pasif arasında fark
    # göstermemek için ikisinde de aynı yanıtı dönüyoruz.
    user = await session.get(User, decoded.user_id)
    if (
        user is None
        or user.status is not UserStatus.active
        or user.token_version != decoded.token_version
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Oturum süresi dolmuş")

    # 🔴 REFRESH TOKEN BURADA YENILENMEZ (auth-refresh-rotasyon, kayit 25).
    # Onceden bu satir `create_refresh_token(...)` cagirip O ANDAN itibaren 30 gunluk
    # YENI bir refresh token basiyordu; eskisi de gecersizlesmedigi icin (rotasyon,
    # jti, kara liste yok) oturumun MUTLAK omru her cagrida sifirlaniyordu. Sonuc:
    # calinmis bir refresh token 30 gun degil SINIRSIZ yasiyordu — saldirgan her
    # 29 gunde bir /auth/refresh cagirdikca saat basa donuyordu.
    # Sunulan token'i aynen geri vererek 30 gunluk tavani GERCEKTEN tavan yapiyoruz.
    # Gercek rotasyon (jti + reuse tespiti) migration ister, ayri dilimde yapilacak;
    # BFF zaten donen refresh token'i saklamiyor (frontend/src/lib/auth/backend.ts:145),
    # bu yuzden mesru kullanicinin davranisi degismez.
    return TokenPair(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=payload.refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """token_version'ı artırır — o ana dek basılmış tüm token'lar (access + refresh) geçersiz
    olur (gerçek sunucu-taraflı çıkış). BFF ayrıca httpOnly cookie'yi siler."""
    user.token_version += 1
    await session.flush()
    return None


@router.get("/me", response_model=MeResponse)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> MeResponse:
    """Matris mantigi YENIDEN YAZILMAZ: `roles.repository.get_role_matrix`
    aynen kullanilir — `/roles/{id}/permissions` ucuyla ayni kaynak, tek fark
    rol kimliginin aktörün kendi rolü olmasi ve ek yetki aranmamasi."""
    matrix = await get_role_matrix(session, user.role_id)
    disciplines = await user_disciplines_detail(session, user.id)
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        title=user.title,
        role_key=user.role.key,
        status=user.status,
        permissions={module.key: perm.access_level for module, perm in matrix},
        disciplines=disciplines,
    )
