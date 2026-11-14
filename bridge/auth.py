"""Who may change things through the bridge.

Reading stays open on the home network; everything that changes a device, the
router or the bridge's settings needs one of:

- an app's gateway access token (`Authorization: Bearer <token>`), the one the app
  got when the user pressed Allow on the iHost; the bridge asks the gateway
  whether it is genuine (see BridgeApp.hub_token_ok);
- a setup-page session (`X-Bridge-Session`), handed out for the setup PIN.

The PIN is chosen on the setup page the first time it is opened after this
version is installed, and stored only as a salted PBKDF2 hash. A forgotten PIN is
cleared by pressing Allow on the iHost (the same dialog as Get Token), which only
someone signed in to the iHost console can do. Wrong PINs lock the check for a
while, doubling each time.

Gateway directives on /webhook carry no credentials, so that route instead only
accepts requests from the gateway's own addresses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

log = logging.getLogger(__name__)

PIN_MIN_LENGTH = 6
SESSION_TTL = 30 * 86400
TRIES_BEFORE_LOCK = 5
FIRST_LOCK = 300
MAX_LOCK = 86400
PBKDF2_ROUNDS = 200_000


class PinLocked(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"too many wrong PINs; try again in {max(1, seconds // 60)} min")
        self.seconds = seconds


class SetupAuth:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "setup_pin.json"
        self._sessions: dict[str, float] = {}  # sha256(session) -> expiry (monotonic)
        self._failures = 0
        self._locks = 0
        self._locked_until = 0.0

    # ----- the PIN ---------------------------------------------------------------

    def _stored(self) -> dict[str, str] | None:
        try:
            data = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("hash") and data.get("salt") else None

    @property
    def pin_set(self) -> bool:
        return self._stored() is not None

    @staticmethod
    def _hash(pin: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ROUNDS).hex()

    def set_first_pin(self, pin: str) -> str:
        """Choose the PIN when none exists yet. Returns a session."""
        if self.pin_set:
            raise PermissionError("a setup PIN is already set")
        pin = str(pin or "")
        if len(pin) < PIN_MIN_LENGTH:
            raise ValueError(f"the PIN needs at least {PIN_MIN_LENGTH} characters")
        salt = secrets.token_bytes(16)
        self.file.write_text(json.dumps({"salt": salt.hex(), "hash": self._hash(pin, salt)}))
        try:
            self.file.chmod(0o600)
        except OSError:
            pass
        log.info("Setup PIN chosen")
        return self._new_session()

    def unlock(self, pin: str) -> str | None:
        """A session for the right PIN, None for a wrong one. Raises PinLocked while locked."""
        now = time.monotonic()
        if now < self._locked_until:
            raise PinLocked(int(self._locked_until - now))
        stored = self._stored()
        if stored is None:
            return None
        if hmac.compare_digest(self._hash(str(pin or ""), bytes.fromhex(stored["salt"])), stored["hash"]):
            self._failures = 0
            self._locks = 0
            return self._new_session()
        self._failures += 1
        log.warning("Wrong setup PIN (%d in a row)", self._failures)
        if self._failures >= TRIES_BEFORE_LOCK:
            self._failures = 0
            self._locked_until = now + min(MAX_LOCK, FIRST_LOCK * 2**self._locks)
            self._locks += 1
            raise PinLocked(int(self._locked_until - now))
        return None

    def clear(self) -> None:
        """Forget the PIN and every session (after Allow was pressed on the iHost)."""
        try:
            self.file.unlink()
        except FileNotFoundError:
            pass
        self._sessions.clear()
        self._failures = self._locks = 0
        self._locked_until = 0.0
        log.warning("Setup PIN cleared; the next visitor of the setup page chooses a new one")

    # ----- sessions -----------------------------------------------------------------

    @staticmethod
    def _key(session: str) -> str:
        return hashlib.sha256(session.encode()).hexdigest()

    def _new_session(self) -> str:
        now = time.monotonic()
        self._sessions = {k: v for k, v in self._sessions.items() if v > now}
        session = secrets.token_urlsafe(32)
        self._sessions[self._key(session)] = now + SESSION_TTL
        return session

    def session_ok(self, session: str | None) -> bool:
        if not session:
            return False
        until = self._sessions.get(self._key(session))
        return until is not None and until > time.monotonic()
