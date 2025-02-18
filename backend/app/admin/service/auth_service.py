#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from fastapi import Request, Response
from fastapi.security import HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask, BackgroundTasks

from backend.app.admin.conf import admin_settings
from backend.app.admin.crud.crud_user import user_dao
from backend.app.admin.model import User
from backend.app.admin.schema.token import GetLoginToken, GetNewToken
from backend.app.admin.schema.user import AuthLoginParam
from backend.app.admin.service.login_log_service import login_log_service
from backend.common.enums import LoginLogStatusType
from backend.common.exception import errors
from backend.common.response.response_code import CustomErrorCode
from backend.common.security.jwt import (
    create_access_token,
    create_new_token,
    create_refresh_token,
    get_token,
    jwt_decode,
    password_verify,
)
from backend.core.conf import settings
from backend.database.db_mysql import async_db_session
from backend.database.db_redis import redis_client
from backend.utils.timezone import timezone


class AuthService:
    @staticmethod
    async def user_verify(db: AsyncSession, username: str, password: str) -> User:
        user = await user_dao.get_by_username(db, username)
        if not user:
            raise errors.NotFoundError(msg='用户名或密码有误')
        elif not password_verify(password, user.password):
            raise errors.AuthorizationError(msg='用户名或密码有误')
        elif not user.status:
            raise errors.AuthorizationError(msg='用户已被锁定, 请联系统管理员')
        return user

    async def swagger_login(self, *, obj: HTTPBasicCredentials) -> tuple[str, User]:
        async with async_db_session.begin() as db:
            user = await self.user_verify(db, obj.username, obj.password)
            user_id = user.id
            a_token = await create_access_token(str(user_id), user.is_multi_login)
            await user_dao.update_login_time(db, obj.username)
            return a_token.access_token, user

    async def login(
        self, *, request: Request, response: Response, obj: AuthLoginParam, background_tasks: BackgroundTasks
    ) -> GetLoginToken:
        async with async_db_session.begin() as db:
            try:
                user = await self.user_verify(db, obj.username, obj.password)
                user_id = user.id
                user_uuid = user.uuid
                username = user.username
                captcha_code = await redis_client.get(f'{admin_settings.CAPTCHA_LOGIN_REDIS_PREFIX}:{request.state.ip}')
                if not captcha_code:
                    raise errors.AuthorizationError(msg='验证码失效，请重新获取')
                if captcha_code.lower() != obj.captcha.lower():
                    raise errors.CustomError(error=CustomErrorCode.CAPTCHA_ERROR)
                a_token = await create_access_token(str(user_id), user.is_multi_login)
                r_token = await create_refresh_token(str(user_id), user.is_multi_login)
            except errors.NotFoundError as e:
                raise errors.NotFoundError(msg=e.msg)
            except (errors.AuthorizationError, errors.CustomError) as e:
                task = BackgroundTask(
                    login_log_service.create,
                    **dict(
                        db=db,
                        request=request,
                        user_uuid=user_uuid,
                        username=username,
                        login_time=timezone.now(),
                        status=LoginLogStatusType.fail.value,
                        msg=e.msg,
                    ),
                )
                raise errors.AuthorizationError(msg=e.msg, background=task)
            except Exception as e:
                raise e
            else:
                background_tasks.add_task(
                    login_log_service.create,
                    **dict(
                        db=db,
                        request=request,
                        user_uuid=user_uuid,
                        username=username,
                        login_time=timezone.now(),
                        status=LoginLogStatusType.success.value,
                        msg='登录成功',
                    ),
                )
                await redis_client.delete(f'{admin_settings.CAPTCHA_LOGIN_REDIS_PREFIX}:{request.state.ip}')
                await user_dao.update_login_time(db, obj.username)
                response.set_cookie(
                    key=settings.COOKIE_REFRESH_TOKEN_KEY,
                    value=r_token.refresh_token,
                    max_age=settings.COOKIE_REFRESH_TOKEN_EXPIRE_SECONDS,
                    expires=timezone.f_utc(r_token.refresh_token_expire_time),
                    httponly=True,
                )
                await db.refresh(user)
                data = GetLoginToken(
                    access_token=a_token.access_token,
                    access_token_expire_time=a_token.access_token_expire_time,
                    user=user,  # type: ignore
                )
                return data

    @staticmethod
    async def new_token(*, request: Request, response: Response) -> GetNewToken:
        refresh_token = request.cookies.get(settings.COOKIE_REFRESH_TOKEN_KEY)
        if not refresh_token:
            raise errors.TokenError(msg='Refresh Token 丢失，请重新登录')
        try:
            user_id = jwt_decode(refresh_token)
        except Exception:
            raise errors.TokenError(msg='Refresh Token 无效')
        if request.user.id != user_id:
            raise errors.TokenError(msg='Refresh Token 无效')
        async with async_db_session() as db:
            user = await user_dao.get(db, user_id)
            if not user:
                raise errors.NotFoundError(msg='用户名或密码有误')
            elif not user.status:
                raise errors.AuthorizationError(msg='用户已被锁定, 请联系统管理员')
            current_token = get_token(request)
            new_token = await create_new_token(
                sub=str(user.id),
                token=current_token,
                refresh_token=refresh_token,
                multi_login=user.is_multi_login,
            )
            response.set_cookie(
                key=settings.COOKIE_REFRESH_TOKEN_KEY,
                value=new_token.new_refresh_token,
                max_age=settings.COOKIE_REFRESH_TOKEN_EXPIRE_SECONDS,
                expires=timezone.f_utc(new_token.new_refresh_token_expire_time),
                httponly=True,
            )
            data = GetNewToken(
                access_token=new_token.new_access_token,
                access_token_expire_time=new_token.new_access_token_expire_time,
            )
            return data

    @staticmethod
    async def logout(*, request: Request, response: Response) -> None:
        token = get_token(request)
        refresh_token = request.cookies.get(settings.COOKIE_REFRESH_TOKEN_KEY)
        response.delete_cookie(settings.COOKIE_REFRESH_TOKEN_KEY)
        if request.user.is_multi_login:
            key = f'{settings.TOKEN_REDIS_PREFIX}:{request.user.id}:{token}'
            await redis_client.delete(key)
            if refresh_token:
                key = f'{settings.TOKEN_REFRESH_REDIS_PREFIX}:{request.user.id}:{refresh_token}'
                await redis_client.delete(key)
        else:
            key_prefix = f'{settings.TOKEN_REDIS_PREFIX}:{request.user.id}:'
            await redis_client.delete_prefix(key_prefix)
            key_prefix = f'{settings.TOKEN_REFRESH_REDIS_PREFIX}:{request.user.id}:'
            await redis_client.delete_prefix(key_prefix)


auth_service: AuthService = AuthService()
