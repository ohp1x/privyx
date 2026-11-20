"""Operators: replace a detection's spans in a text, and restore them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.operators._base import Operator
from privyx.operators._restore import restore

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.operators._encrypt import EncryptOperator
    from privyx.operators._faker import FakerOperator
    from privyx.operators._fpe import FPEOperator
    from privyx.operators._hash import HashOperator
    from privyx.operators._partial import PartialOperator
    from privyx.operators._pseudonym import PseudonymOperator
    from privyx.operators._redact import RedactOperator

__all__ = [
    "EncryptOperator",
    "FPEOperator",
    "FakerOperator",
    "HashOperator",
    "Operator",
    "PartialOperator",
    "PseudonymOperator",
    "RedactOperator",
    "restore",
]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "pseudonym": "privyx.operators._pseudonym:PseudonymOperator",
    "redact": "privyx.operators._redact:RedactOperator",
    "hash": "privyx.operators._hash:HashOperator",
    "faker": "privyx.operators._faker:FakerOperator",
    "encrypt": "privyx.operators._encrypt:EncryptOperator",
    "partial": "privyx.operators._partial:PartialOperator",
    "fpe": "privyx.operators._fpe:FPEOperator",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.operators import FakerOperator`` imports it on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in operators with ``app``."""
    app.registry(Operator).builtins(_BUILTINS)
