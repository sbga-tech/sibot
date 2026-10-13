"""On-demand queries for Codex reset credits and Claude reset grants."""

import asyncio

from nonebot import logger

from .models import (
    CodexAccountResetCredits,
    QuotaCredential,
    ResetOverview,
)
from .portal import PortalClient, PortalError
from .quota import extract_accounts

_RESET_QUERY_CONCURRENCY = 4


async def load_reset_overview(portal: PortalClient) -> ResetOverview:
    # Keeper's official refresh caches every Claude grant; its live grant
    # endpoint only lists grants usable this instant, so read the cache.
    try:
        snapshot = await portal.quota()
    except PortalError as error:
        logger.warning("Mzk1 AI reset grants query failed: {}", type(error).__name__)
        snapshot = None
    if snapshot is not None:
        credentials = snapshot.credentials
        claude = tuple(
            account
            for account in extract_accounts(snapshot)
            if account.provider == "claude"
        )
    else:
        claude = None
        # The account index still parses when only Keeper's cache is malformed.
        try:
            credentials = await portal.quota_credentials()
        except PortalError as error:
            logger.warning(
                "Mzk1 AI reset credits query failed: {}", type(error).__name__
            )
            credentials = None
    codex = None if credentials is None else await _load_codex(portal, credentials)
    return ResetOverview(codex=codex, claude=claude)


async def _load_codex(
    portal: PortalClient, credentials: list[QuotaCredential]
) -> tuple[CodexAccountResetCredits, ...]:
    semaphore = asyncio.Semaphore(_RESET_QUERY_CONCURRENCY)
    return tuple(
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
