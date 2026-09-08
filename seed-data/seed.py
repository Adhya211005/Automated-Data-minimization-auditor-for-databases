#!/usr/bin/env python3
"""
Phase 0 - Seed data generator for the Automated Data Minimization Auditor.

What this script produces, in one run:

  1. A realistic PostgreSQL schema for a small e-commerce / SaaS app
     (users, orders, support_tickets) containing:
       - obviously sensitive columns  (email, phone, ssn, mothers_maiden_name,
         password_hash, card_last4, shipping/billing address)
       - borderline / context-dependent columns (last_login_ip, ip_address,
         date_of_birth, gender, ticket body free-text)
       - clearly harmless columns (favorite_color, theme_preference, locale,
         customer_sentiment, satisfaction_rating)

  2. Synthetic rows generated with Faker, with realistic created/updated
     timestamps spread over ~2 years. A deliberate slice of rows is older
     than 400 days so the Phase-3 retention checker has stale data to find.

  3. A synthetic application query log (last 90 days) written to
     seed-data/output/ as both JSONL and CSV. Access patterns are uneven
     on purpose:
       - some columns are read on almost every request (email, theme_preference)
       - some are read rarely (ssn, billing_address, gender)
       - some are NEVER read by any query (mothers_maiden_name, favorite_color,
         orders.notes, satisfaction_rating)
     That gap is the signal the Phase-2 Usage Analyzer is meant to detect.

  4. seed-data/output/column_catalog.csv - a ground-truth catalogue of every
     column (intended sensitivity tier + expected usage + whether it was
     seeded with stale rows). Later phases can score their classifier /
     analyzer against this.

This script only WRITES to its own throwaway database (default: dma_auditor).
It never touches any other database. The auditor itself (later phases) must
connect read-only per CLAUDE.md - that is a separate concern from this seeder.

Usage:
    python seed.py                     # create schema + data + logs
    python seed.py --reset             # drop & recreate everything first
    python seed.py --logs-only         # regenerate query log without touching DB
    python seed.py --users 2000        # bigger dataset
    python seed.py --help

Connection settings are read from seed-data/.env (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import psycopg
except ImportError:
    sys.exit("Missing dependency 'psycopg'. Run:  pip install -r seed-data/requirements.txt")

try:
    from faker import Faker
except ImportError:
    sys.exit("Missing dependency 'Faker'. Run:  pip install -r seed-data/requirements.txt")


HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output"
NOW = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# 0. Tiny .env loader (avoids a python-dotenv dependency)
# ---------------------------------------------------------------------------

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def db_settings() -> dict:
    return dict(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "dma_seed"),
        password=os.environ.get("DB_PASSWORD", "dma_seed_local_pw"),
        dbname=os.environ.get("DB_NAME", "dma_auditor"),
    )


# ---------------------------------------------------------------------------
# 1. Schema definition
# ---------------------------------------------------------------------------
# One CREATE statement per table. Column choices are documented in
# COLUMN_CATALOG below (that list is the ground truth later phases score
# themselves against, so keep the two in sync).

DDL = {
    "users": """
        CREATE TABLE users (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            created_at          TIMESTAMPTZ  NOT NULL,
            updated_at          TIMESTAMPTZ  NOT NULL,
            last_login_at       TIMESTAMPTZ,
            email               TEXT         NOT NULL UNIQUE,
            password_hash       TEXT         NOT NULL,
            full_name           TEXT         NOT NULL,
            phone               TEXT,
            ssn                 TEXT,
            mothers_maiden_name TEXT,
            date_of_birth       DATE,
            gender              TEXT,
            last_login_ip       INET,
            marketing_consent   BOOLEAN      NOT NULL DEFAULT FALSE,
            favorite_color      TEXT,
            theme_preference    TEXT         NOT NULL DEFAULT 'system',
            locale              TEXT         NOT NULL DEFAULT 'en-US',
            is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
            account_status      TEXT         NOT NULL DEFAULT 'active'
        )
    """,
    "orders": """
        CREATE TABLE orders (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id          BIGINT       NOT NULL REFERENCES users(id),
            created_at       TIMESTAMPTZ  NOT NULL,
            updated_at       TIMESTAMPTZ  NOT NULL,
            status           TEXT         NOT NULL,
            total_amount     NUMERIC(10,2) NOT NULL,
            currency         TEXT         NOT NULL DEFAULT 'USD',
            shipping_address TEXT,
            billing_address  TEXT,
            card_last4       TEXT,
            card_brand       TEXT,
            ip_address       INET,
            promo_code       TEXT,
            notes            TEXT
        )
    """,
    "support_tickets": """
        CREATE TABLE support_tickets (
            id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id            BIGINT       NOT NULL REFERENCES users(id),
            created_at         TIMESTAMPTZ  NOT NULL,
            updated_at         TIMESTAMPTZ  NOT NULL,
            resolved_at        TIMESTAMPTZ,
            subject            TEXT         NOT NULL,
            body               TEXT         NOT NULL,
            category           TEXT         NOT NULL,
            priority           TEXT         NOT NULL,
            status             TEXT         NOT NULL,
            assigned_agent     TEXT,
            customer_sentiment TEXT,
            satisfaction_rating INT
        )
    """,
}

TABLES = list(DDL.keys())

# Ground-truth catalogue: (table, column, sensitivity_tier, sensitivity_score,
# pii_type, expected_usage, seeded_stale, rationale)
# sensitivity_tier / expected_usage are the labels; sensitivity_score is a
# rough 0-1 hint for the eventual Necessity Score = Sens x (1-Usage) x Staleness.
COLUMN_CATALOG = [
    # table            column                 tier      score pii_type          usage      stale  rationale
    ("users", "id",                   "none",   0.00, "",                "high",    False, "surrogate key"),
    ("users", "created_at",           "none",   0.00, "",                "high",    False, "used for sorting / retention math"),
    ("users", "updated_at",           "none",   0.00, "",                "medium",  False, "audit timestamp"),
    ("users", "last_login_at",        "low",    0.15, "activity",        "medium",  True,  "some accounts idle 400+ days"),
    ("users", "email",                "high",   0.90, "contact",         "high",    False, "login + notifications, always used"),
    ("users", "password_hash",        "high",   0.85, "credential",      "high",    False, "sensitive but necessary - used at login"),
    ("users", "full_name",            "medium", 0.55, "identity",        "high",    False, "shown throughout the app"),
    ("users", "phone",                "high",   0.80, "contact",         "low",     False, "collected at signup, rarely read"),
    ("users", "ssn",                  "high",   0.95, "government_id",    "rare",    True,  "looks necessary, barely queried, old rows"),
    ("users", "mothers_maiden_name",  "high",   0.90, "knowledge_based", "never",   True,  "textbook violation: sensitive, unused, stale"),
    ("users", "date_of_birth",        "high",   0.75, "identity",        "rare",    False, "used only for age verification"),
    ("users", "gender",               "medium", 0.50, "demographic",     "rare",    False, "quarterly demographics report only"),
    ("users", "last_login_ip",        "medium", 0.60, "network_id",      "medium",  False, "legally PII though it looks harmless; security uses it"),
    ("users", "marketing_consent",    "low",    0.20, "preference",      "low",     False, "read by marketing segment export"),
    ("users", "favorite_color",       "none",   0.05, "preference",      "never",   True,  "harmless AND unused - must NOT be flagged"),
    ("users", "theme_preference",     "none",   0.05, "preference",      "high",    False, "read on every app bootstrap"),
    ("users", "locale",               "none",   0.05, "preference",      "high",    False, "read on every app bootstrap"),
    ("users", "is_active",            "none",   0.00, "",                "high",    False, "auth check"),
    ("users", "account_status",       "low",    0.10, "",                "low",     False, "admin / support"),

    ("orders", "id",               "none",   0.00, "",            "high",   False, "surrogate key"),
    ("orders", "user_id",          "low",    0.10, "linkage",     "high",   False, "join key"),
    ("orders", "created_at",       "none",   0.00, "",            "high",   False, "sorting / retention"),
    ("orders", "updated_at",       "none",   0.00, "",            "medium", False, "audit timestamp"),
    ("orders", "status",           "none",   0.00, "",            "high",   False, "core order field"),
    ("orders", "total_amount",     "low",    0.15, "financial",   "high",   False, "core order field"),
    ("orders", "currency",         "none",   0.00, "",            "high",   False, "core order field"),
    ("orders", "shipping_address", "high",   0.80, "address",     "medium", False, "needed to fulfil orders"),
    ("orders", "billing_address",  "high",   0.80, "address",     "rare",   True,  "rarely read after checkout; old rows"),
    ("orders", "card_last4",       "medium", 0.65, "financial",   "medium", False, "shown on receipts"),
    ("orders", "card_brand",       "low",    0.25, "financial",   "medium", False, "shown on receipts"),
    ("orders", "ip_address",       "medium", 0.60, "network_id",  "low",    False, "fraud checks only"),
    ("orders", "promo_code",       "none",   0.05, "",            "low",    False, "written at checkout, seldom read"),
    ("orders", "notes",            "low",    0.20, "freetext",    "never",  True,  "internal notes, never read back by app"),

    ("support_tickets", "id",                  "none",   0.00, "",           "high",   False, "surrogate key"),
    ("support_tickets", "user_id",             "low",    0.10, "linkage",    "high",   False, "join key"),
    ("support_tickets", "created_at",          "none",   0.00, "",           "high",   False, "sorting / retention"),
    ("support_tickets", "updated_at",          "none",   0.00, "",           "medium", False, "audit timestamp"),
    ("support_tickets", "resolved_at",         "none",   0.00, "",           "low",    False, "SLA reporting"),
    ("support_tickets", "subject",             "low",    0.25, "freetext",   "high",   False, "shown in ticket list"),
    ("support_tickets", "body",                "medium", 0.55, "freetext",   "medium", True,  "free text may contain PII; old tickets never purged"),
    ("support_tickets", "category",            "none",   0.00, "",           "medium", False, "routing"),
    ("support_tickets", "priority",            "none",   0.00, "",           "high",   False, "routing"),
    ("support_tickets", "status",              "none",   0.00, "",           "high",   False, "routing"),
    ("support_tickets", "assigned_agent",      "low",    0.15, "employee",   "medium", False, "internal assignment"),
    ("support_tickets", "customer_sentiment",  "none",   0.05, "",           "never",  True,  "captured by an old experiment, never read"),
    ("support_tickets", "satisfaction_rating", "none",   0.05, "",           "never",  True,  "CSAT survey column, never read back"),
]


# ---------------------------------------------------------------------------
# 2. Query-log templates
# ---------------------------------------------------------------------------
# Each template is a stylised application query. `weight` is its relative
# frequency; the generator normalises weights to the requested total.
# `recency` shapes WHEN in the 90-day window the query fires.
#
# Columns are written as "table.column" so the Usage Analyzer can attribute
# reads/writes per column directly. Any column NOT appearing in ANY template
# has zero observed usage - that is the intended blind spot.

QUERY_TEMPLATES = [
    dict(name="user_login", service="auth-service", op="SELECT", weight=4200,
         recency="recent", tables=["users"],
         reads=["users.id", "users.email", "users.password_hash", "users.is_active"],
         writes=[],
         sql="SELECT id, email, password_hash, is_active FROM users WHERE email = $1"),

    dict(name="user_login_touch", service="auth-service", op="UPDATE", weight=4000,
         recency="recent", tables=["users"],
         reads=["users.id"],
         writes=["users.last_login_at", "users.last_login_ip", "users.updated_at"],
         sql="UPDATE users SET last_login_at = now(), last_login_ip = $1, updated_at = now() WHERE id = $2"),

    dict(name="app_bootstrap_prefs", service="web-app", op="SELECT", weight=3600,
         recency="uniform", tables=["users"],
         reads=["users.id", "users.theme_preference", "users.locale", "users.is_active"],
         writes=[],
         sql="SELECT theme_preference, locale, is_active FROM users WHERE id = $1"),

    dict(name="load_user_profile", service="web-app", op="SELECT", weight=2600,
         recency="uniform", tables=["users"],
         reads=["users.id", "users.full_name", "users.email", "users.phone",
                "users.marketing_consent", "users.account_status"],
         writes=[],
         sql="SELECT full_name, email, phone, marketing_consent, account_status FROM users WHERE id = $1"),

    dict(name="list_user_orders", service="web-app", op="SELECT", weight=2400,
         recency="uniform", tables=["orders"],
         reads=["orders.id", "orders.user_id", "orders.created_at", "orders.status",
                "orders.total_amount", "orders.currency"],
         writes=[],
         sql="SELECT id, created_at, status, total_amount, currency FROM orders WHERE user_id = $1 ORDER BY created_at DESC"),

    dict(name="order_detail", service="web-app", op="SELECT", weight=1300,
         recency="uniform", tables=["orders"],
         reads=["orders.id", "orders.status", "orders.total_amount", "orders.currency",
                "orders.shipping_address", "orders.card_last4", "orders.card_brand",
                "orders.promo_code"],
         writes=[],
         sql="SELECT status, total_amount, currency, shipping_address, card_last4, card_brand, promo_code FROM orders WHERE id = $1"),

    dict(name="checkout_create_order", service="checkout-service", op="INSERT", weight=950,
         recency="growing", tables=["orders"],
         reads=["users.id"],
         writes=["orders.user_id", "orders.status", "orders.total_amount", "orders.currency",
                 "orders.shipping_address", "orders.billing_address", "orders.card_last4",
                 "orders.card_brand", "orders.ip_address", "orders.promo_code", "orders.created_at",
                 "orders.updated_at"],
         sql="INSERT INTO orders (user_id, status, total_amount, currency, shipping_address, billing_address, card_last4, card_brand, ip_address, promo_code, created_at, updated_at) VALUES (...)"),

    dict(name="support_ticket_list", service="support-console", op="SELECT", weight=720,
         recency="uniform", tables=["support_tickets"],
         reads=["support_tickets.id", "support_tickets.user_id", "support_tickets.subject",
                "support_tickets.status", "support_tickets.priority", "support_tickets.category",
                "support_tickets.created_at"],
         writes=[],
         sql="SELECT id, subject, status, priority, category, created_at FROM support_tickets WHERE status <> 'closed' ORDER BY priority"),

    dict(name="support_ticket_view", service="support-console", op="SELECT", weight=640,
         recency="uniform", tables=["support_tickets", "users"],
         reads=["support_tickets.id", "support_tickets.subject", "support_tickets.body",
                "support_tickets.category", "support_tickets.assigned_agent",
                "support_tickets.resolved_at", "support_tickets.updated_at",
                "users.full_name", "users.email"],
         writes=[],
         sql="SELECT t.subject, t.body, t.category, t.assigned_agent, u.full_name, u.email FROM support_tickets t JOIN users u ON u.id = t.user_id WHERE t.id = $1"),

    dict(name="fraud_check", service="risk-service", op="SELECT", weight=300,
         recency="uniform", tables=["orders", "users"],
         reads=["orders.id", "orders.ip_address", "orders.card_last4", "orders.total_amount",
                "users.id", "users.last_login_ip", "users.created_at"],
         writes=[],
         sql="SELECT o.ip_address, o.card_last4, u.last_login_ip FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = $1"),

    dict(name="security_login_ip_audit", service="security-batch", op="SELECT", weight=110,
         recency="weekly", tables=["users"],
         reads=["users.id", "users.last_login_ip", "users.last_login_at", "users.updated_at"],
         writes=[],
         sql="SELECT id, last_login_ip, last_login_at FROM users WHERE last_login_at > now() - interval '7 days'"),

    dict(name="marketing_segment_export", service="marketing-batch", op="SELECT", weight=70,
         recency="weekly", tables=["users"],
         reads=["users.id", "users.email", "users.marketing_consent", "users.locale", "users.created_at"],
         writes=[],
         sql="SELECT id, email, locale FROM users WHERE marketing_consent = true"),

    dict(name="age_verification", service="checkout-service", op="SELECT", weight=45,
         recency="uniform", tables=["users"],
         reads=["users.id", "users.date_of_birth"],
         writes=[],
         sql="SELECT date_of_birth FROM users WHERE id = $1"),

    dict(name="billing_dispute_lookup", service="support-console", op="SELECT", weight=14,
         recency="uniform", tables=["orders"],
         reads=["orders.id", "orders.billing_address", "orders.card_last4", "orders.card_brand",
                "orders.total_amount"],
         writes=[],
         sql="SELECT billing_address, card_last4, card_brand, total_amount FROM orders WHERE id = $1"),

    dict(name="quarterly_demographics_report", service="analytics-batch", op="SELECT", weight=9,
         recency="quarterly", tables=["users"],
         reads=["users.gender", "users.date_of_birth", "users.locale", "users.created_at"],
         writes=[],
         sql="SELECT gender, date_part('year', age(date_of_birth)) AS age, count(*) FROM users GROUP BY 1, 2"),

    dict(name="annual_ssn_compliance_export", service="compliance-batch", op="SELECT", weight=3,
         recency="single_old", tables=["users"],
         reads=["users.id", "users.ssn", "users.full_name", "users.created_at"],
         writes=[],
         sql="SELECT id, ssn, full_name FROM users WHERE created_at < now() - interval '1 year'"),
]

def db_user_for(service: str, op: str) -> str:
    """Pick a plausible DB role for a service/operation (analyzers may key on this)."""
    if service.endswith("-batch"):
        return "batch_job"
    if service == "support-console":
        return "support_app"
    if service == "risk-service":
        return "app_ro"
    return "app_rw" if op != "SELECT" else random.choice(["app_rw", "app_ro"])

HOUR_WEIGHTS = [  # UTC hour -> relative traffic (business-hours heavy, never zero)
    2, 1, 1, 1, 1, 2, 4, 7, 10, 12, 13, 13,
    12, 12, 13, 13, 12, 10, 8, 6, 5, 4, 3, 2,
]


# ---------------------------------------------------------------------------
# 3. Data generation helpers
# ---------------------------------------------------------------------------

def make_timestamp_pair(fake: Faker, max_age_days: int, stale_fraction: float,
                        stale_min_days: int = 400, stale_max_days: int = 900):
    """Return (created_at, updated_at). With probability `stale_fraction` the
    row is created between stale_min/stale_max days ago so retention logic
    has something to catch."""
    if random.random() < stale_fraction:
        age = random.uniform(stale_min_days, stale_max_days)
    else:
        age = random.uniform(1, max_age_days)
    created = NOW - timedelta(days=age, seconds=random.randint(0, 86399))
    # updated_at somewhere between created_at and now, skewed toward created_at
    span = (NOW - created).total_seconds()
    updated = created + timedelta(seconds=span * (random.random() ** 2))
    return created, updated


def gen_users(fake: Faker, n: int):
    genders = ["female", "male", "non-binary", "prefer not to say", None]
    colors = ["blue", "green", "red", "purple", "teal", "orange", "black", None]
    themes = ["system", "light", "dark"]
    locales = ["en-US", "en-GB", "en-IN", "de-DE", "fr-FR", "es-ES", "pt-BR"]
    statuses = ["active"] * 12 + ["suspended", "closed", "pending_verification"]
    rows = []
    for _ in range(n):
        created, updated = make_timestamp_pair(fake, max_age_days=700, stale_fraction=0.18)
        # last_login_at: usually recent, but a chunk of accounts have gone quiet
        if random.random() < 0.22:
            last_login = created + timedelta(days=random.uniform(0, 30))
        else:
            last_login = NOW - timedelta(days=random.uniform(0, 120),
                                         seconds=random.randint(0, 86399))
        last_login = max(min(last_login, NOW), created)
        first = fake.first_name()
        last = fake.last_name()
        rows.append((
            created, updated, last_login,
            f"{first.lower()}.{last.lower()}{random.randint(1, 999)}@{fake.free_email_domain()}",
            "bcrypt$2b$12$" + fake.sha256()[:53],           # password_hash (fake)
            f"{first} {last}",                                # full_name
            fake.msisdn()[:12],                              # phone
            f"{random.randint(100,899)}-{random.randint(10,99)}-{random.randint(1000,9999)}",  # ssn-like
            fake.last_name(),                                # mothers_maiden_name
            fake.date_of_birth(minimum_age=18, maximum_age=85),
            random.choice(genders),
            fake.ipv4_public(),                              # last_login_ip
            random.random() < 0.45,                          # marketing_consent
            random.choice(colors),                           # favorite_color
            random.choice(themes),
            random.choice(locales),
            random.random() < 0.93,                          # is_active
            random.choice(statuses),
        ))
    return rows


USER_COLS = ("created_at", "updated_at", "last_login_at", "email", "password_hash",
             "full_name", "phone", "ssn", "mothers_maiden_name", "date_of_birth",
             "gender", "last_login_ip", "marketing_consent", "favorite_color",
             "theme_preference", "locale", "is_active", "account_status")


def gen_orders(fake: Faker, user_rows_with_ids):
    """user_rows_with_ids: list of (id, created_at)."""
    order_statuses = (["delivered"] * 6 + ["shipped"] * 2 + ["processing", "cancelled", "refunded"])
    brands = ["visa", "mastercard", "amex", "discover"]
    promos = [None] * 6 + ["WELCOME10", "SUMMER20", "FREESHIP", "VIP15"]
    rows = []
    for uid, ucreated in user_rows_with_ids:
        n_orders = random.choices([0, 1, 2, 3, 5, 9], weights=[18, 30, 25, 15, 8, 4])[0]
        for _ in range(n_orders):
            # order created after the user, up to now
            span = (NOW - ucreated).total_seconds()
            if span <= 0:
                continue
            created = ucreated + timedelta(seconds=span * random.random())
            uspan = (NOW - created).total_seconds()
            updated = created + timedelta(seconds=uspan * (random.random() ** 2))
            rows.append((
                uid, created, updated,
                random.choice(order_statuses),
                round(random.uniform(5, 900), 2),
                random.choice(["USD", "USD", "USD", "EUR", "GBP", "INR"]),
                fake.street_address() + ", " + fake.city(),      # shipping_address
                fake.street_address() + ", " + fake.city(),      # billing_address
                f"{random.randint(0, 9999):04d}",                # card_last4
                random.choice(brands),
                fake.ipv4_public(),                              # ip_address
                random.choice(promos),
                fake.sentence(nb_words=8) if random.random() < 0.15 else None,  # notes
            ))
    return rows


ORDER_COLS = ("user_id", "created_at", "updated_at", "status", "total_amount", "currency",
              "shipping_address", "billing_address", "card_last4", "card_brand",
              "ip_address", "promo_code", "notes")


def gen_tickets(fake: Faker, user_rows_with_ids):
    categories = ["billing", "shipping", "account", "bug", "refund", "other"]
    priorities = ["low"] * 4 + ["medium"] * 3 + ["high", "urgent"]
    sentiments = ["positive", "neutral", "negative", None]
    agents = [f"agent_{n:02d}" for n in range(1, 13)]
    rows = []
    for uid, ucreated in user_rows_with_ids:
        n_tickets = random.choices([0, 1, 2, 4], weights=[62, 25, 9, 4])[0]
        for _ in range(n_tickets):
            span = (NOW - ucreated).total_seconds()
            if span <= 0:
                continue
            # tickets skew old: support data is notoriously never purged
            created = ucreated + timedelta(seconds=span * (random.random() ** 1.6))
            resolved = None
            status = random.choices(["closed", "resolved", "open", "pending"],
                                    weights=[55, 20, 15, 10])[0]
            uspan = (NOW - created).total_seconds()
            updated = created + timedelta(seconds=uspan * (random.random() ** 2))
            if status in ("closed", "resolved") and uspan > 0:
                resolved = created + timedelta(seconds=uspan * random.random() * 0.8)
            rating = random.choice([1, 2, 3, 4, 5, None]) if status in ("closed", "resolved") else None
            rows.append((
                uid, created, updated, resolved,
                fake.sentence(nb_words=6).rstrip("."),
                fake.paragraph(nb_sentences=3),
                random.choice(categories),
                random.choice(priorities),
                status,
                random.choice(agents) if status != "open" else None,
                random.choice(sentiments),
                rating,
            ))
    return rows


TICKET_COLS = ("user_id", "created_at", "updated_at", "resolved_at", "subject", "body",
               "category", "priority", "status", "assigned_agent", "customer_sentiment",
               "satisfaction_rating")


# ---------------------------------------------------------------------------
# 4. Query-log generation
# ---------------------------------------------------------------------------

def pick_offset_days(recency: str, days: int) -> float:
    """Return how many days ago (0..days) a query of this template fired."""
    if recency == "recent":            # steady, mild bias to the last few weeks
        return (random.random() ** 1.3) * days
    if recency == "growing":           # traffic ramps up over the window
        return days * (1 - random.random() ** 0.6)
    if recency == "weekly":            # roughly one burst every 7 days
        week = random.randrange(0, max(1, days // 7))
        return min(days - 0.1, week * 7 + random.random() * 1.5)
    if recency == "quarterly":         # two clusters, near day ~10 and day ~82
        centre = random.choice([10, 82])
        return min(days - 0.1, max(0.1, random.gauss(centre, 2.5)))
    if recency == "single_old":        # a one-off run, ~85 days back
        return min(days - 0.1, max(0.1, random.gauss(85, 1.5)))
    return random.random() * days      # uniform


def gen_query_log(total: int, days: int):
    weights = [t["weight"] for t in QUERY_TEMPLATES]
    wsum = sum(weights)
    counts = [max(1 if t["recency"] != "single_old" else 2, round(total * w / wsum))
              for t, w in zip(QUERY_TEMPLATES, weights)]

    entries = []
    qid = 0
    for tmpl, count in zip(QUERY_TEMPLATES, counts):
        for _ in range(count):
            off = pick_offset_days(tmpl["recency"], days)
            ts = NOW - timedelta(days=off)
            hour = random.choices(range(24), weights=HOUR_WEIGHTS)[0]
            ts = ts.replace(hour=hour, minute=random.randint(0, 59),
                            second=random.randint(0, 59), microsecond=random.randint(0, 999999))
            if ts > NOW:
                ts -= timedelta(days=1)
            op = tmpl["op"]
            base = {"SELECT": 4.0, "INSERT": 6.0, "UPDATE": 5.0}.get(op, 4.0)
            entries.append({
                "ts": ts.isoformat(),
                "query_id": f"q_{qid:07d}",
                "template": tmpl["name"],
                "service": tmpl["service"],
                "db_user": db_user_for(tmpl["service"], op),
                "operation": op,
                "tables": tmpl["tables"],
                "columns_read": tmpl["reads"],
                "columns_written": tmpl["writes"],
                "rows": random.randint(1, 3) if op != "SELECT" else random.choice([1, 1, 1, 5, 20, 50]),
                "duration_ms": round(random.gammavariate(2.0, base / 2.0), 2),
                "sql": tmpl["sql"],
            })
            qid += 1

    entries.sort(key=lambda e: e["ts"])
    # renumber query ids in chronological order
    for i, e in enumerate(entries):
        e["query_id"] = f"q_{i:07d}"
    return entries


def write_query_log(entries):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jsonl_path = OUT_DIR / "query_log.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")

    csv_path = OUT_DIR / "query_log.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "query_id", "template", "service", "db_user", "operation",
                    "tables", "columns_read", "columns_written", "rows", "duration_ms", "sql"])
        for e in entries:
            w.writerow([e["ts"], e["query_id"], e["template"], e["service"], e["db_user"],
                        e["operation"], "|".join(e["tables"]), "|".join(e["columns_read"]),
                        "|".join(e["columns_written"]), e["rows"], e["duration_ms"], e["sql"]])
    return jsonl_path, csv_path


def write_column_catalog():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "column_catalog.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["table", "column", "sensitivity_tier", "sensitivity_score",
                    "pii_type", "expected_usage", "seeded_stale_rows", "rationale"])
        for row in COLUMN_CATALOG:
            w.writerow(row)
    return path


def usage_summary(entries):
    """Per-column observed read/write counts - the signal the analyzer must find."""
    seen = {}
    for e in entries:
        for c in e["columns_read"]:
            seen.setdefault(c, [0, 0])[0] += 1
        for c in e["columns_written"]:
            seen.setdefault(c, [0, 0])[1] += 1
    rows = []
    for table, col, tier, *_rest in COLUMN_CATALOG:
        key = f"{table}.{col}"
        r, wr = seen.get(key, (0, 0))
        rows.append((key, tier, r, wr))
    return rows


# ---------------------------------------------------------------------------
# 5. Database plumbing
# ---------------------------------------------------------------------------

def ensure_database(cfg: dict):
    """Connect to the maintenance DB and CREATE DATABASE if it is missing."""
    maint = dict(cfg, dbname="postgres")
    try:
        with psycopg.connect(**maint, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (cfg["dbname"],)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{cfg["dbname"]}"')
                print(f"  created database {cfg['dbname']!r}")
            else:
                print(f"  database {cfg['dbname']!r} already exists")
    except psycopg.OperationalError as exc:
        sys.exit(
            f"\nCould not connect to PostgreSQL as {cfg['user']}@{cfg['host']}:{cfg['port']}.\n"
            f"  {exc}\n"
            "Fix seed-data/.env, and make sure you created the role/database first:\n"
            "  psql -U postgres -c \"CREATE ROLE dma_seed WITH LOGIN PASSWORD 'dma_seed_local_pw' CREATEDB;\"\n"
            "  psql -U postgres -c \"CREATE DATABASE dma_auditor OWNER dma_seed;\"\n"
        )


def copy_rows(cur, table: str, cols: tuple, rows: list):
    collist = ", ".join(cols)
    with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def build_database(cfg: dict, args, fake: Faker):
    ensure_database(cfg)
    with psycopg.connect(**cfg) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            existing = {
                r[0] for r in cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ).fetchall()
            }
            if args.reset and (existing & set(TABLES)):
                print("  --reset: dropping existing tables")
                cur.execute("DROP TABLE IF EXISTS support_tickets, orders, users CASCADE")
                existing = set()

            if set(TABLES) & existing:
                sys.exit("  tables already exist - re-run with --reset to rebuild")

            for name in TABLES:
                cur.execute(DDL[name])
            print(f"  created tables: {', '.join(TABLES)}")

            # users
            users = gen_users(fake, args.users)
            copy_rows(cur, "users", USER_COLS, users)
            uid_created = cur.execute(
                "SELECT id, created_at FROM users ORDER BY id"
            ).fetchall()
            print(f"  inserted {len(users):,} users")

            # orders
            orders = gen_orders(fake, uid_created)
            copy_rows(cur, "orders", ORDER_COLS, orders)
            print(f"  inserted {len(orders):,} orders")

            # tickets
            tickets = gen_tickets(fake, uid_created)
            copy_rows(cur, "support_tickets", TICKET_COLS, tickets)
            print(f"  inserted {len(tickets):,} support_tickets")

            cur.execute("CREATE INDEX ON orders (user_id)")
            cur.execute("CREATE INDEX ON support_tickets (user_id)")
            cur.execute("ANALYZE")
        conn.commit()

    return dict(users=len(users), orders=len(orders), tickets=len(tickets))


def db_stats(cfg: dict) -> dict:
    with psycopg.connect(**cfg) as conn:
        q = conn.execute("""
            SELECT
              (SELECT count(*) FROM users),
              (SELECT count(*) FROM users WHERE created_at < now() - interval '400 days'),
              (SELECT count(*) FROM orders),
              (SELECT count(*) FROM orders WHERE created_at < now() - interval '400 days'),
              (SELECT count(*) FROM support_tickets),
              (SELECT count(*) FROM support_tickets WHERE created_at < now() - interval '400 days')
        """).fetchone()
    return dict(users=q[0], users_stale=q[1], orders=q[2], orders_stale=q[3],
               tickets=q[4], tickets_stale=q[5])


# ---------------------------------------------------------------------------
# 6. Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true",
                    help="drop existing tables and rebuild from scratch")
    ap.add_argument("--logs-only", action="store_true",
                    help="only (re)generate the query log + catalogue, skip the database")
    ap.add_argument("--users", type=int, default=600, help="number of users to seed (default 600)")
    ap.add_argument("--queries", type=int, default=30000,
                    help="approx number of query-log entries (default 30000)")
    ap.add_argument("--days", type=int, default=90, help="query-log window in days (default 90)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    args = ap.parse_args()

    load_env(HERE / ".env")
    random.seed(args.seed)
    fake = Faker()
    Faker.seed(args.seed)

    print(f"Data Minimization Auditor - Phase 0 seed generator  (seed={args.seed})")
    print(f"now = {NOW.isoformat()}\n")

    db_summary = None
    if not args.logs_only:
        print("[1/3] Database")
        cfg = db_settings()
        db_summary = build_database(cfg, args, fake)
        stats = db_stats(cfg)
        print(f"        stale (>400d): {stats['users_stale']} users, "
              f"{stats['orders_stale']} orders, {stats['tickets_stale']} tickets\n")
    else:
        print("[1/3] Database  (skipped: --logs-only)\n")

    print(f"[2/3] Query log  (~{args.queries} entries over {args.days} days)")
    entries = gen_query_log(args.queries, args.days)
    jsonl_path, csv_path = write_query_log(entries)
    span_lo = min(e["ts"] for e in entries)
    span_hi = max(e["ts"] for e in entries)
    print(f"        {len(entries):,} entries  {span_lo[:10]} .. {span_hi[:10]}")
    print(f"        {jsonl_path.relative_to(HERE.parent)}")
    print(f"        {csv_path.relative_to(HERE.parent)}\n")

    print("[3/3] Column catalogue (ground truth)")
    cat_path = write_column_catalog()
    print(f"        {cat_path.relative_to(HERE.parent)}\n")

    # manifest
    manifest = {
        "generated_at": NOW.isoformat(),
        "seed": args.seed,
        "database": None if args.logs_only else db_settings()["dbname"],
        "row_counts": db_summary,
        "query_log": {
            "entries": len(entries),
            "window_days": args.days,
            "from": span_lo,
            "to": span_hi,
            "templates": len(QUERY_TEMPLATES),
        },
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # usage signal preview
    print("Per-column observed usage in the query log (reads / writes):")
    print(f"  {'column':<38} {'tier':<8} {'reads':>8} {'writes':>8}")
    for key, tier, r, wr in usage_summary(entries):
        flag = "   <- NEVER queried" if (r == 0 and wr == 0) else ""
        print(f"  {key:<38} {tier:<8} {r:>8,} {wr:>8,}{flag}")

    print("\nDone. Next phases read from:")
    print(f"  - PostgreSQL db '{db_settings()['dbname']}' (tables: {', '.join(TABLES)})")
    print(f"  - {jsonl_path.relative_to(HERE.parent)}  (usage analyzer input)")
    print(f"  - {cat_path.relative_to(HERE.parent)}  (evaluation ground truth)")


if __name__ == "__main__":
    main()
