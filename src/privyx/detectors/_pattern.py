"""The pattern engine of the ``regex`` and ``yaml`` detectors.

The built-in entity patterns and the checks their matches must pass, the
compiling of ``terms`` and ``patterns`` from a config into one pattern per
entity, and the scan itself.  Dependency-free: pure regex and simple
heuristics.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from privyx.core import ConfigError, Detection
from privyx.detectors import _catalog as catalog

#: The phone pattern accepts an optional ``+<country>`` prefix and 2-3 groups
#: separated by space/dot/dash, or a number without separators in E.164 form
#: (``+6281234567890``) or starting with ``08`` (``081234567890``).  The
#: lookarounds keep it from biting a chunk out of a longer digit run:
#: ``(?<!\d[-.])`` and ``(?![-.]\d)`` stop it matching inside an IP address or
#: SSN, where a more specific pattern should win, and ``(?<!\d:)`` keeps it off
#: the seconds and milliseconds of a time (``09:49:08.123``).
PHONE_PATTERN = (
    r"(?<![\w\d])(?<!\d[-.])(?<!\d:)"
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

_SECRET_TYPE_NAMES: tuple[str, ...] = (
    "str",
    "string",
    "int",
    "bool",
    "boolean",
    "number",
    "any",
    "none",
    "null",
    "true",
    "false",
    "undefined",
    "optional",
    "dict",
    "list",
)
_SECRET_TYPE_NAMES_SET: frozenset[str] = frozenset(_SECRET_TYPE_NAMES)

#: A value assigned to a secret-looking name: ``.env``, shell, YAML, JSON, PHP,
#: JS/Python, query strings, CLI flags.  Simple ``str`` / ``string`` annotations
#: are allowed before an assignment. Quoted, up to the closing quote (including
#: Python triple quotes), with or without a string prefix (``b"…"``); unquoted,
#: up to whitespace or a delimiter, skipping type hints (``password: str``),
#: ``${…}`` references, and calls (``token = get_token()``).
#: ``_KEY`` must be upper case: ``cache_key`` or ``sort_key`` in code is no
#: secret.  :func:`_secret_value` drops the rest of what cannot be one.
#: A whitespace separator needs a CLI flag; quote selection is atomic so an
#: empty triple-quoted string cannot backtrack into a nonempty single-quoted one.
SECRET_PATTERN = (
    r"""(?ix)
(?P<flag> --(?:\w{1,40}-)*?(?:require)? )?
(?P<name>
    (?: passw(?:or)?d | passphrase | pwd | pass(?![a-z]) | secret(?![a-z]) | token(?![a-z])
      | api[_-]?key | access[_-]?key | private[_-]?key | (?-i:_KEY) | credentials? )
    [\w.-]{0,40}? )
["']? [ \t]* (?: :[ \t]*(?:str|string)[ \t]*(?==) )?
(?P<sep> => | := | [:=](?!=) | (?(flag)[ \t]+|(?!)) ) [ \t]*
(?: (?:[rubf]|[bf]r|r[bf])? (?> (?P<triple> \"{3} | '{3} ) | (?P<q>["'`]) )
  | (?! (?:"""
    + "|".join(_SECRET_TYPE_NAMES)
    + r""")\b
      | \$[{(] ) )
(?P<value>
    (?(triple) (?:\\[\s\S]|(?!(?P=triple))[^\\])+
      | (?(q) (?:\\.|(?!(?P=q))[^\r\n\\])+
          | [^\s"'`,;&#(){}\[\]<>]++(?!\() ) )
)
"""
)


#: Built-in entity patterns.  A ``regex`` detector layers its own patterns over
#: these.
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
    **catalog.CHECKED_PATTERNS,
}

#: Built-in entities a detector turns on with ``enable``.
OPTIONAL_PATTERNS: dict[str, str] = dict(catalog.OPTIONAL_PATTERNS)

#: The built-ins ``enable`` accepts: the opt-in ones, and the checked ones a
#: ``yaml`` detector lacks (a no-op in a ``regex`` detector, which has them).
ENABLEABLE_PATTERNS: dict[str, str] = {**catalog.CHECKED_PATTERNS, **OPTIONAL_PATTERNS}


def _luhn(match: re.Match[str]) -> bool:
    """Whether the digits pass the Luhn checksum, as every card number does."""
    digits = [int(char) for char in match[0] if char.isdigit()]
    doubled = [sum(divmod(2 * digit, 10)) for digit in digits[-2::-2]]
    return (sum(digits[-1::-2]) + sum(doubled)) % 10 == 0


def _card_number(match: re.Match[str]) -> bool:
    """Whether the digits can be a card number: not a Unix time, and Luhn-valid.

    Unix time in milliseconds (13 digits) or microseconds (16) starts with a 1
    until 2033, and one in ten passes the Luhn check.  Of the card networks only
    UATP, an airline card, starts with a 1.
    """
    return not match[0].startswith("1") and _luhn(match)


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


_URL_USERINFO = re.compile(r"""(?i)[a-z][a-z0-9+.-]{0,20}://[^\s:/@"'<>]{0,256}:$""")


def _email_address(match: re.Match[str]) -> bool:
    """A URL password followed by ``@host`` is not an email address.

    URL_CREDENTIAL already captures the password. Treating password@host as an
    email would merge both spans and mask the hostname too. Keep the hostname
    visible and classify the password as a credential.
    """
    return _URL_USERINFO.search(match.string[max(0, match.start() - 282) : match.start()]) is None


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
#: ``KEY_FILE``, ``passwordLength``, ``API_KEY_PATTERN``, and counts, rates, and
#: limits: ``token_count``, ``pass_rate``, ``TOKEN_LIMIT``, ``tokenTtl``.
_SECRET_METADATA = re.compile(
    r"(?:[_.-](?i:url|uri|endpoint|path|file|header|name|type|length|prefix|pattern|regex"
    r"|count|rate|limit|level|size|max|min|ttl|expiry)"
    r"|Url|Uri|Endpoint|Path|File|Header|Name|Type|Length|Prefix|Pattern|Regex"
    r"|Count|Rate|Limit|Level|Size|Max|Min|Ttl|Expiry)$"
)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_DOTTED_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_STRING_PREFIX = re.compile(r"(?i:[rubf]|[bf]r|r[bf])")
_WORDS_NEXT = re.compile(r"[ \t]+[A-Za-z]")


def _secret_value(match: re.Match[str]) -> bool:
    """Whether the value assigned to a secret-looking name can be the secret.

    Not when it has no letter or digit (``-`` in ``${API_KEY:-}``, ``**`` in
    ``**Token:**``) or is cut short (``sk-...``), or when the name describes the
    secret instead of holding it (``TOKEN_URL``, ``token_type``).  Unquoted, not
    when it refers to a variable (``os.environ``, ``settings.SECRET_KEY``) or to
    one named for the same thing (``api_key=api_key``, ``self._token = token``),
    when it is a subscripted name (``_KEYS: tuple[str, ...]``) or the prefix of a
    string literal (``r"…"``), or when a single word after a colon starts a
    sentence (``api_key: When set, …`` in a docstring).
    """
    groups = match.groupdict()
    name = groups.get("name")
    value = groups.get("value")
    if name is None or value is None:
        return True
    if not any(char.isalnum() for char in value) or "..." in value or "…" in value:
        return False
    if _SECRET_METADATA.search(name):
        return False
    # A following flag or a shell variable is not the credential itself.
    flag = groups.get("flag")
    if flag and value.startswith(("--", "$")):
        return False
    if bool(groups.get("q")) or bool(groups.get("triple")):
        return True
    if value.lower() in _SECRET_TYPE_NAMES_SET:
        return False
    if _DOTTED_IDENTIFIER.fullmatch(value):
        return False
    following = match.string[match.end() : match.end() + 1]
    if _IDENTIFIER.fullmatch(value) and (following == "[" or _named_for(value, name)):
        return False
    if _STRING_PREFIX.fullmatch(value) and following in ('"', "'"):
        return False
    sep = groups.get("sep")
    return not (sep == ":" and value.isalpha() and _WORDS_NEXT.match(match.string, match.end()))


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
    "EMAIL": _email_address,
    # Timestamps, long IDs, and a `git log` hash next to its date are digit runs too.
    "CREDIT_CARD": _card_number,
    "IP_ADDRESS": _host_address,
    "PHONE": _phone_number,
    "SECRET": _secret_value,
    **catalog.CHECKS,
}

#: Built-in entities whose match can start at a number written before the
#: value, so that the value starts inside the match, and whether to search
#: inside a match, given whether its check passed.  Inside a card candidate the
#: check rejects: the card number in ``7 4111-1111-1111-1111`` after the
#: rejected ``7 4111-1111-1111``.  Inside a phone number of bare groups the
#: check accepts, with no ``+<country>`` code, area code in parentheses, or
#: leading 0 to mark where it starts: the number in ``ref 2026 0812 3456 7890``
#: after ``2026 0812 3456``.  The other checked patterns cannot start inside a
#: candidate of their own, or would only find a tail of the rejected value,
#: which their check reads whole.
_SEARCH_INSIDE: dict[str, Callable[[re.Match[str], bool], bool]] = {
    "CREDIT_CARD": lambda match, accepted: not accepted,
    "PHONE": lambda match, accepted: accepted and match[0][0] not in "+(0",
}


# Every SECRET match starts with a CLI flag or one of these name prefixes.
# Explicit initial character classes let re skip unrelated text with its fast
# charset search; a global IGNORECASE flag disables that optimization. Long s
# (ſ) must be included because Python's IGNORECASE treats it as s.
_SECRET_START = re.compile(
    r"--|[pP](?i:ass|wd|rivate)|[sSſ](?i:ecret)|[tT](?i:oken)"
    r"|[aA](?i:pi|ccess)|_KEY|[cC](?i:redential)"
)


def _secret_matches(pattern: re.Pattern[str], text: str, check: Check) -> Iterator[re.Match[str]]:
    """The SECRET matches that pass ``check``, the regex tried only where one can start.

    On failure advance only one character: a valid name can start inside a
    failed candidate (``passpassword=...``).  A rejected match is searched
    again from the start of its value, as a secret can be assigned inside it
    (``TOKEN_URL=https://…?client_secret=…``), but only once: a match found
    there and rejected too is skipped whole.  The values inside an unquoted
    value end where it ends, so searching each of them again would rescan the
    rest of it once per name, quadratic in the length of ``TOKEN_URL=TOKEN_URL=…``.
    """
    pos = searched = 0
    while (candidate := _SECRET_START.search(text, pos)) is not None:
        match = pattern.match(text, candidate.start())
        if match is None:
            pos = candidate.start() + 1
        elif check(match):
            yield match
            pos = match.end()
        elif match.start() >= searched:
            searched, pos = match.end(), match.start("value")
        else:
            pos = match.end()


Check = Callable[[re.Match[str]], bool]


#: A letter or digit: ``\w`` without ``_``.
_ALNUM = r"[^\W_]"

#: Built-in entities whose word boundaries also fall at ``_`` with
#: ``underscore_separates_words``: ``host_10.0.0.1``, ``my_sk-…``.  Not ``SECRET``,
#: ``HIGH_ENTROPY``, or ``ASSIGNED_SECRET``, whose matches hold ``_`` themselves
#: (a name such as ``DB_PASSWORD``, a token's characters), nor ``EMAIL``,
#: ``PRIVATE_KEY``, and the rest, which have no word boundary to move.
UNDERSCORE_SPLIT: frozenset[str] = frozenset(
    {
        "PHONE",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "SSN",
        "API_KEY",
        "JWT",
        "CRYPTO_WALLET",
        "SG_NRIC_FIN",
        "ES_NIF",
        "ES_NIE",
        "MRN",
        "ICD10",
    }
)

#: Each word-boundary form those patterns use, and the same with ``_`` as a
#: separator, in the order they are replaced.
_UNDERSCORE_FORMS = (
    ("\\b", f"(?:(?<!{_ALNUM})(?={_ALNUM})|(?<={_ALNUM})(?!{_ALNUM}))"),
    ("(?<![\\w-])", f"(?<!{_ALNUM}|-)"),
    ("[\\w\\d]", _ALNUM),
    ("[\\w]", _ALNUM),
    ("(?<!\\w", f"(?<!{_ALNUM}"),
)


def split_underscore(pattern: str) -> str:
    """``pattern``, a built-in in :data:`UNDERSCORE_SPLIT`, with ``_`` as a word separator."""
    for old, new in _UNDERSCORE_FORMS:
        pattern = pattern.replace(old, new)
    return pattern


def _builtin(entity: str, pattern: str) -> bool:
    return pattern in (DEFAULT_PATTERNS.get(entity), OPTIONAL_PATTERNS.get(entity))


def _check_for(
    entity: str, pattern: re.Pattern[str], checks: dict[str, Check], builtin: bool
) -> Check | None:
    """The check a match of ``pattern`` must pass: its built-in entity's, if it is one.

    ``builtin`` says whether it is, also once :func:`split_underscore` changed it.
    A custom ``SECRET`` pattern with ``name`` and ``value`` groups keeps the
    built-in check too, so ``password: str`` stays unmasked.
    """
    if builtin or (entity == "SECRET" and {"name", "value"} <= pattern.groupindex.keys()):
        return checks.get(entity)
    return None


def _checked_matches(
    pattern: re.Pattern[str],
    text: str,
    check: Check,
    inside: Callable[[re.Match[str], bool], bool],
) -> Iterator[re.Match[str]]:
    """The matches of ``pattern`` in ``text`` that pass ``check``.

    Unlike finditer, the search goes on from the next character of a match
    ``inside`` picks, not from its end, so a value that starts inside it is
    found.  A match that ends no later than one found before adds nothing and
    is left out.
    """
    pos = end = 0
    while (match := pattern.search(text, pos)) is not None:
        accepted = check(match)
        if accepted and match.end() > end:
            yield match
            end = match.end()
        pos = match.start() + 1 if inside(match, accepted) else match.end()


def _detect(
    compiled: list[tuple[str, re.Pattern[str], Check | None, frozenset[str] | None]], text: str
) -> Detection:
    """Run each entity's regex over ``text``.

    A pattern with a ``value`` group marks only that group, so
    ``DB_PASSWORD=(?P<value>\\S+)`` hides the secret but leaves the variable
    name for the model to read.  A match whose ``value`` group did not take part
    (another alternative matched) marks the whole match; an empty span marks
    nothing.  A match must pass its entity's check, if it has one, and have one
    of its context words nearby, if it has any.
    """
    detection = Detection()
    for entity, pattern, check, context in compiled:
        has_value = "value" in pattern.groupindex
        if check is not None and entity == "SECRET" and pattern.pattern == SECRET_PATTERN:
            matches = _secret_matches(pattern, text, check)
            check = None  # already applied
        elif check is not None and entity in _SEARCH_INSIDE:
            matches = _checked_matches(pattern, text, check, _SEARCH_INSIDE[entity])
            check = None  # already applied
        else:
            matches = pattern.finditer(text)
        for match in matches:
            if check is not None and not check(match):
                continue
            group = "value" if has_value and match["value"] is not None else 0
            start, end = match.span(group)
            if start >= end:
                continue
            if context and not catalog.near(text, start, end, context):
                continue
            detection.add(start, end, entity, match[group])
    return detection


#: Entity names must round-trip through a token, so they follow the codec's
#: ``type`` grammar (see :mod:`privyx.token.codec`).
_ENTITY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

_log = logging.getLogger("privyx")


def _enabled(config: dict[str, Any]) -> dict[str, str]:
    """The built-in patterns ``enable`` lists."""
    _disabled(config)
    enable = config.get("enable") or []
    unknown = sorted(set(enable) - ENABLEABLE_PATTERNS.keys())
    if unknown:
        raise ConfigError(
            f"detector 'enable' lists unknown entities {unknown}; "
            f"it accepts {sorted(ENABLEABLE_PATTERNS)}"
        )
    return {entity: ENABLEABLE_PATTERNS[entity] for entity in enable}


def _disabled(config: dict[str, Any]) -> set[str]:
    """Validate opt-outs, including built-ins that are not enabled yet."""
    disable = set(config.get("disable") or [])
    known = DEFAULT_PATTERNS.keys() | OPTIONAL_PATTERNS.keys()
    unknown = sorted(disable - known)
    if unknown:
        raise ConfigError(
            f"detector 'disable' lists unknown entities {unknown}; "
            f"the built-ins are {sorted(known)}"
        )
    conflict = sorted(disable.intersection(config.get("enable") or []))
    if conflict:
        raise ConfigError(f"detector lists entities in both 'enable' and 'disable': {conflict}")
    return disable


def _builtins(config: dict[str, Any]) -> dict[str, str]:
    """The built-in patterns of a ``regex`` detector, but those ``disable`` lists."""
    disable = _disabled(config)
    return {entity: regex for entity, regex in DEFAULT_PATTERNS.items() if entity not in disable}


def _pattern_options(config: dict[str, Any], patterns: dict[str, str]) -> dict[str, Any]:
    """The ``context``, ``entropy_threshold``, ``decode``, ``normalize``, and
    ``underscore`` of a regex or yaml detector."""
    context = config.get("context") or {}
    unknown = sorted(set(context) - patterns.keys())
    if unknown:
        # Context only narrows an entity; one the detector lacks is a typo.
        raise ConfigError(f"detector 'context' names entities it does not detect: {unknown}")
    options: dict[str, Any] = {
        "context": context,
        "decode": bool(config.get("decode")),
        "normalize": bool(config.get("normalize")),
        "underscore": bool(config.get("underscore_separates_words")),
    }
    if config.get("entropy_threshold") is not None:
        options["entropy_threshold"] = float(config["entropy_threshold"])
    return options


def _validate(patterns: dict[str, str]) -> None:
    """Fail fast on an invalid entity name or regex rather than at first request.

    The name check mirrors the token codec's ``type`` grammar
    (:data:`privyx.token.codec._DEFAULT_GRAMMARS`): an entity that cannot be
    serialized into a token would otherwise only blow up mid-request, once
    something is actually detected.
    """
    for entity, pattern in patterns.items():
        if not _ENTITY_NAME.fullmatch(entity):
            raise ConfigError(
                f"invalid entity name {entity!r}: must start with a letter and "
                "contain only letters, digits, or underscores (max 64 chars)"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex for entity {entity!r}: {exc}") from exc
        if backtracks(pattern):
            # A warning, not an error: the same config was accepted before.
            _log.warning(
                "the regex for entity %r has a repeated group that repeats itself, like "
                "(a+)+, so a crafted input can stall the proxy; make the inner "
                "quantifier possessive (a++) or rewrite it",
                entity,
            )


def backtracks(pattern: str) -> bool:
    """Whether ``pattern`` can backtrack catastrophically (ReDoS).

    Flags an unbounded repeat whose body is itself, apart from parts that may
    match nothing, an unbounded repeat: ``(a+)+``, ``(\\w+\\s?)*``,
    ``(a+|b)+``.  Such a body can split one run of text in exponentially many
    ways, all tried before a match fails.  ``(?:[a-z]+\\.)+`` is fine: the
    ``\\.`` fixes where each round ends.  Overlapping alternatives such as
    ``(a|a)+`` are not detected.
    """
    # The regex parser of the stdlib, so the check sees what `re` compiles.
    from re import _constants as sre  # type: ignore[attr-defined]
    from re import _parser  # type: ignore[attr-defined]

    def width(item: tuple[Any, Any], body: Any) -> int:
        return int(_parser.SubPattern(body.state, [item]).getwidth()[0])

    def flat(body: Any) -> Iterator[tuple[Any, Any]]:
        for op, av in body:
            if op is sre.SUBPATTERN:
                yield from flat(av[3])
            else:
                yield op, av

    def ambiguous(body: Any) -> bool:
        items = list(flat(body))
        for i, (op, av) in enumerate(items):
            if any(width(other, body) for j, other in enumerate(items) if j != i):
                continue
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT) and av[1] == sre.MAXREPEAT:
                return True
            if op is sre.BRANCH and any(ambiguous(branch) for branch in av[1]):
                return True
        return False

    def walk(body: Any) -> bool:
        for op, av in body:
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
                if av[1] == sre.MAXREPEAT and ambiguous(av[2]):
                    return True
                children = [av[2]]
            elif op is sre.POSSESSIVE_REPEAT:
                # It gives nothing back once done, but what it repeats can
                # still backtrack while it runs: (?:(a+)+b)++.
                children = [av[2]]
            elif op is sre.SUBPATTERN:
                children = [av[3]]
            elif op is sre.ATOMIC_GROUP:
                children = [av]  # same: (?>(a+)+b)
            elif op is sre.BRANCH:
                children = av[1]
            elif op in (sre.ASSERT, sre.ASSERT_NOT):
                children = [av[1]]
            elif op is sre.GROUPREF_EXISTS:
                children = [branch for branch in av[1:] if branch is not None]
            else:
                continue
            if any(walk(child) for child in children):
                return True
        return False

    return walk(_parser.parse(pattern))


#: The ``patterns``-over-``terms`` warnings given in this process.
_OVERRIDES_WARNED: set[tuple[str, int]] = set()


def _patterns_from(config: dict[str, Any]) -> dict[str, str]:
    """Collect ``terms`` and ``patterns`` into a single entity -> regex map.

    With ``merge_terms`` (the default), terms are tried before a hand-written
    ``patterns`` regex for the same entity; without it, the regex wins.
    Entities that appear in only one of the two are all kept. The terms a regex replaces are
    named in a warning, by count: one regex added to complete them, such as
    from an ``include:``, would otherwise drop them without a word.
    """
    patterns = config.get("patterns", {})
    if not isinstance(patterns, dict):
        raise ConfigError("detector 'patterns' must be a mapping of entity -> regex")
    compiled = {str(k): str(v) for k, v in patterns.items()}
    terms = _terms_to_patterns(
        config.get("terms", {}),
        bool(config.get("term_variants")),
        underscore=bool(config.get("underscore_separates_words")),
    )
    listed = {str(entity): values for entity, values in config["terms"].items()} if terms else {}
    for entity in sorted(terms.keys() & compiled.keys()):
        if config.get("merge_terms"):
            compiled[entity] = f"(?:{terms[entity]})|{_scoped_pattern(entity, compiled[entity])}"
            continue
        warning = (entity, len(listed[entity]))
        if warning not in _OVERRIDES_WARNED:  # once, not per detector built
            _OVERRIDES_WARNED.add(warning)
            _log.warning(
                "detector: the patterns regex for %r replaces its %d terms, which are no "
                "longer matched; set detector.merge_terms: true or give the regex "
                "an entity name of its own to keep both",
                *warning,
            )
    return {**terms, **compiled}


def _scoped_pattern(entity: str, pattern: str) -> str:
    """Scope global flags before placing a user's regex in an alternation."""
    try:
        flags = re.compile(pattern).flags
    except re.error as exc:
        raise ConfigError(f"invalid regex for entity {entity!r}: {exc}") from exc
    # Global flag groups may follow comments, or whitespace in verbose mode.
    # Remove only those groups; preserve comments and escaped characters.
    preamble = r"\(\?[aiLmsux]+\)|\(\?#(?:\\.|[^\\)])*\)"
    if flags & re.X:
        preamble += r"|\s+|\#[^\n]*(?:\n|$)"
    kept = ""
    while match := re.match(preamble, pattern):
        token = match[0]
        if not re.fullmatch(r"\(\?[aiLmsux]+\)", token):
            kept += token
        pattern = pattern[match.end() :]
    names = "".join(
        name
        for flag, name in ((re.A, "a"), (re.I, "i"), (re.M, "m"), (re.S, "s"), (re.X, "x"))
        if flags & flag
    )
    # End a verbose trailing comment before closing the scoped group.
    end = "\n" if flags & re.X else ""
    return f"(?{names}:{kept}{pattern}{end})" if names else f"(?:{kept}{pattern})"


def _terms_to_patterns(
    terms: Any, variants: bool = False, *, underscore: bool = False
) -> dict[str, str]:
    """Compile literal term lists into one case-insensitive regex per entity.

    Matching is case-insensitive via an inline ``(?i:...)`` group rather than a
    compile flag, so the detectors stay untouched and a hand-written ``patterns``
    regex keeps its own (case-sensitive) semantics.

    Terms are ordered longest-first so a compound term wins over a substring of
    itself, and each one gets word boundaries only on the sides where it ends in
    a word character — that keeps ``ann`` out of ``annual`` while still matching
    a URL that ends in ``/``.  With ``variants``, a term of several words also
    matches its other spellings (:func:`_variants`).  With ``underscore``, ``_``
    separates words (:func:`_bounded`).
    """
    if not isinstance(terms, dict):
        raise ConfigError("detector 'terms' must be a mapping of entity -> list of strings")

    patterns: dict[str, str] = {}
    for entity, values in terms.items():
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise ConfigError(
                f"detector 'terms' for entity {str(entity)!r} must be a list of strings, "
                f"got {type(values).__name__}"
            )
        cleaned = {str(v).strip() for v in values}
        if variants:
            cleaned = {variant for term in cleaned for variant in _variants(term)}
        cleaned.discard("")
        if not cleaned:
            # An empty alternation would match the empty string everywhere.
            continue
        # Longest first; the alphabetical tie-break keeps the pattern stable
        # across runs, since `cleaned` is a set.
        ordered = sorted(cleaned, key=lambda term: (-len(term), term))
        alternatives = [_bounded(term, underscore=underscore) for term in ordered]
        patterns[str(entity)] = "(?i:" + "|".join(alternatives) + ")"
    return patterns


#: A term written as words split by spaces, dots, dashes, or underscores.
_WORDS = re.compile(r"[^\W_]+(?:[ ._-]+[^\W_]+)*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _variants(term: str) -> set[str]:
    """``term`` and its other spellings: ``Acme Corp`` also as ``acmecorp``,
    ``acme-corp``, ``acme_corp``, and ``acme.corp``.

    Words are split at separators and at case changes (``AcmeCorp``) when each
    part keeps two characters or more.  Matching ignores case, so this covers
    ``ACME_CORP`` and ``acmeCorp`` too.  A term with other characters (a URL,
    an email) is kept as written.
    """
    if not _WORDS.fullmatch(term):
        return {term}
    words: list[str] = []
    for word in re.split(r"[ ._-]+", term):
        parts = _CAMEL.findall(word)
        split = "".join(parts) == word and all(len(part) > 1 for part in parts)
        words += parts if split else [word]
    if len(words) < 2:
        return {term}
    return {term} | {sep.join(words) for sep in ("", " ", "-", "_", ".")}


def _bounded(term: str, *, underscore: bool = False) -> str:
    """Escape ``term`` and guard the sides that would otherwise match mid-word.

    With ``underscore``, ``_`` separates words: ``john`` is found in
    ``DB_JOHN`` and ``JOHN__SMITH``, still not in ``johnson``.
    """
    if underscore:
        prefix = r"(?<![^\W_])" if term[0].isalnum() else ""
        suffix = r"(?![^\W_])" if term[-1].isalnum() else ""
    else:
        prefix = r"(?<!\w)" if _is_word_char(term[0]) else ""
        suffix = r"(?!\w)" if _is_word_char(term[-1]) else ""
    return f"{prefix}{re.escape(term)}{suffix}"


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"
