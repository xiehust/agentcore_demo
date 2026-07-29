"""
Lambda tool provider for AgentCore Gateway (Workaround 1: Lambda bridge).

This function is attached to the isolated VPC's private subnets, so it can reach
the private RDS instance over 3306. AgentCore Gateway invokes it over the AWS
backbone via the Lambda service API -- the Gateway itself never enters the VPC,
which is exactly why this pattern needs no VPC egress feature.

Invocation shapes handled:
  1. AgentCore Gateway tool call -- the event IS the tool input object, and the
     tool name arrives in the client context as `bedrockAgentCoreToolName`.
  2. Direct admin invoke -- {"__admin": "init"} seeds the demo schema. Used
     because the VPC has no bastion and no internet route.
"""

import os
import socket

import pymysql

DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]
DB_NAME = os.environ["DB_NAME"]

SEED_ROWS = [
    ("ORD-1001", "alice@example.com", "SHIPPED", 129.99),
    ("ORD-1002", "bob@example.com", "PENDING", 42.50),
    ("ORD-1003", "carol@example.com", "SHIPPED", 891.00),
    ("ORD-1004", "dave@example.com", "CANCELLED", 15.25),
    ("ORD-1005", "erin@example.com", "PENDING", 310.75),
]


def connect():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        connect_timeout=8,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def tool_name_from(context):
    """AgentCore Gateway passes the resolved tool name in the client context."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName", "")
    # Tools are exposed as "<targetName>___<toolName>"; keep only the tool part.
    return raw.split("___")[-1] if raw else ""


# ---------------- tools ----------------

def t_list_orders(args):
    status = args.get("status")
    limit = int(args.get("limit", 10))
    limit = max(1, min(limit, 100))
    sql = "SELECT order_ref, customer_email, status, amount FROM orders"
    params = []
    if status:
        sql += " WHERE status = %s"
        params.append(status.upper())
    sql += " ORDER BY id LIMIT %s"
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {"count": len(rows), "orders": [
        {**r, "amount": float(r["amount"])} for r in rows
    ]}


def t_db_info(_args):
    """Proves the query really executed inside the private VPC against RDS."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT VERSION() AS version, @@hostname AS db_hostname, "
                    "DATABASE() AS db_name, USER() AS db_user, NOW() AS server_time")
        info = cur.fetchone()
    info["server_time"] = str(info["server_time"])
    return {
        "rds_endpoint": DB_HOST,
        "rds_resolved_private_ip": socket.gethostbyname(DB_HOST),
        "lambda_private_ip": socket.gethostbyname(socket.gethostname()),
        "path": "AgentCore Gateway -> Lambda (VPC-attached) -> RDS",
        **info,
    }


TOOLS = {"list_orders": t_list_orders, "db_info": t_db_info}


# ---------------- admin ----------------

def admin_init():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
              id INT AUTO_INCREMENT PRIMARY KEY,
              order_ref VARCHAR(32) NOT NULL UNIQUE,
              customer_email VARCHAR(128) NOT NULL,
              status VARCHAR(16) NOT NULL,
              amount DECIMAL(10,2) NOT NULL
            )
        """)
        for row in SEED_ROWS:
            cur.execute(
                "INSERT INTO orders (order_ref, customer_email, status, amount) "
                "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status)", row)
        cur.execute("SELECT COUNT(*) AS n FROM orders")
        n = cur.fetchone()["n"]
    return {"seeded": True, "row_count": n}


def lambda_handler(event, context):
    if isinstance(event, dict) and event.get("__admin") == "init":
        return admin_init()

    name = tool_name_from(context)
    args = event if isinstance(event, dict) else {}
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}", "available": sorted(TOOLS)}
    try:
        return fn(args)
    except Exception as exc:  # surfaced to the agent as the tool result
        return {"error": f"{type(exc).__name__}: {exc}"}
