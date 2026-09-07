"""On-demand queries for Codex reset credits."""

import asyncio

from nonebot import logger

from .models import CodexAccountResetCredits, QuotaCredential
from .portal import PortalClient, PortalError

_RESET_QUERY_CONCURRENCY = 4


async def load_reset_credits(portal: PortalClient) -> list[CodexAccountResetCredits]:
    credentials = await portal.quota_credentials()
    semaphore = asyncio.Semaphore(_RESET_QUERY_CONCURRENCY)
    return list(
        await asyncio.gather(
            *(
                _load_account(portal, credential, semaphore)
                for credential in credentials
                if credential.provider.lower() == "codex" and not credential.disabled
            )
        )
    )


async def _load_account(
    portal: PortalClient,
    credential: QuotaCredential,
    semaphore: asyncio.Semaphore,
) -> CodexAccountResetCredits:
    async with semaphore:
        try:
            response = await portal.reset_credits(credential.credential_id)
        except PortalError as error:
            logger.warning(
                "Mzk1 AI reset credits query failed: {}", type(error).__name__
            )
            response = None
    return CodexAccountResetCredits(
        display_name=credential.display_name,
        response=response,
    )
