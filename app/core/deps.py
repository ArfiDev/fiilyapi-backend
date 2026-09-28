from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import joinedload

from app.core.db import DbSession
from app.core.security import TokenError, decode_token
from app.modules.users.models import User, UserStatus

_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Oturum geçersiz veya süresi dolmuş",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: DbSession,
) -> User:
    if credentials is None:
        raise _UNAUTHORIZED

    try:
        decoded = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise _UNAUTHORIZED from exc

    user = await session.get(User, decoded.user_id, options=[joinedload(User.role)])
    if (
        user is None
        or user.status is not UserStatus.active
        or user.token_version != decoded.token_version
    ):
        raise _UNAUTHORIZED

    return user
