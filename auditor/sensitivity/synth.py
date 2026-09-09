"""Synthetic column corpus for training the ML sensitivity classifier.

46 seed columns is far too few to train and hold out a test set. This module
generates a few hundred plausible columns - varied names, types, and sample
values across the sensitivity spectrum - which are then labelled by the
RegexSensitivityClassifier (see ml_classifier.train_model).

The seed's 46 columns are deliberately NOT part of this corpus, so they can
serve as an independent test set (scored against the hand-authored
column_catalog.csv, not against regex output).
"""

from __future__ import annotations

import random
import string
from typing import Callable

from auditor.ingestion.metadata import ColumnMetadata

_FIRST = "james mary john patricia robert jennifer michael linda david elizabeth".split()
_LAST = "smith johnson williams brown jones garcia miller davis rodriguez martinez".split()
_STREET = "oak maple cedar pine elm washington lake hill park river".split()
_ST = ["St", "Ave", "Rd", "Blvd", "Ln", "Dr"]
_CITY = "springfield franklin clinton madison georgetown salem fairview".split()
_DOMAIN = "gmail.com yahoo.com outlook.com proton.me company.co example.org".split()
_WORDS = ("the quick brown fox jumps over lazy dog customer order issue refund "
         "please contact regarding account balance shipment delayed thank you "
         "resolved escalated priority follow up call back").split()
_ENUM = {
    "status": ["active", "pending", "closed", "cancelled", "shipped", "delivered"],
    "priority": ["low", "medium", "high", "urgent"],
    "type": ["standard", "premium", "trial", "internal"],
    "color": ["blue", "green", "red", "purple", "teal", "orange", "black"],
    "theme": ["light", "dark", "system"],
    "locale": ["en-US", "en-GB", "de-DE", "fr-FR", "pt-BR", "hi-IN"],
    "gender": ["female", "male", "non-binary", "prefer not to say"],
    "blood": ["A+", "O-", "B+", "AB+", "O+"],
    "card_brand": ["visa", "mastercard", "amex", "discover"],
}


def _digits(rng, n):
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _hex(rng, n):
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _para(rng):
    k = rng.randint(6, 22)
    s = " ".join(rng.choice(_WORDS) for _ in range(k)).capitalize() + "."
    if rng.random() < 0.15:  # PII leaking into free text
        s += f" reach me at {rng.choice(_FIRST)}.{rng.choice(_LAST)}@{rng.choice(_DOMAIN)}"
    return s


def _date(rng):
    return f"{rng.randint(1945, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def _ts(rng):
    return _date(rng) + f"T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}+00:00"


# family: (name choices, generic_type, raw_type, value fn)
_Family = tuple[list[str], str, str, Callable]

_FAMILIES: list[_Family] = [
    # --- sensitive ------------------------------------------------------
    (["email", "email_address", "contact_email", "work_email", "recovery_email"],
     "text", "TEXT", lambda r: f"{r.choice(_FIRST)}.{r.choice(_LAST)}{r.randint(1,99)}@{r.choice(_DOMAIN)}"),
    (["phone", "mobile", "phone_number", "cell_phone", "fax_number", "contact_number"],
     "text", "TEXT", lambda r: _digits(r, r.choice([10, 11, 12]))),
    (["ssn", "social_security_number", "national_id", "tax_id", "aadhaar_number"],
     "text", "TEXT", lambda r: f"{r.randint(100,899)}-{r.randint(10,99)}-{_digits(r,4)}"),
    (["full_name", "first_name", "last_name", "customer_name", "legal_name", "contact_name"],
     "text", "TEXT", lambda r: f"{r.choice(_FIRST).title()} {r.choice(_LAST).title()}"),
    (["address", "home_address", "street_address", "mailing_address", "shipping_address", "billing_address"],
     "text", "TEXT", lambda r: f"{r.randint(1,9999)} {r.choice(_STREET).title()} {r.choice(_ST)}, {r.choice(_CITY).title()}"),
    (["date_of_birth", "dob", "birth_date", "birthdate"],
     "date", "DATE", lambda r: f"{r.randint(1945,2007)}-{r.randint(1,12):02d}-{r.randint(1,28):02d}"),
    (["password", "password_hash", "passwd", "user_secret"],
     "text", "TEXT", lambda r: "$2b$12$" + _hex(r, 50)),
    (["api_key", "secret_key", "access_token", "refresh_token", "private_key"],
     "text", "TEXT", lambda r: _hex(r, r.choice([32, 40, 64]))),
    (["credit_card_number", "card_number", "cc_number", "iban", "bank_account_number"],
     "text", "TEXT", lambda r: _digits(r, 16)),
    (["card_last4", "card_last_four"], "text", "TEXT", lambda r: _digits(r, 4)),
    (["ip_address", "last_login_ip", "client_ip", "remote_addr", "source_ip"],
     "inet", "INET", lambda r: ".".join(str(r.randint(1, 254)) for _ in range(4))),
    (["diagnosis", "medical_history", "medication", "health_condition", "prescription"],
     "text", "TEXT", _para),
    (["blood_type"], "text", "TEXT", lambda r: r.choice(_ENUM["blood"])),
    (["gender", "sex", "ethnicity", "religion", "nationality", "marital_status"],
     "text", "TEXT", lambda r: r.choice(_ENUM["gender"])),
    (["mothers_maiden_name", "security_question", "security_answer", "challenge_answer"],
     "text", "TEXT", lambda r: r.choice(_LAST).title()),
    (["latitude", "longitude", "geo_lat", "geo_lng"], "float", "DOUBLE PRECISION",
     lambda r: f"{r.uniform(-90, 90):.5f}"),
    # --- ambiguous / borderline ------------------------------------
    (["notes", "comment", "comments", "description", "body", "message", "bio", "remarks"],
     "text", "TEXT", _para),
    (["subject", "title", "headline", "summary"], "text", "TEXT",
     lambda r: " ".join(r.choice(_WORDS) for _ in range(r.randint(3, 7))).capitalize()),
    (["data", "value", "payload", "content", "raw"], "text", "TEXT", _para),
    (["metadata", "attributes", "properties", "config"], "json", "JSONB",
     lambda r: '{"k": "' + r.choice(_WORDS) + '"}'),
    (["assigned_agent", "reviewer", "created_by", "owner", "manager"],
     "text", "TEXT", lambda r: f"agent_{r.randint(1,40):02d}"),
    (["marketing_consent", "opt_in", "newsletter_subscribed", "gdpr_consent"],
     "boolean", "BOOLEAN", lambda r: r.choice(["true", "false"])),
    # --- harmless ---------------------------------------------------
    (["created_at", "updated_at", "deleted_at", "last_synced_at", "processed_at", "resolved_at"],
     "timestamptz", "TIMESTAMP WITH TIME ZONE", _ts),
    (["id", "user_id", "order_id", "parent_id", "row_id"], "integer", "BIGINT",
     lambda r: str(r.randint(1, 10 ** 6))),
    (["uuid", "session_id", "request_id", "trace_id", "external_id"], "uuid", "UUID",
     lambda r: f"{_hex(r,8)}-{_hex(r,4)}-{_hex(r,4)}-{_hex(r,4)}-{_hex(r,12)}"),
    (["status", "state", "stage", "phase"], "text", "TEXT", lambda r: r.choice(_ENUM["status"])),
    (["type", "kind", "category", "tier"], "text", "TEXT", lambda r: r.choice(_ENUM["type"])),
    (["priority", "severity", "level"], "text", "TEXT", lambda r: r.choice(_ENUM["priority"])),
    (["is_active", "is_deleted", "has_paid", "enabled", "archived", "is_verified"],
     "boolean", "BOOLEAN", lambda r: r.choice(["true", "false"])),
    (["favorite_color", "favourite_colour", "accent_color"], "text", "TEXT",
     lambda r: r.choice(_ENUM["color"])),
    (["theme", "theme_preference", "display_mode", "ui_density"], "text", "TEXT",
     lambda r: r.choice(_ENUM["theme"])),
    (["locale", "language", "timezone", "currency", "country_code"], "text", "TEXT",
     lambda r: r.choice(_ENUM["locale"])),
    (["count", "total", "quantity", "num_items", "retry_count", "view_count"],
     "integer", "INTEGER", lambda r: str(r.randint(0, 5000))),
    (["price", "amount", "total_amount", "balance", "cost", "fee", "subtotal"],
     "numeric", "NUMERIC(10,2)", lambda r: f"{r.uniform(1, 5000):.2f}"),
    (["score", "rating", "satisfaction_rating", "nps", "confidence"],
     "float", "REAL", lambda r: f"{r.uniform(0, 5):.1f}"),
    (["card_brand", "payment_method", "carrier"], "text", "TEXT",
     lambda r: r.choice(_ENUM["card_brand"])),
    (["file_name", "mime_type", "file_extension", "content_type"], "text", "TEXT",
     lambda r: r.choice(_WORDS) + r.choice([".pdf", ".png", ".csv", ".json"])),
    (["url", "endpoint", "path", "slug", "referrer"], "text", "TEXT",
     lambda r: "/" + "/".join(r.choice(_WORDS) for _ in range(r.randint(1, 3)))),
    (["version", "revision", "schema_version", "build"], "text", "TEXT",
     lambda r: f"{r.randint(1,9)}.{r.randint(0,20)}.{r.randint(0,50)}"),
    (["hash", "checksum", "etag", "digest"], "text", "TEXT", lambda r: _hex(r, 32)),
    (["currency_code"], "text", "TEXT", lambda r: r.choice(["USD", "EUR", "GBP", "INR"])),
    (["sentiment", "customer_sentiment", "mood"], "text", "TEXT",
     lambda r: r.choice(["positive", "neutral", "negative"])),
]

_PREFIX = ["", "", "", "user_", "customer_", "account_", "app_", "tmp_", "legacy_", "src_"]
_SUFFIX = ["", "", "", "", "_v2", "_bak", "_old", "_raw", "_txt"]


def generate_synthetic_columns(n: int = 800, *, seed: int = 42) -> list[ColumnMetadata]:
    rng = random.Random(seed)
    out: list[ColumnMetadata] = []
    for i in range(n):
        names, gtype, raw, vfn = rng.choice(_FAMILIES)
        base = rng.choice(names)
        name = rng.choice(_PREFIX) + base + rng.choice(_SUFFIX)

        n_samples = rng.choice([0, 5, 12, 12, 20, 25])
        null_rate = rng.choice([0.0, 0.0, 0.05, 0.2, 0.5]) if n_samples else 0.0
        vals = []
        for _ in range(n_samples):
            if rng.random() < null_rate:
                continue
            try:
                vals.append(str(vfn(rng)))
            except Exception:  # noqa: BLE001
                vals.append("x")
        distinct = len(set(vals))

        out.append(ColumnMetadata(
            table=f"t{i % 30}", name=name, ordinal=i,
            generic_type=gtype, raw_type=raw,
            nullable=rng.random() < 0.8,
            is_primary_key=(base in ("id",) and rng.random() < 0.3),
            is_foreign_key=(base.endswith("_id") and base != "id" and rng.random() < 0.6),
            is_unique=rng.random() < 0.1,
            is_indexed=rng.random() < 0.2,
            char_max_length=(rng.choice([None, 64, 255]) if gtype in ("string", "text") else None),
            is_temporal=gtype in ("date", "timestamp", "timestamptz", "time"),
            sample_values=vals,
            sample_size=n_samples,
            sample_null_fraction=round(1 - len(vals) / n_samples, 3) if n_samples else None,
            distinct_in_sample=distinct,
        ))
    return out
