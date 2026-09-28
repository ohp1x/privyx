"""Built-in detectors shipped with Privyx.

These are intentionally dependency-free: pure regex + simple heuristics.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Callable

from privyx.core.context import Context
from privyx.core.result import Detection, Span
from privyx.privacy.detector.base import BaseDetector, Detector

#: The phone pattern accepts an optional ``+<country>`` prefix and 2-3 groups
#: separated by space/dot/dash, or a number without separators in E.164 form
#: (``+6281234567890``) or starting with ``08`` (``081234567890``).  The
#: lookarounds keep it from biting a chunk out of a longer digit run:
#: ``(?<!\d[-.])`` and ``(?![-.]\d)`` stop it matching inside an IP address or
#: SSN, where a more specific pattern should win.
PHONE_PATTERN = (
    r"(?<![\w\d])(?<!\d[-.])"
    r"(?:(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]\d{3,4}(?:[\s.-]\d{3,4})?"
    r"|\+[1-9]\d{9,14}|08\d{8,11})"
    r"(?![\w\d])(?![-.]\d)"
)

#: Vendor-prefixed keys and webhook URLs, one vendor per line.  ``(?<![\w-])``
#: keeps ``task-...`` from reading as ``sk-...``; ``(?<=\\[nrt])`` still allows
#: one right after an escaped ``\n``.
API_KEY_PATTERN = r"""(?x)
(?:(?<=\\[nrt])|(?<![\w-]))
(?:
    sk-[\w-]{16,}                           # OpenAI, Anthropic, DeepSeek, OpenRouter, gateways
  | (?:sk|rk)_(?:live|test)_\w{16,}         # Stripe secret / restricted
  | whsec_\w{24,}                           # Stripe webhook
  | gh[pousr]_\w{36,} | github_pat_\w{22,}  # GitHub
  | gl(?:pat|dt|ptt|rt|oas|soat|cbt|ffct)-[\w-]{20,}   # GitLab
  | (?:AKIA|ASIA)[0-9A-Z]{16}               # AWS access key id
  | AIza[\w-]{35} | GOCSPX-[\w-]{24,} | ya29\.[\w-]{20,}   # Google
  | xox[abposre]-[\w-]{10,} | xapp-\d-[\w-]{10,}           # Slack
  | hf_\w{30,}                              # Hugging Face
  | gsk_\w{40,} | xai-\w{40,} | r8_\w{30,} | pplx-\w{40,} | nvapi-[\w-]{40,}
  | npm_\w{36,} | pypi-AgEIcHlwaS5vcmc[\w-]{50,}
  | SG\.[\w-]{20,}\.[\w-]{20,}              # SendGrid
  | do[opr]_v1_[0-9a-f]{64}                 # DigitalOcean
  | shp(?:at|ca|pa|ss)_[0-9a-fA-F]{32}      # Shopify
  | ATATT3[\w=-]{100,}                      # Atlassian
  | lin_api_\w{40}                          # Linear
  | sbp_[0-9a-f]{40}                        # Supabase
  | sntry[su]_[\w+/=]{40,}                  # Sentry
  | AGE-SECRET-KEY-1[0-9A-Z]{58}            # age
  | PMAK-[0-9a-f]{24}-[0-9a-f]{34}          # Postman
)
| (?<!\d)\d{8,10}:AA[\w-]{33}               # Telegram bot, also inside /bot<token>/
| hooks\.slack\.com/(?:services|workflows|triggers)/[\w/]+
| discord(?:app)?\.com/api/webhooks/\d+/[\w-]+
"""

#: PEM / PGP private key blocks.
PRIVATE_KEY_PATTERN = r"""(?x)
-----BEGIN[A-Z0-9 ]*PRIVATE\ KEY(?:\ BLOCK)?-----
(?:
    (?:(?!-----BEGIN)[\s\S])*?-----END[A-Z0-9 ]*PRIVATE\ KEY(?:\ BLOCK)?-----
  | (?:(?:\s|\\[rn])+[\w+/=-]{16,})+        # cut off before END: still mask the body
)
"""

#: ``scheme://user:PASSWORD@host``: DB URLs, git remotes, ``redis://:pw@``.
#: Bounded lengths keep it linear on long runs without ``://``.
URL_CREDENTIAL_PATTERN = (
    r"""(?ix) [a-z][a-z0-9+.-]{0,20}:// [^\s:/@"'<>]{0,256} : (?P<value>[^\s/@"'<>]{1,256}) @"""
)

#: A value assigned to a secret-looking name: ``.env``, shell, YAML, JSON, PHP,
#: JS/Python, query strings, CLI flags.  Quoted, up to the closing quote;
#: unquoted, up to whitespace or a delimiter, skipping type hints
#: (``password: str``), ``${…}`` references, and calls (``token = get_token()``).
#: ``_KEY`` must be upper case: ``cache_key`` or ``sort_key`` in code is no
#: secret.  :func:`_secret_value` drops the rest of what cannot be one.
SECRET_PATTERN = r"""(?ix)
(?P<name>
    (?: passw(?:or)?d | passphrase | pwd | pass(?![a-z]) | secret(?![a-z]) | token(?![a-z])
      | api[_-]?key | access[_-]?key | private[_-]?key | (?-i:_KEY) | credentials? )
    [\w.-]{0,40}? )
["']? [ \t]* (?P<sep> => | := | [:=](?!=) ) [ \t]*
(?: (?P<q>["'`])
  | (?! (?:str|string|int|bool|boolean|number|any|none|null|true|false|undefined|optional
           |dict|list)\b
      | \$[{(] ) )
(?P<value> (?(q) (?:\\.|(?!(?P=q))[^\r\n\\])+ | [^\s"'`,;&#(){}\[\]<>]++(?!\() ) )
"""

#: Built-in entity patterns.  A ``regex`` detector built from config layers its
#: own patterns over these (:func:`privyx.privacy.detector.yaml.build_detector`).
DEFAULT_PATTERNS: dict[str, str] = {
    # Capped at the RFC 5321 lengths to stay linear: with `+`, every start in a
    # long run without `@` (hex, base64) rescanned the rest of the run.
    "EMAIL": r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,63}",
    "PHONE": PHONE_PATTERN,
    "CREDIT_CARD": r"\b(?:\d[ -]*?){13,16}\b",
    "IP_ADDRESS": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "API_KEY": API_KEY_PATTERN,
    "JWT": r"(?:(?<=\\[nrt])|(?<![\w-]))eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]*",
    "PRIVATE_KEY": PRIVATE_KEY_PATTERN,
    # `Authorization: Bearer …` / `Basic …`
    "AUTH_TOKEN": r"(?i)(?:bearer|basic)[ \t]+(?P<value>[\w.~+/-]{16,}=*)",
    "URL_CREDENTIAL": URL_CREDENTIAL_PATTERN,
    "SECRET": SECRET_PATTERN,
}


def _luhn(match: re.Match[str]) -> bool:
    """Whether the digits pass the Luhn checksum, as every card number does."""
    digits = [int(char) for char in match[0] if char.isdigit()]
    doubled = [sum(divmod(2 * digit, 10)) for digit in digits[-2::-2]]
    return (sum(digits[-1::-2]) + sum(doubled)) % 10 == 0


#: Set aside for documentation and examples (RFC 5737).
_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def _host_address(match: re.Match[str]) -> bool:
    """Whether the dotted quad is an address that can point at a real host.

    An octet above 255 makes a version number, not an address.  Loopback,
    ``0.0.0.0``, and the documentation ranges point at no host in particular.
    Private ranges count: they map out an internal network.
    """
    octets = [int(octet) for octet in match[0].split(".")]
    if max(octets) > 255:
        return False
    address = ipaddress.IPv4Address(bytes(octets))
    return not (
        address.is_loopback
        or address.is_unspecified
        or any(address in network for network in _DOCUMENTATION_NETWORKS)
    )


def _phone_number(match: re.Match[str]) -> bool:
    """Whether the match reads as a phone number rather than two numbers.

    Two bare digit groups are more often a screen size, the seconds and year of
    a date, or an ID (``1920 1080``, ``11 2026``, ``2026-0042``) than a local
    number, so fewer than three groups need a ``+<country>`` code, an area code
    in parentheses, or a leading 0.
    """
    number = match[0]
    return number[0] in "+(0" or len(re.findall(r"\d+", number)) > 2


#: Names that describe a secret rather than hold it: ``TOKEN_URL``, ``token_type``,
#: ``KEY_FILE``, ``passwordLength``.
_SECRET_METADATA = re.compile(
    r"(?:[_.-](?i:url|uri|endpoint|path|file|header|name|type|length|prefix)"
    r"|Url|Uri|Endpoint|Path|File|Header|Name|Type|Length|Prefix)$"
)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_WORDS_NEXT = re.compile(r"[ \t]+[A-Za-z]")


def _secret_value(match: re.Match[str]) -> bool:
    """Whether the value assigned to a secret-looking name can be the secret.

    Not when it has no letter or digit (``-`` in ``${API_KEY:-}``, ``**`` in
    ``**Token:**``) or is cut short (``sk-...``), or when the name describes the
    secret instead of holding it (``TOKEN_URL``, ``token_type``).  Unquoted, not
    when it refers to a variable (``os.environ``, ``settings.SECRET_KEY``) or to
    one named for the same thing (``api_key=api_key``, ``self._token = token``),
    or when a single word after a colon starts a sentence (``api_key: When set,
    …`` in a docstring).
    """
    name, value = match["name"], match["value"]
    if not any(char.isalnum() for char in value) or "..." in value or "…" in value:
        return False
    if _SECRET_METADATA.search(name):
        return False
    if match["q"] is not None:
        return True
    if _DOTTED_IDENTIFIER.fullmatch(value):
        return False
    if _IDENTIFIER.fullmatch(value) and _named_for(value, name):
        return False
    return not (
        match["sep"] == ":" and value.isalpha() and _WORDS_NEXT.match(match.string, match.end())
    )


def _named_for(variable: str, name: str) -> bool:
    """Whether ``variable`` is named for ``name``: ``api_key``, ``openai_api_key``, ``apiKey``."""
    key = name.lstrip("_").lower()
    variable = variable.lstrip("_")
    start = len(variable) - len(key)
    return variable.lower().endswith(key) and (
        start == 0 or variable[start - 1] == "_" or variable[start].isupper()
    )


#: Checks a match of a built-in pattern must pass to count.  They belong to the
#: built-in pattern: your own pattern for the same entity is used as written.
_CHECKS: dict[str, Callable[[re.Match[str]], bool]] = {
    # Timestamps, long IDs, and a `git log` hash next to its date are digit runs too.
    "CREDIT_CARD": _luhn,
    "IP_ADDRESS": _host_address,
    "PHONE": _phone_number,
    "SECRET": _secret_value,
}


def _detect(compiled: list[tuple[str, re.Pattern[str]]], text: str) -> Detection:
    """Run each entity's regex over ``text``.

    A pattern with a ``value`` group marks only that group, so
    ``DB_PASSWORD=(?P<value>\\S+)`` hides the secret but leaves the variable
    name for the model to read.  A match whose ``value`` group did not take part
    (another alternative matched) marks the whole match; an empty span marks
    nothing.  A built-in pattern's match must also pass its entity's check in
    :data:`_CHECKS`.
    """
    detection = Detection()
    for entity, pattern in compiled:
        has_value = "value" in pattern.groupindex
        check = _CHECKS.get(entity) if pattern.pattern == DEFAULT_PATTERNS.get(entity) else None
        for match in pattern.finditer(text):
            if check is not None and not check(match):
                continue
            group = "value" if has_value and match["value"] is not None else 0
            start, end = match.span(group)
            if start < end:
                detection.add(start, end, entity, match[group])
    return detection


class RegexDetector(BaseDetector):
    """Detect entities with a set of named regular expressions."""

    name = "regex"

    def __init__(self, patterns: dict[str, str] | None = None) -> None:
        super().__init__()
        patterns = patterns or dict(DEFAULT_PATTERNS)
        self._compiled = [(name, re.compile(pattern)) for name, pattern in patterns.items()]

    def detect_sync(self, text: str, context: Context) -> Detection:
        return _detect(self._compiled, text)


class YamlDetector(BaseDetector):
    """Composite detector configured from YAML (entity -> regex list).

    Example config:

    .. code-block:: yaml

        detectors:
          - name: custom
            type: regex
            patterns:
              LICENSE_PLATE: "[A-Z]{1,3}-\\d{1,4}"
    """

    name = "yaml"

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        super().__init__()
        self._rules = rules or {}
        self._compiled = [(entity, re.compile(pattern)) for entity, pattern in self._rules.items()]

    def detect_sync(self, text: str, context: Context) -> Detection:
        return _detect(self._compiled, text)


class CompositeDetector:
    """Run several detectors concurrently and pool their spans.

    Overlaps are left in place on purpose: the policy must see every span before
    the operator merges them.  Merging first could fold an allowed EMAIL into a
    longer span of a type ``strict`` drops, and the email would leak.  Only exact
    duplicates (two detectors finding the same entity at the same range) are
    collapsed, so audit counts are not doubled.  A failing detector fails the
    whole detection rather than silently skipping its spans.
    """

    def __init__(self, detectors: list[Detector]) -> None:
        self._detectors = detectors
        self.name = "+".join(d.name for d in detectors)

    async def detect(self, text: str, context: Context) -> Detection:
        results = await asyncio.gather(*(d.detect(text, context) for d in self._detectors))
        unique: dict[tuple[int, int, str], Span] = {}
        for result in results:
            for span in result.spans:
                unique.setdefault((span.start, span.end, span.entity_type), span)
        return Detection(spans=list(unique.values()), cacheable=all(r.cacheable for r in results))
