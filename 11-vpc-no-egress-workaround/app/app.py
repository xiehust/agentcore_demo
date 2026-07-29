"""
In-VPC internal HTTP API (Workaround 2 backend).

Runs on an EC2 instance in a private subnet with no public IP and no route to
the internet. Fronted by an internal NLB, which an API Gateway VPC Link targets.
Represents the "existing internal REST service" case from the design doc.
"""

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymysql

DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]
DB_NAME = os.environ["DB_NAME"]


def connect():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME,
        connect_timeout=8, cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def get_orders(qs):
    status = (qs.get("status") or [None])[0]
    limit = int((qs.get("limit") or [10])[0])
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
    return {"count": len(rows),
            "orders": [{**r, "amount": float(r["amount"])} for r in rows]}


def get_dbinfo():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT VERSION() AS version, @@hostname AS db_hostname, "
                    "DATABASE() AS db_name, USER() AS db_user, NOW() AS server_time")
        info = cur.fetchone()
    info["server_time"] = str(info["server_time"])
    return {
        "rds_endpoint": DB_HOST,
        "rds_resolved_private_ip": socket.gethostbyname(DB_HOST),
        "ec2_private_ip": socket.gethostbyname(socket.gethostname()),
        "ec2_hostname": socket.gethostname(),
        "path": "AgentCore Gateway -> API Gateway -> VPC Link -> NLB -> EC2 (private subnet) -> RDS",
        **info,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            if route == "/health":
                self._send(200, {"status": "ok"})
            elif route == "/orders":
                self._send(200, get_orders(parse_qs(parsed.query)))
            elif route == "/dbinfo":
                self._send(200, get_dbinfo())
            else:
                self._send(404, {"error": f"no route {route}"})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):  # keep journald output compact
        print(fmt % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
