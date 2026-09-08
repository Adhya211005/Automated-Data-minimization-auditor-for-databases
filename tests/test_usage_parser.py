"""Phase 3 - SQL -> columns parser. Pure unit tests, no database."""

from __future__ import annotations

import pytest

from auditor.usage.parser import QueryParser

SCHEMA = {
    "users": ["id", "email", "password_hash", "is_active", "full_name", "phone",
              "last_login_at", "last_login_ip", "updated_at", "created_at", "gender",
              "date_of_birth", "ssn"],
    "orders": ["id", "user_id", "status", "total_amount", "currency", "ip_address",
               "card_last4", "created_at", "updated_at", "shipping_address"],
    "support_tickets": ["id", "user_id", "subject", "body", "category", "status",
                        "priority", "created_at", "assigned_agent"],
}

P = QueryParser.from_schema(SCHEMA)
P_NOSCHEMA = QueryParser()


def test_simple_select_projection_and_where():
    qc = P.parse("SELECT id, email, password_hash FROM users WHERE email = $1")
    assert qc.parsed_ok and qc.method == "sqlglot"
    assert qc.reads == {"users.id", "users.email", "users.password_hash"}
    assert qc.writes == set()


def test_join_resolves_aliases_and_on_columns():
    qc = P.parse(
        "SELECT t.subject, u.email FROM support_tickets t "
        "JOIN users u ON u.id = t.user_id WHERE t.id = $1"
    )
    assert qc.reads == {
        "support_tickets.subject", "users.email",
        "support_tickets.id", "support_tickets.user_id", "users.id",
    }


def test_group_by_and_order_by_columns_count_as_reads():
    qc = P.parse(
        "SELECT gender, count(*) FROM users WHERE is_active GROUP BY gender ORDER BY gender"
    )
    assert "users.gender" in qc.reads
    assert "users.is_active" in qc.reads


def test_count_star_is_not_a_wildcard():
    # regression: count(*) must not pull in every column of the table
    qc = P.parse("SELECT count(*) FROM users WHERE gender = 'f'")
    assert qc.reads == {"users.gender"}
    assert not qc.wildcards


def test_select_star_expands_with_schema():
    qc = P.parse("SELECT * FROM users WHERE id = $1")
    assert qc.reads == {f"users.{c}" for c in SCHEMA["users"]}
    assert not qc.wildcards


def test_select_star_without_schema_is_a_wildcard():
    qc = P_NOSCHEMA.parse("SELECT * FROM users WHERE id = 1")
    assert qc.wildcards == {"users.*"}


def test_subquery_columns_are_reads():
    qc = P.parse(
        "SELECT id FROM orders WHERE user_id IN "
        "(SELECT id FROM users WHERE last_login_at < now())"
    )
    assert {"orders.id", "orders.user_id", "users.id", "users.last_login_at"} <= qc.reads


def test_cte_columns_are_reads():
    qc = P.parse(
        "WITH recent AS (SELECT id FROM users WHERE created_at > now()) "
        "SELECT o.id FROM orders o JOIN recent r ON r.id = o.user_id"
    )
    assert "users.id" in qc.reads
    assert "users.created_at" in qc.reads
    assert "orders.user_id" in qc.reads


def test_update_set_targets_are_writes_where_is_read():
    qc = P.parse(
        "UPDATE users SET last_login_at = now(), last_login_ip = $1, updated_at = now() "
        "WHERE id = $2"
    )
    assert qc.writes == {"users.last_login_at", "users.last_login_ip", "users.updated_at"}
    assert qc.reads == {"users.id"}


def test_update_with_column_valued_assignment_reads_rhs():
    qc = P.parse("UPDATE orders SET updated_at = created_at WHERE status = 'x'")
    assert qc.writes == {"orders.updated_at"}
    assert {"orders.created_at", "orders.status"} <= qc.reads


def test_insert_with_column_list_are_writes():
    qc = P.parse(
        "INSERT INTO orders (user_id, status, total_amount) VALUES ($1, $2, $3)"
    )
    assert qc.writes == {"orders.user_id", "orders.status", "orders.total_amount"}


def test_insert_with_elided_values_uses_regex_fallback():
    qc = P.parse("INSERT INTO orders (user_id, status, total_amount) VALUES (...)")
    assert qc.method == "insert-regex"
    assert qc.writes == {"orders.user_id", "orders.status", "orders.total_amount"}


def test_insert_select_reads_source_columns():
    qc = P.parse(
        "INSERT INTO orders (user_id, status) SELECT id, 'new' FROM users WHERE is_active"
    )
    assert "orders.user_id" in qc.writes and "orders.status" in qc.writes
    assert "users.id" in qc.reads and "users.is_active" in qc.reads


def test_delete_where_columns_are_reads():
    qc = P.parse("DELETE FROM support_tickets WHERE status = 'closed' AND created_at < now()")
    assert {"support_tickets.status", "support_tickets.created_at"} <= qc.reads


def test_unparseable_returns_not_ok():
    qc = P.parse("this is not sql at all ;;;")
    assert not qc.parsed_ok
    assert qc.all_columns == set()


def test_unknown_table_column_goes_to_unresolved():
    qc = P.parse("SELECT mystery_col FROM some_other_table WHERE x = 1")
    assert qc.reads == set()
    assert qc.unresolved


def test_parse_is_cached():
    p = QueryParser.from_schema(SCHEMA)
    a = p.parse("SELECT id FROM users")
    b = p.parse("SELECT id FROM users")
    assert a is b
