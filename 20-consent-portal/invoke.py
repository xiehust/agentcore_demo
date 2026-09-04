#!/usr/bin/env python3
"""Call the Google Calendar tools through the gateway as the Cognito test user.

Flow: Cognito USER_PASSWORD_AUTH -> access token -> MCP JSON-RPC to the gateway
(initialize, tools/list, tools/call). If the user has not yet connected Google
Calendar on the consent portal, the gateway returns an authorization prompt
instead of events; the script detects that and points at the portal URL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".deployment.json"
PROTOCOL_VERSION = "2025-11-25"
TARGET_NAME = "google-calendar"


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        raise SystemExit("No .deployment.json found; run deploy.py first.")
    return json.loads(STATE_FILE.read_text())


def cognito_access_token(region: str, client_id: str, username: str, password: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=region)
    response = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return response["AuthenticationResult"]["AccessToken"]


class GatewaySession:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self.protocol_version = PROTOCOL_VERSION
        self.next_id = 1

    def initialize(self) -> dict[str, Any]:
        result = self.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "consent-portal-demo", "version": "1.0"},
            },
        )
        # Use whatever version the gateway negotiated for the rest of the session.
        self.protocol_version = result.get("protocolVersion", PROTOCOL_VERSION)
        return result

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            body["params"] = params
        self.next_id += 1
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
                if "text/event-stream" in response.headers.get("content-type", ""):
                    events = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
                    payload = events[-1] if events else {}
                else:
                    payload = json.loads(raw) if raw else {}
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise SystemExit(f"Gateway returned HTTP {error.code} for {method}:\n{detail}") from error
        if "error" in payload:
            error = payload["error"]
            # MCP 2025-11-25 URL elicitation: the gateway needs the user to consent first.
            if error.get("code") == -32042 and error.get("data", {}).get("elicitations"):
                return {"elicitations": error["data"]["elicitations"]}
            raise SystemExit(f"Gateway JSON-RPC error for {method}: {json.dumps(error, indent=2)}")
        return payload.get("result", {})


def text_of(result: dict[str, Any]) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="look-ahead window in days (default 7)")
    parser.add_argument("--calendar", default="primary", help="calendarId (default primary)")
    parser.add_argument("--list-calendars", action="store_true", help="call listCalendars instead of listEvents")
    args = parser.parse_args()

    state = load_state()
    created = state["created"]
    region = state["region"]
    user = created["testUser"]

    print(f"Signing in to Cognito as {user['username']} ...")
    token = cognito_access_token(region, created["apiClientId"], user["username"], user["password"])

    gateway = GatewaySession(created["gatewayUrl"], token)
    init = gateway.initialize()
    print(f"MCP protocol version negotiated: {init.get('protocolVersion')}")
    tools = gateway.call("tools/list").get("tools", [])
    names = [tool["name"] for tool in tools]
    print(f"Tools exposed by the gateway: {names}")

    target = created.get("targetId") and "google-calendar"
    if not target:
        raise SystemExit("Gateway target not deployed yet; finish deploy.py first.")

    if args.list_calendars:
        tool_name = "google-calendar___listCalendars"
        arguments: dict[str, Any] = {"maxResults": 50}
    else:
        now = dt.datetime.now(dt.timezone.utc)
        tool_name = "google-calendar___listEvents"
        arguments = {
            "calendarId": args.calendar,
            "timeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": (now + dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 25,
        }
    if tool_name not in names:
        raise SystemExit(f"{tool_name} not in tools/list; check the target status in the console.")

    print(f"Calling {tool_name} {json.dumps(arguments)} ...")
    result = gateway.call("tools/call", {"name": tool_name, "arguments": arguments})
    text = text_of(result)

    if result.get("elicitations"):
        print("\nNo Google credential is stored for this user yet; the gateway asked for consent (URL elicitation).")
        print(f"Recommended: open the consent portal, sign in as {user['username']}, and connect '{TARGET_NAME}':")
        print(f"    {created['portalUrl']}")
        print("\nThe gateway also returned a direct authorization URL. It is bound to this session for 10 minutes")
        print("and still requires the consent portal to complete session binding, so prefer the portal:")
        for elicitation in result["elicitations"]:
            print(f"    {elicitation.get('url')}")
        sys.exit(3)

    if result.get("isError") or "authorizationUrl" in text or ("authorization" in text.lower() and "http" in text):
        print("\nThe gateway could not find a stored Google credential for this user.")
        print(f"Open the consent portal, sign in as {user['username']}, and connect Google Calendar:")
        print(f"    {created['portalUrl']}")
        print("\nRaw gateway response:")
        print(text or json.dumps(result, indent=2))
        sys.exit(3)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print(text or json.dumps(result, indent=2))
        return

    items = payload.get("items", [])
    if args.list_calendars:
        print(f"\n{len(items)} calendar(s):")
        for item in items:
            flag = " (primary)" if item.get("primary") else ""
            print(f"  - {item.get('summary')}{flag}  [{item.get('id')}]")
        return

    print(f"\n{len(items)} event(s) in the next {args.days} day(s) on '{args.calendar}':")
    for item in items:
        start = item.get("start", {})
        when = start.get("dateTime") or start.get("date")
        print(f"  - {when}  {item.get('summary', '(no title)')}")


if __name__ == "__main__":
    main()
