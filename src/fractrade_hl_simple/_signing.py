"""The state of each signing key, shared by every client in the process.

One registry: two clients that sign with the same key must not use the same nonce, and a
stop loss or take profit replacement must not be interleaved with another signed action.
"""
import threading
from typing import Dict


class SignerState:
    """Locks and last nonce of one signing key, shared by every client in the process.

    `lock` serializes single signed actions; `operation_lock` serializes multi-step
    operations (stop loss / take profit replacement) and is held around `lock`, never inside.
    """

    __slots__ = ("lock", "operation_lock", "last_nonce_ms")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.operation_lock = threading.RLock()
        self.last_nonce_ms = 0


_signers: Dict[str, SignerState] = {}
_signers_lock = threading.Lock()


def signer_state(address: str) -> SignerState:
    with _signers_lock:
        return _signers.setdefault(str(address).lower(), SignerState())
