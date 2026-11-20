"""Build the token codec of a config's ``token`` section."""

from __future__ import annotations

from privyx.core import ConfigError
from privyx.token.codec import CompactCodec, FormatCodec, LabelCodec


def build_codec(
    format: str, namespace: str, *, compact: bool = False, label_format: str = ""
) -> FormatCodec:
    """The codec of ``token.format``, ``.namespace``, ``.compact``, and ``.label_format``.

    Raises:
        TokenFormatError: If ``format`` or ``label_format`` is unusable (a
            ``ConfigError``, so an invalid syntax fails at startup rather than
            on the first request).
        ConfigError: If ``label_format`` is set with ``compact``.
    """
    if label_format:
        if compact:
            raise ConfigError("token.label_format cannot be combined with token.compact")
        return LabelCodec(format, label_format, namespace)
    codec = FormatCodec(format, namespace)
    return CompactCodec(codec) if compact else codec
