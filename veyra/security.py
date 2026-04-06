from cryptography.fernet import Fernet

from veyra.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.fernet_key
        if not key:
            # Auto-generate a key for development; warn in logs
            key = Fernet.generate_key().decode()
            import warnings
            warnings.warn(
                "VEYRA_FERNET_KEY not set — generated a temporary key. "
                "Stored passwords will be unreadable after restart.",
                stacklevel=2,
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()
