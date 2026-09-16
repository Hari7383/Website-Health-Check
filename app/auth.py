"""User accounts and password verification.

Passwords are stored as scrypt hashes with a per-user random salt. Plaintext
is never written to disk and never logged. Accounts are created from the
command line (`python manage.py adduser`), so a password is typed only into a
terminal prompt that does not echo.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone

from .config import USERS_FILE

MIN_PASSWORD_LENGTH = 10
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")

# scrypt parameters. n=2**15 keeps verification near ~100ms on a laptop.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 64


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
        maxmem=64 * 1024 * 1024,
    )


def _load() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(users: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2)
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass  # best effort; Windows ACLs differ


def user_count() -> int:
    return len(_load())


def list_users() -> list:
    users = _load()
    return sorted(
        ({"username": u, "display_name": d.get("display_name", u)} for u, d in users.items()),
        key=lambda x: x["username"],
    )


def user_exists(username: str) -> bool:
    return (username or "").strip().lower() in _load()


def add_user(username: str, password: str, display_name: str, replace: bool = False) -> None:
    """Create an account. Raises ValueError on bad input.

    `replace` must be set explicitly to overwrite an existing account, so a
    self-service sign-up can never take over someone else's username.
    """
    username = (username or "").strip().lower()
    display_name = (display_name or "").strip()

    if not username:
        raise ValueError("Username cannot be empty.")
    if not USERNAME_RE.match(username):
        raise ValueError("Username may use letters, numbers, dots, hyphens and underscores "
                         "only, and must be 3 to 32 characters.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if not display_name:
        raise ValueError("Display name cannot be empty — it is written into the Checked By column.")
    if len(display_name) > 60:
        raise ValueError("Display name must be 60 characters or fewer.")

    users = _load()
    if username in users and not replace:
        raise ValueError(f"The username '{username}' is already taken. Pick another.")
    salt = secrets.token_bytes(16)
    users[username] = {
        "display_name": display_name,
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save(users)


def delete_user(username: str) -> bool:
    users = _load()
    if username.strip().lower() in users:
        del users[username.strip().lower()]
        _save(users)
        return True
    return False


def verify(username: str, password: str):
    """Return the user record on success, else None.

    Runs the KDF even when the user does not exist, so a missing account and a
    wrong password take comparable time.
    """
    users = _load()
    record = users.get((username or "").strip().lower())

    if record is None:
        _hash(password or "", b"\x00" * 16)  # equalise timing
        return None

    expected = bytes.fromhex(record["hash"])
    actual = _hash(password or "", bytes.fromhex(record["salt"]))
    if hmac.compare_digest(expected, actual):
        return {"username": (username or "").strip().lower(),
                "display_name": record.get("display_name", username)}
    return None
