import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet


class PolicyError(ValueError):
    pass


def mask(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(
        r"(?i)(password|token|secret|api[_-]?key)([\s\"':=]+)[^\s,\"'}]+", r"\1\2[REDACTED]", value
    )
    value = re.sub(r"(://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(
            part in key.upper() for part in ("SECRET", "TOKEN", "PASSWORD", "API_KEY")
        ):
            value = value.replace(secret, "[REDACTED]")
    return value[:8000]


def prompt_hash(prompt: str, key: str) -> str:
    if not key:
        raise PolicyError("Configure FOUNDRY_PROMPT_HASH_KEY")
    return hmac.new(key.encode(), " ".join(prompt.lower().split()).encode(), hashlib.sha256).hexdigest()


def encrypt_payload(value: dict, key: str) -> str:
    if not key:
        raise PolicyError("Configure FOUNDRY_JOB_ENCRYPTION_KEY before enabling builds")
    return Fernet(key.encode()).encrypt(json.dumps(value).encode()).decode()


def decrypt_payload(value: str, key: str) -> dict:
    return json.loads(Fernet(key.encode()).decrypt(value.encode()))


def safe_path(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative:
        raise PolicyError("Invalid relative path")
    base = root.resolve()
    path = (base / relative).resolve()
    if path == base or not path.is_relative_to(base):
        raise PolicyError("Path escapes its approved root")
    current = base
    for component in Path(relative).parts:
        current /= component
        if current.is_symlink():
            raise PolicyError("Symlinks are prohibited")
    return path
