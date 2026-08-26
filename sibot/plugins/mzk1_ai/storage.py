"""Persistent alert state stored as one atomically replaced JSON file."""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_STATE_VERSION = 1


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeeklyAlertState(StateModel):
    remaining_percent: float
    reset_at: datetime
    window_seconds: int
    notified_thresholds: set[int] = Field(default_factory=set)


class CredentialAlertState(StateModel):
    auth_status: Literal["unknown", "normal", "invalid"] = "unknown"
    last_refreshed_at: datetime | None = None
    weekly: WeeklyAlertState | None = None


class PendingNotification(StateModel):
    event_key: str
    group_id: int
    message: str
    created_at: datetime
    attempts: int = 0
    next_attempt_at: datetime


class PersistedState(StateModel):
    state_version: Literal[1] = _STATE_VERSION
    credentials: dict[str, CredentialAlertState] = Field(default_factory=dict)
    pending_notifications: list[PendingNotification] = Field(default_factory=list)


class StateLoadError(RuntimeError):
    """The persisted state file is unreadable or invalid."""

    def __init__(self) -> None:
        super().__init__("Mzk1 AI state file is unreadable or invalid")


class StateStore:
    """Serialize access and atomically replace the state file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def load(self) -> PersistedState:
        async with self._lock:
            return await asyncio.to_thread(self._load_sync)

    async def save(self, state: PersistedState) -> None:
        async with self._lock:
            payload = f"{state.model_dump_json(indent=2)}\n"
            await asyncio.to_thread(self._save_sync, payload)

    def _load_sync(self) -> PersistedState:
        try:
            payload = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PersistedState()
        except OSError as error:
            raise StateLoadError from error

        try:
            return PersistedState.model_validate_json(payload)
        except (ValidationError, ValueError) as error:
            raise StateLoadError from error

    def _save_sync(self, payload: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self._path)
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _fsync_directory(self) -> None:
        try:
            directory_fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
