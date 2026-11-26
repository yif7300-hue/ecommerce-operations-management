"""
Features:
1. Multi-criteria Order Search (`search_orders`) with SQL-level pagination (`limit`, `offset`)
2. Full Order Detail Retrieval (`get_order_detail`) - header, items, payments, reviews
3. Order Lifecycle Timeline & SLA Tracking (`track_order_status`)
4. Transactional Order Status Update (`update_order_status`) with CHECK constraint validation
   and automatic rollback on invalid transitions or DB errors.
5. Query Execution Plan & Latency Benchmark (`benchmark_order_queries`)
6. Interactive CLI Menu (`cli_menu`) or automated demo (`run_demo`).
"""
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))


def resolve_db_path() -> str:
    """Resolve olist.db across modules/order/, repository root, or common data folders."""
    candidates = [
        os.path.join(BASE_DIR, "olist.db"),
        os.path.join(REPO_ROOT, "olist.db"),
        os.path.join(REPO_ROOT, "database", "olist.db"),
        os.path.join(REPO_ROOT, "data", "olist.db"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return os.path.join(REPO_ROOT, "olist.db")


DB_PATH = resolve_db_path()

VALID_STATUSES = (
    "created",
    "approved",
    "invoiced",
    "processing",
    "shipped",
    "delivered",
    "unavailable",
    "canceled",
)

ALLOWED_TRANSITIONS = {
    "created": {"approved", "canceled", "unavailable"},
    "approved": {"invoiced", "processing", "canceled", "unavailable"},
    "invoiced": {"processing", "shipped", "canceled", "unavailable"},
    "processing": {"shipped", "canceled", "unavailable"},
    "shipped": {"delivered", "canceled"},
    "delivered": set(),
    "unavailable": set(),
    "canceled": set(),
}

STATUS_TIMESTAMP_COL = {
    "approved": "order_approved_at",
    "shipped": "order_delivered_carrier_date",
    "delivered": "order_delivered_customer_date",
}


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled and dict-like row access."""
    resolved = db_path or resolve_db_path()
    if not os.path.exists(resolved):
        raise FileNotFoundError(
            f"Database not found at '{resolved}'. Please run `python load_to_sqlite.py` first."
        )
    con = sqlite3.connect(resolved)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def search_orders(
    order_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    customer_unique_id: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search orders by optional filters (order_id prefix/exact, customer_id,
    customer_unique_id, order_status, purchase date range) with SQL pagination.
    """
    conditions = []
    params: List[Any] = []

    if order_id:
        conditions.append("o.order_id LIKE ?")
        params.append(f"{order_id.strip()}%")
    if customer_id:
        conditions.append("o.customer_id = ?")
        params.append(customer_id.strip())
    if customer_unique_id:
        conditions.append("c.customer_unique_id = ?")
        params.append(customer_unique_id.strip())
    if status:
        status_clean = status.strip().lower()
        if status_clean not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}.")
        conditions.append("o.order_status = ?")
        params.append(status_clean)
    if start_date:
        conditions.append("o.order_purchase_timestamp >= ?")
        params.append(start_date.strip())
    if end_date:
        conditions.append("o.order_purchase_timestamp <= ?")
        params.append(end_date.strip())

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            o.order_id,
            o.customer_id,
            c.customer_unique_id,
            g.city  AS customer_city,
            g.state AS customer_state,
            o.order_status,
            o.order_purchase_timestamp,
            o.order_estimated_delivery_date,
            COUNT(oi.order_item_id)                                  AS item_count,
            ROUND(COALESCE(SUM(oi.price), 0), 2)                     AS items_subtotal,
            ROUND(COALESCE(SUM(oi.freight_value), 0), 2)             AS freight_total,
            ROUND(COALESCE(SUM(oi.price + oi.freight_value), 0), 2)  AS order_total
        FROM orders o
        LEFT JOIN customer c     ON o.customer_id = c.customer_id
        LEFT JOIN geolocation g  ON c.zip_code_prefix = g.zip_code_prefix
        LEFT JOIN order_item oi  ON o.order_id = oi.order_id
        {where_clause}
        GROUP BY o.order_id
        ORDER BY o.order_purchase_timestamp DESC
        LIMIT ? OFFSET ?
    """
    params.append(max(1, int(limit)))
    params.append(max(0, int(offset)))

    with get_connection(db_path) as con:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_order_detail(order_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieve full details for a single order:
      - header (customer info, timestamps, status, totals)
      - items (product, English category, seller location, price, freight)
      - payments (sequential payment records)
      - reviews (customer scores & comments)
    """
    oid = order_id.strip()
    with get_connection(db_path) as con:
        header_row = con.execute(
            """
            SELECT
                o.*,
                c.customer_unique_id,
                c.zip_code_prefix AS customer_zip,
                g.city            AS customer_city,
                g.state           AS customer_state
            FROM orders o
            LEFT JOIN customer c    ON o.customer_id = c.customer_id
            LEFT JOIN geolocation g ON c.zip_code_prefix = g.zip_code_prefix
            WHERE o.order_id = ?
            """,
            (oid,),
        ).fetchone()

        if header_row is None:
            return None

        items_rows = con.execute(
            """
            SELECT
                oi.order_item_id,
                oi.product_id,
                COALESCE(pc.product_category_name_english, p.product_category_name, 'unknown') AS category_en,
                oi.seller_id,
                sg.city  AS seller_city,
                sg.state AS seller_state,
                oi.shipping_limit_date,
                oi.price,
                oi.freight_value,
                ROUND(oi.price + oi.freight_value, 2) AS line_total
            FROM order_item oi
            LEFT JOIN product p           ON oi.product_id = p.product_id
            LEFT JOIN product_category pc ON p.product_category_name = pc.product_category_name
            LEFT JOIN seller s            ON oi.seller_id = s.seller_id
            LEFT JOIN geolocation sg      ON s.zip_code_prefix = sg.zip_code_prefix
            WHERE oi.order_id = ?
            ORDER BY oi.order_item_id
            """,
            (oid,),
        ).fetchall()

        payment_rows = con.execute(
            """
            SELECT payment_sequential, payment_type, payment_installments, payment_value
            FROM payment
            WHERE order_id = ?
            ORDER BY payment_sequential
            """,
            (oid,),
        ).fetchall()

        review_rows = con.execute(
            """
            SELECT review_pk, review_id, review_score, review_comment_title,
                   review_comment_message, review_creation_date, review_answer_timestamp
            FROM review
            WHERE order_id = ?
            """,
            (oid,),
        ).fetchall()

    items = [dict(r) for r in items_rows]
    payments = [dict(r) for r in payment_rows]
    reviews = [dict(r) for r in review_rows]
    header = dict(header_row)
    header["items_subtotal"] = round(sum(x["price"] for x in items), 2)
    header["freight_total"] = round(sum(x["freight_value"] for x in items), 2)
    header["order_total"] = round(header["items_subtotal"] + header["freight_total"], 2)
    header["total_paid"] = round(sum(x["payment_value"] or 0 for x in payments), 2)

    return {
        "header": header,
        "items": items,
        "payments": payments,
        "reviews": reviews,
    }


def track_order_status(order_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Build a structured lifecycle timeline & delivery SLA evaluation for an order.
    """
    detail = get_order_detail(order_id, db_path=db_path)
    if detail is None:
        return None

    h = detail["header"]
    timeline = [
        {"stage": "1. Order Placed (created)", "timestamp": h["order_purchase_timestamp"], "completed": bool(h["order_purchase_timestamp"])},
        {"stage": "2. Payment Approved (approved)", "timestamp": h["order_approved_at"], "completed": bool(h["order_approved_at"])},
        {"stage": "3. Handed to Carrier (shipped)", "timestamp": h["order_delivered_carrier_date"], "completed": bool(h["order_delivered_carrier_date"])},
        {"stage": "4. Delivered to Customer (delivered)", "timestamp": h["order_delivered_customer_date"], "completed": bool(h["order_delivered_customer_date"])},
    ]

    sla_status = "In Progress"
    if h["order_status"] in ("canceled", "unavailable"):
        sla_status = f"Order {h['order_status'].capitalize()}"
    elif h["order_delivered_customer_date"] and h["order_estimated_delivery_date"]:
        delivered_day = str(h["order_delivered_customer_date"])[:10]
        estimated_day = str(h["order_estimated_delivery_date"])[:10]
        sla_status = "On Time" if delivered_day <= estimated_day else "Delayed"

    return {
        "order_id": h["order_id"],
        "current_status": h["order_status"],
        "estimated_delivery_date": h["order_estimated_delivery_date"],
        "sla_status": sla_status,
        "next_allowed_statuses": sorted(ALLOWED_TRANSITIONS.get(h["order_status"], set())),
        "timeline": timeline,
    }


def update_order_status(
    order_id: str,
    new_status: str,
    event_timestamp: Optional[str] = None,
    strict_transition: bool = True,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transactionally update an order's status (and corresponding lifecycle timestamp).
    Uses explicit BEGIN / COMMIT / ROLLBACK and validates both business state machine
    rules and SQLite schema CHECK constraints.
    """
    oid = order_id.strip()
    target = new_status.strip().lower()
    ts = event_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    con = get_connection(db_path)
    try:
        con.execute("BEGIN IMMEDIATE;")

        row = con.execute(
            "SELECT order_id, order_status FROM orders WHERE order_id = ?", (oid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Order '{oid}' does not exist.")

        old_status = row["order_status"]
        if old_status == target:
            raise ValueError(f"Order '{oid}' is already in status '{target}'.")

        if strict_transition:
            allowed = ALLOWED_TRANSITIONS.get(old_status, set())
            if target not in allowed:
                raise ValueError(
                    f"Invalid status transition: '{old_status}' -> '{target}'. "
                    f"Allowed next statuses: {sorted(allowed) if allowed else 'None (terminal state)'}"
                )

        ts_col = STATUS_TIMESTAMP_COL.get(target)
        if ts_col:
            con.execute(
                f"UPDATE orders SET order_status = ?, {ts_col} = COALESCE({ts_col}, ?) WHERE order_id = ?",
                (target, ts, oid),
            )
        else:
            con.execute(
                "UPDATE orders SET order_status = ? WHERE order_id = ?",
                (target, oid),
            )

        con.commit()
        return {
            "success": True,
            "order_id": oid,
            "old_status": old_status,
            "new_status": target,
            "timestamp_updated": ts_col,
            "timestamp_value": ts if ts_col else None,
        }
    except Exception as exc:
        con.rollback()
        raise RuntimeError(f"Transaction rolled back: {exc}") from exc
    finally:
        con.close()


def get_status_summary(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return order counts and GMV grouped by order_status."""
    sql = """
        SELECT
            o.order_status,
            COUNT(DISTINCT o.order_id) AS order_count,
            ROUND(100.0 * COUNT(DISTINCT o.order_id) / (SELECT COUNT(*) FROM orders), 2) AS share_pct,
            ROUND(COALESCE(SUM(oi.price + oi.freight_value), 0), 2) AS total_gmv
        FROM orders o
        LEFT JOIN order_item oi ON o.order_id = oi.order_id
        GROUP BY o.order_status
        ORDER BY order_count DESC
    """
    with get_connection(db_path) as con:
        return [dict(r) for r in con.execute(sql).fetchall()]


def benchmark_order_queries(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Run query execution plans (EXPLAIN QUERY PLAN) and latency benchmarks
    for key Module D queries to support performance reporting.
    """
    test_cases = [
        (
            "filter_by_status",
            "SELECT order_id, customer_id, order_purchase_timestamp FROM orders WHERE order_status = ? LIMIT 20",
            ("shipped",),
        ),
        (
            "filter_by_customer",
            "SELECT order_id, order_status, order_purchase_timestamp FROM orders WHERE customer_id = ?",
            ("f3f0e613e0bdb9c7cee75504f0f90679",),
        ),
        (
            "order_items_join",
            """
            SELECT oi.order_item_id, oi.product_id, oi.seller_id, oi.price, oi.freight_value
            FROM order_item oi
            WHERE oi.order_id = ?
            ORDER BY oi.order_item_id
            """,
            ("0008288aa423d2a3f00fcb17cd7d8719",),
        ),
    ]

    results = []
    with get_connection(db_path) as con:
        for name, sql, params in test_cases:
            plan_rows = con.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            plan_text = " | ".join(r["detail"] for r in plan_rows)

            t0 = time.perf_counter()
            rows = con.execute(sql, params).fetchall()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            results.append({
                "query": name,
                "row_count": len(rows),
                "elapsed_ms": round(elapsed_ms, 3),
                "plan": plan_text,
            })

    print("\n--- Module D Query Plan & Performance Benchmark ---")
    for item in results:
        print(
            f"  [{item['query']}] rows={item['row_count']} | "
            f"time={item['elapsed_ms']:.3f} ms | plan={item['plan']}"
        )
    return {"benchmarks": results}


def print_order_detail(detail: Dict[str, Any]) -> None:
    """Pretty-print an order detail dictionary to console."""
    h = detail["header"]
    print("\n--- Order Detail: " + str(h["order_id"]) + " ---")
    print(f"Status      : {h['order_status']} | Customer: {h['customer_id']} ({h['customer_city']}, {h['customer_state']})")
    print(f"Purchased   : {h['order_purchase_timestamp']} | Approved: {h['order_approved_at']}")
    print(f"Shipped     : {h['order_delivered_carrier_date']} | Delivered: {h['order_delivered_customer_date']}")
    print(f"Est. Deliv. : {h['order_estimated_delivery_date']}")
    print(
        f"Totals      : Items R$ {h['items_subtotal']:.2f} + Freight R$ {h['freight_total']:.2f} "
        f"= R$ {h['order_total']:.2f} (Paid: R$ {h['total_paid']:.2f})"
    )

    print("Line Items:")
    for it in detail["items"]:
        print(
            f"  #{it['order_item_id']} | Product {it['product_id'][:8]}... ({it['category_en']}) | "
            f"Seller {it['seller_id'][:8]}... ({it['seller_city']}-{it['seller_state']}) | "
            f"Price R$ {it['price']:.2f} + Freight R$ {it['freight_value']:.2f}"
        )

    print("Payments:")
    for p in detail["payments"]:
        print(
            f"  Seq {p['payment_sequential']}: {p['payment_type']} "
            f"({p['payment_installments']}x) - R$ {p['payment_value']:.2f}"
        )

    if detail["reviews"]:
        for r in detail["reviews"]:
            print(
                f"Review      : Score {r['review_score']}/5 | "
                f"Title: {r['review_comment_title']} | Msg: {r['review_comment_message']}"
            )
    else:
        print("Review      : No review recorded.")


def run_demo() -> None:
    print("===== Module D: Order Management Demo =====")

    print("\n1. Order Status Summary:")
    for row in get_status_summary():
        print(
            f"  {row['order_status']:12s} | Orders: {row['order_count']:6d} "
            f"({row['share_pct']:5.2f}%) | GMV: R$ {row['total_gmv']:12,.2f}"
        )

    print("\n2. Search Orders (status='shipped', limit=3, offset=0):")
    shipped_orders = search_orders(status="shipped", limit=3, offset=0)
    for o in shipped_orders:
        print(
            f"  {o['order_id']} | {o['order_purchase_timestamp']} | "
            f"{o['customer_state']} | Items: {o['item_count']} | Total: R$ {o['order_total']:.2f}"
        )

    sample_id = "0008288aa423d2a3f00fcb17cd7d8719"
    detail = get_order_detail(sample_id)
    if detail:
        print("\n3. Full Order Detail:")
        print_order_detail(detail)

        print("\n4. Order Status Tracking:")
        track = track_order_status(sample_id)
        print(f"  Current Status: {track['current_status']} | SLA: {track['sla_status']}")
        for step in track["timeline"]:
            mark = "[x]" if step["completed"] else "[ ]"
            print(f"  {mark} {step['stage']}: {step['timestamp']}")

    print("\n5. Transaction Control & Rollback Verification:")
    try:
        update_order_status(sample_id, "invalid_status")
    except RuntimeError as e:
        print(f"  [OK] Invalid status blocked -> {e}")

    try:
        update_order_status(sample_id, "shipped")
    except RuntimeError as e:
        print(f"  [OK] Terminal state rule enforced -> {e}")

    benchmark_order_queries()


def cli_menu() -> None:
    """Simple interactive CLI for video recording."""
    while True:
        print("\n================ Order Management (Module D) ================")
        print("1. View Order Status Summary")
        print("2. Search Orders (by order_id / customer_id / status)")
        print("3. View Full Order Detail (by order_id)")
        print("4. Track Order Lifecycle & SLA (by order_id)")
        print("5. Update Order Status (Transactional)")
        print("6. Run Automated Demo")
        print("7. Run Query Plan & Performance Benchmark")
        print("0. Exit")
        choice = input("Select an option: ").strip()

        try:
            if choice == "1":
                for row in get_status_summary():
                    print(
                        f"  {row['order_status']:12s} | {row['order_count']:6d} "
                        f"({row['share_pct']:5.2f}%) | GMV: R$ {row['total_gmv']:12,.2f}"
                    )
            elif choice == "2":
                oid = input("Order ID (or prefix, Enter to skip): ").strip() or None
                cid = input("Customer ID (Enter to skip): ").strip() or None
                st = input(f"Status {VALID_STATUSES} (Enter to skip): ").strip() or None
                results = search_orders(order_id=oid, customer_id=cid, status=st, limit=10)
                print(f"Found {len(results)} matching orders:")
                for r in results:
                    print(
                        f"  {r['order_id']} | {r['order_status']:11s} | "
                        f"{r['order_purchase_timestamp']} | R$ {r['order_total']:.2f}"
                    )
            elif choice == "3":
                oid = input("Enter Order ID: ").strip()
                detail = get_order_detail(oid)
                if detail:
                    print_order_detail(detail)
                else:
                    print("Order not found.")
            elif choice == "4":
                oid = input("Enter Order ID: ").strip()
                track = track_order_status(oid)
                if track:
                    print(f"Order: {track['order_id']} | Status: {track['current_status']} | SLA: {track['sla_status']}")
                    print(f"Allowed next statuses: {track['next_allowed_statuses']}")
                    for s in track["timeline"]:
                        print(f"  {'[x]' if s['completed'] else '[ ]'} {s['stage']}: {s['timestamp']}")
                else:
                    print("Order not found.")
            elif choice == "5":
                oid = input("Enter Order ID: ").strip()
                new_st = input(f"Enter New Status {VALID_STATUSES}: ").strip()
                res = update_order_status(oid, new_st)
                print("Update committed:", res)
            elif choice == "6":
                run_demo()
            elif choice == "7":
                benchmark_order_queries()
            elif choice == "0":
                break
            else:
                print("Invalid option.")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    run_demo()
    print("\nTip: Run `python -c \"import order_manager; order_manager.cli_menu()\"` for interactive mode.")
