"""The metadata extractor's output shape is the contract for Phases 2-4.
These tests pin that shape against the Phase 0 seed schema."""

from __future__ import annotations

import json

from auditor.ingestion.metadata import DatabaseMetadata


SEED_TABLES = {"users", "orders", "support_tickets"}


def test_all_seed_tables_present(metadata):
    assert SEED_TABLES.issubset(set(metadata.table_names))


def test_database_identity(metadata):
    assert metadata.dialect == "postgresql"
    assert metadata.database == "dma_auditor"
    assert metadata.schema == "public"
    assert metadata.generated_at.endswith("+00:00")


def test_column_count_and_lookup(metadata):
    # 46 columns across the three seed tables (see seed-data/seed.py)
    seed_cols = [c for c in metadata.iter_columns() if c.table in SEED_TABLES]
    assert len(seed_cols) == 46
    email = metadata.column("users.email")
    assert email is not None
    assert email.qualified_name == "users.email"
    assert email.generic_type in {"string", "text"}
    assert email.nullable is False
    assert email.is_unique is True


def test_primary_and_foreign_keys(metadata):
    assert metadata.column("users.id").is_primary_key is True
    fk = metadata.column("orders.user_id")
    assert fk.is_foreign_key is True
    assert fk.foreign_key_target == "users.id"
    orders = metadata.table("orders")
    assert orders.primary_key == ["id"]
    assert any(f["ref_table"] == "users" for f in orders.foreign_keys)


def test_temporal_columns_detected_with_spans(metadata):
    users = metadata.table("users")
    assert "created_at" in users.temporal_columns
    created = metadata.column("users.created_at")
    assert created.is_temporal is True
    assert created.generic_type == "timestamptz"
    # spans were pre-fetched and are ISO strings
    assert created.min_value and created.max_value
    assert created.min_value < created.max_value
    # non-temporal columns carry no span
    assert metadata.column("users.email").min_value is None


def test_sample_values_present_and_typed(metadata):
    email = metadata.column("users.email")
    assert email.sample_size > 0
    assert len(email.sample_values) > 0
    assert all(isinstance(v, str) and "@" in v for v in email.sample_values)
    # harmless + never-queried columns still get sampled (Phase 3/5 need them)
    assert metadata.column("users.favorite_color").sample_values
    assert metadata.column("users.mothers_maiden_name") is not None


def test_row_counts_positive_and_exact(metadata):
    for t in metadata.tables:
        if t.name in SEED_TABLES:
            assert t.row_count > 0
            assert t.row_count_exact is True


def test_json_round_trip_preserves_shape(metadata):
    blob = metadata.to_json()
    json.loads(blob)  # valid JSON
    restored = DatabaseMetadata.from_json(blob)
    assert restored.table_names == metadata.table_names
    assert restored.column_names == metadata.column_names
    a = metadata.column("orders.user_id")
    b = restored.column("orders.user_id")
    assert (b.is_foreign_key, b.foreign_key_target, b.generic_type) == \
           (a.is_foreign_key, a.foreign_key_target, a.generic_type)


def test_dict_has_counts_for_consumers(metadata):
    d = metadata.to_dict()
    assert d["table_count"] == len(metadata.tables)
    assert d["column_count"] == sum(len(t.columns) for t in metadata.tables)
    assert d["tables"][0]["columns"][0]["qualified_name"] == \
           metadata.tables[0].columns[0].qualified_name
