"""Subscription reminders driven by wall-clock time, not quota refreshes.

Only credentials whose provider reports an expiry (Codex id_token claims)
produce reminders; Claude credentials carry no expiry.
"""

from datetime import datetime, timedelta

from .alerts import AlertEvaluation, AlertEvent, SubscriptionExpiringAlert
from .models import QuotaCredential
from .quota import credential_provider
from .storage import CredentialAlertState, PersistedState, SubscriptionAlertState


def evaluate_subscriptions(
    state: PersistedState,
    credentials: list[QuotaCredential],
    alert_hours: tuple[int, ...],
    now: datetime,
) -> AlertEvaluation:
    active_expiries = {
        credential.credential_id: credential.subscription_active_until
        for credential in credentials
        if credential_provider(credential) is not None
        and not credential.disabled
        and credential.subscription_active_until is not None
    }
    pending = state.pending_notifications
    state.pending_notifications = [
        item
        for item in pending
        if item.subscription is None
        or (
            active_expiries.get(item.subscription.credential_id)
            == item.subscription.active_until
            and item.subscription.active_until > now
        )
    ]
    events: list[AlertEvent] = []
    changed = len(pending) != len(state.pending_notifications)
    for credential in credentials:
        active_until = credential.subscription_active_until
        provider = credential_provider(credential)
        if provider is None or credential.disabled or active_until is None:
            continue
        account = state.credentials.get(credential.credential_id)
        if account is None:
            account = CredentialAlertState()
            state.credentials[credential.credential_id] = account
        subscription = account.subscription
        if subscription is None or subscription.active_until != active_until:
            subscription = SubscriptionAlertState(active_until=active_until)
            account.subscription = subscription
            changed = True

        remaining = active_until - now
        if remaining <= timedelta(0):
            continue
        reached = {
            hours for hours in alert_hours if remaining <= timedelta(hours=hours)
        }
        if not reached - subscription.notified_hours:
            continue
        subscription.notified_hours.update(reached)
        changed = True
        events.append(
            SubscriptionExpiringAlert(
                kind="subscription_expiring",
                provider=provider,
                credential_id=credential.credential_id,
                display_name=credential.display_name,
                active_until=active_until,
                threshold_hours=min(reached),
                observed_at=now,
            )
        )
    return AlertEvaluation(events=tuple(events), changed=changed)
