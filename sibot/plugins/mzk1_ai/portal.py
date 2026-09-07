"""Authenticated client for the CPA Portal Admin API."""

from collections.abc import Mapping
from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from .models import (
    QuotaCredential,
    QuotaCredentialsResponse,
    QuotaSnapshot,
    RankingPeriod,
    RankingResponse,
    ResetCreditsResponse,
)

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


class PortalError(RuntimeError):
    """Base Portal client error."""


class PortalUnauthorizedError(PortalError):
    """Portal rejected the configured Bearer token."""

    def __init__(self) -> None:
        super().__init__("CPA Portal rejected the Admin API token")


class PortalBadRequestError(PortalError):
    """The client sent an invalid Admin API request."""

    def __init__(self) -> None:
        super().__init__("CPA Portal rejected the request parameters")


class PortalUpstreamError(PortalError):
    """Portal could not obtain data from Keeper or CPA."""

    def __init__(self) -> None:
        super().__init__("CPA Portal could not load upstream data")


class PortalUnavailableError(PortalError):
    """Portal was unreachable or returned an unexpected HTTP status."""

    def __init__(self, status_code: int | None = None) -> None:
        message = "CPA Portal request failed"
        if status_code is not None:
            message = f"CPA Portal returned HTTP {status_code}"
        super().__init__(message)


class PortalProtocolError(PortalError):
    """Portal returned a response incompatible with the supported schema."""

    def __init__(self) -> None:
        super().__init__("CPA Portal returned an incompatible response")


class PortalClient:
    """Small reusable async client with one connection pool."""

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = f"{base_url.rstrip('/')}/"
        self._token = token
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._token.get_secret_value()}",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(10.0, connect=3.0, write=5.0, pool=3.0),
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ranking(self, period: RankingPeriod) -> RankingResponse:
        return await self._get_model(
            "ranking",
            RankingResponse,
            params={"period": period, "metric": "total_tokens"},
        )

    async def quota(self) -> QuotaSnapshot:
        return await self._get_model("quota", QuotaSnapshot)

    async def quota_credentials(self) -> list[QuotaCredential]:
        response = await self._get_model("quota", QuotaCredentialsResponse)
        return response.credentials

    async def reset_credits(self, auth_index: str) -> ResetCreditsResponse:
        if not auth_index.strip() or auth_index in {".", ".."}:
            raise PortalBadRequestError
        response = await self._get_model(
            f"quota/reset-credits/{quote(auth_index, safe='')}",
            ResetCreditsResponse,
        )
        if response.auth_index != auth_index:
            raise PortalProtocolError
        return response

    async def _get_model(
        self,
        path: str,
        model: type[_ResponseModel],
        *,
        params: Mapping[str, str] | None = None,
    ) -> _ResponseModel:
        try:
            response = await self._get_client().get(path, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise PortalUnavailableError from error

        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise PortalUnauthorizedError
        if response.status_code == httpx.codes.BAD_REQUEST:
            raise PortalBadRequestError
        if response.status_code == httpx.codes.BAD_GATEWAY:
            raise PortalUpstreamError
        if not response.is_success:
            raise PortalUnavailableError(response.status_code)

        try:
            return model.model_validate_json(response.content)
        except (ValidationError, ValueError) as error:
            raise PortalProtocolError from error
