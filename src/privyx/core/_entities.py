"""The entities Privyx's own pattern detector finds, by name.

Kept here so a component of another kind, such as the ``strict`` policy, can
name them without importing the detectors.
"""

#: Every entity the built-in patterns find: by default, or once ``enable`` lists it.
BUILTIN_ENTITIES = frozenset(
    {
        "EMAIL",
        "PHONE",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "SSN",
        "API_KEY",
        "JWT",
        "PRIVATE_KEY",
        "AUTH_TOKEN",
        "URL_CREDENTIAL",
        "SECRET",
        "CRYPTO_WALLET",
        "ID_NIK",
        "ID_NPWP",
        "SG_NRIC_FIN",
        "MY_NRIC",
        "ES_NIF",
        "ES_NIE",
        "UK_NHS",
        "IN_AADHAAR",
        "IBAN",
        "HIGH_ENTROPY",
        "ASSIGNED_SECRET",
        "MRN",
        "ICD10",
        "WALLET_KEY",
        "PASSPORT",
        "MAC_ADDRESS",
        "CN_RESIDENT_ID",
        "CN_MOBILE",
    }
)

#: Built-in entities that are credentials, not personal data.
SECRET_TYPES = ("API_KEY", "PRIVATE_KEY", "JWT", "AUTH_TOKEN", "URL_CREDENTIAL", "SECRET")
