"""Regex / keyword rule tables for the sensitivity baseline.

Three kinds of signal:

  NAME_RULES     - patterns on the (normalized) column name. Strongest signal.
  VALUE_RULES    - patterns on sample values. Confirm/raise confidence, and
                   catch generically-named columns ("data", "col1", "body").
  HARMLESS_RULES - names that are clearly not personal data. They *cap* the
                   score low, but only when no strong positive rule fired
                   (so `date_of_birth` stays sensitive despite looking like a
                   timestamp).

Names are normalized by lower-casing and turning every run of non-alphanumerics
into a single space, so ``last_login_ip`` -> ``"last login ip"`` and ``\bip\b``
matches (underscores are word characters, plain \b would not).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


@dataclass(frozen=True)
class NameRule:
    id: str
    pattern: str
    pii_type: str
    weight: float          # target sensitivity score if this rule matches
    rx: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "rx", re.compile(self.pattern))

    def matches(self, normalized_name: str) -> bool:
        return self.rx.search(normalized_name) is not None


@dataclass(frozen=True)
class ValueRule:
    id: str
    pattern: str
    pii_type: str | None
    weight: float
    mode: str = "fullmatch"   # "fullmatch" | "search"
    rx: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "rx", re.compile(self.pattern, re.I))

    def hit(self, value: str) -> bool:
        if self.mode == "fullmatch":
            return self.rx.fullmatch(value) is not None
        return self.rx.search(value) is not None


@dataclass(frozen=True)
class HarmlessRule:
    id: str
    pattern: str
    rx: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "rx", re.compile(self.pattern))

    def matches(self, normalized_name: str) -> bool:
        return self.rx.search(normalized_name) is not None


# --------------------------------------------------------------------------- #
# Column-name rules (order matters only for tie-breaking the reported pii_type;
# the score is the max weight over all matches)
# --------------------------------------------------------------------------- #

NAME_RULES: list[NameRule] = [
    NameRule("govt_id_ssn", r"\b(ssn|social security( (no|number))?|sin|nino|national insurance)\b", "government_id", 0.95),
    NameRule("govt_id_other", r"\b(passport( no| number)?|aadhaar|aadhar|\bpan\b|tax(payer)? id|\btin\b|driver'?s? licen[cs]e|voter id|national id)\b", "government_id", 0.9),
    NameRule("knowledge_based", r"\b(mother'?s? maiden|maiden name|security question|security answer|secret question|challenge answer)\b", "knowledge_based", 0.9),
    NameRule("credential_password", r"\b(password|passwd|pwd|passphrase)\b", "credential", 0.88),
    NameRule("credential_secret", r"\b(secret|api key|private key|access key|client secret|auth token|refresh token|bearer)\b", "credential", 0.82),
    NameRule("credential_weak", r"\b(otp|totp|mfa|2fa|pin( code)?|cvv|cvc|session token)\b", "credential", 0.6),
    NameRule("contact_email", r"\b(e ?mail( address)?)\b", "contact", 0.9),
    NameRule("contact_phone", r"\b(phone( number)?|telephone|mobile( number)?|msisdn|cell( phone)?|fax|whatsapp( number)?)\b", "contact", 0.85),
    NameRule("financial_account", r"\b(card number|card no|cardnum|cc num(ber)?|primary account number|\bpan\b|iban|bank account|account number|acct( no| number)|routing( number)?|sort code)\b", "financial", 0.88),
    NameRule("financial_meta", r"\b(card( |_)?last ?4|last ?4 digits|card bin|card exp\w*|expiry date)\b", "financial", 0.5),
    NameRule("health", r"\b(health|medical|diagnos\w+|disease|condition|medication|prescription|blood type|allerg\w+|disabilit\w+|insurance (id|member|number))\b", "health", 0.9),
    NameRule("special_category", r"\b(race|ethnic\w*|religio\w+|sexual orientation|gender identity|political( affiliation| opinion)?|trade union|biometric|genetic|immigration status)\b", "special_category", 0.9),
    NameRule("identity_dob", r"\b(dob|date of birth|birth ?date|birthday|born( on)?|year of birth)\b", "identity", 0.8),
    NameRule("address", r"(?<!ip )(?<!mac )(?<!url )(?<!web )(?<!host )(?<!email )address(es)?\b|\b(street|home address|mailing address|postal address|billing address|shipping address|delivery address|address line|zip ?code|postal ?code|postcode|pin ?code|\baddr\b)\b", "address", 0.8),
    NameRule("address_weak", r"\b(city|county|province|district|locality)\b", "address", 0.4),
    NameRule("network_ip", r"\b(ip( address)?|ipv4|ipv6|ipaddr|last ip|client ip|remote addr|x forwarded for|source ip)\b", "network_id", 0.6),
    NameRule("network_device", r"\b(mac address|device id|device token|udid|imei|advertising id|user agent|fingerprint)\b", "network_id", 0.55),
    NameRule("geo", r"\b(latitude|longitude|geo location|gps|coordinates?|geohash)\b", "network_id", 0.6),
    NameRule("identity_name_full", r"\b(full name|first name|last name|middle name|given name|sur ?name|maiden name|legal name|f ?name|l ?name)\b", "identity", 0.6),
    NameRule("identity_name_bare", r"\b(name)\b", "identity", 0.42),
    NameRule("identity_username", r"\b(user ?name|screen ?name|nick ?name|handle|display name)\b", "identity", 0.4),
    NameRule("demographic", r"\b(gender|\bsex\b|marital status|nationality|citizenship|country of birth|mother tongue|pronouns?)\b", "demographic", 0.55),
    NameRule("freetext_body", r"\b(body|message( body| text)?|content|transcript|conversation|chat log|note body)\b", "freetext", 0.5),
    NameRule("freetext_notes", r"\b(notes?|comments?|remarks?|memo|description text|free ?text|bio|about( me)?)\b", "freetext", 0.32),
    NameRule("freetext_subject", r"\b(subject|title|headline|summary)\b", "freetext", 0.25),
    NameRule("contact_social", r"\b(twitter|facebook|instagram|linkedin|telegram|signal|social handle)\b", "contact", 0.55),
    NameRule("preference_consent", r"\b(consent|opt in|opt out|newsletter|marketing( consent)?|subscribe\w*|do not (track|sell))\b", "preference", 0.2),
    NameRule("activity", r"\b(last login|last seen|last active|login count|failed logins?)\b", "activity", 0.15),
]

# --------------------------------------------------------------------------- #
# Sample-value rules (priority order: first fullmatch wins per value)
# --------------------------------------------------------------------------- #

VALUE_RULES: list[ValueRule] = [
    ValueRule("ssn_like", r"\d{3}-\d{2}-\d{4}", "government_id", 0.92),
    ValueRule("iso_date", r"(19|20)\d\d-\d\d-\d\d", "identity", 0.35),
    ValueRule("ipv4", r"(\d{1,3}\.){3}\d{1,3}", "network_id", 0.7),
    ValueRule("ipv6", r"([0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}", "network_id", 0.7),
    ValueRule("email", r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", "contact", 0.85),
    ValueRule("credit_card", r"\d{13,19}", "financial", 0.8),
    ValueRule("uuid", r"[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}", None, 0.08),
    ValueRule("phone_like", r"\+?\d[\d\s().-]{6,17}\d", "contact", 0.7),
]

# Patterns searched *inside* longer text (free-text columns leaking PII)
EMBEDDED_VALUE_RULES: list[ValueRule] = [
    ValueRule("email_in_text", r"[^@\s]+@[^@\s]+\.[a-z]{2,}", "contact", 0.5, mode="search"),
    ValueRule("ssn_in_text", r"\b\d{3}-\d{2}-\d{4}\b", "government_id", 0.55, mode="search"),
    ValueRule("phone_in_text", r"(?<!\d)\+?\d[\d\s().-]{8,}\d(?!\d)", "contact", 0.35, mode="search"),
]

# --------------------------------------------------------------------------- #
# Harmless-name rules
# --------------------------------------------------------------------------- #

HARMLESS_RULES: list[HarmlessRule] = [
    HarmlessRule("color", r"\b(colou?r|favou?rite colou?r)\b"),
    HarmlessRule("ui_pref", r"\b(theme|dark mode|light mode|font size|layout|density|ui setting)\b"),
    HarmlessRule("locale", r"\b(locale|language|lang|timezone|time zone|\btz\b|currency|country code|units?)\b"),
    HarmlessRule("boolean_flag", r"\b(is [a-z]+|has [a-z]+|enabled|disabled|active|inactive|archived|deleted|verified|flag)\b"),
    HarmlessRule("status_enum", r"\b(status|state|stage|phase|kind|type|category|tier|level|priority|severity|mode|reason)\b"),
    HarmlessRule("timestamp", r"\b(created|updated|modified|inserted|deleted|resolved|closed|expired|processed|synced|timestamp)\b"),
    HarmlessRule("counter_money", r"\b(count|total|subtotal|sum|amount|qty|quantity|balance|price|cost|fee|discount|rate|ratio|percent\w*|score|points?|stars?|rating|nps|csat|sentiment)\b"),
    HarmlessRule("identifier", r"\b(uuid|guid|slug|token id|ref|reference|\bcode\b|checksum|\bhash\b|etag|version|revision|\bseq\b|sequence|index|ordinal|position|\brank\b|sort order)\b"),
    HarmlessRule("system_meta", r"\b(file name|table name|column name|field name|host name|class name|event name|action name|product name|brand|make|model|sku|mime type|extension|url path|endpoint)\b"),
]

# --------------------------------------------------------------------------- #
# Type / structural signals
# --------------------------------------------------------------------------- #

# generic_type -> (pii_type, weight). Applied as a weak independent signal.
TYPE_RULES: dict[str, tuple[str, float]] = {
    "inet": ("network_id", 0.6),
}
