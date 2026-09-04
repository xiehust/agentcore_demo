#!/usr/bin/env python3
"""Delete every resource recorded in .deployment.json, in reverse dependency order.

Destructive. Review .deployment.json before running.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

import boto3
import botocore.exceptions

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".deployment.json"


def attempt(description: str, action: Callable[[], Any], *, ignore: tuple[str, ...] = ()) -> bool:
    try:
        action()
        print(f"  deleted {description}")
        return True
    except botocore.exceptions.ClientError as error:
        code = error.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "NoSuchEntity", "NotFoundException", *ignore):
            print(f"  {description}: already gone ({code})")
            return True
        print(f"  {description}: FAILED {code}: {error.response['Error'].get('Message')}")
        return False


def wait_gone(read: Callable[[], Any], description: str, attempts: int = 60, delay: int = 5) -> None:
    for _ in range(attempts):
        try:
            read()
        except botocore.exceptions.ClientError as error:
            if error.response["Error"]["Code"] in ("ResourceNotFoundException", "NotFoundException"):
                return
            raise
        time.sleep(delay)
    print(f"  warning: {description} still present after waiting")


def delete_role(iam: Any, role_name: str) -> None:
    for policy in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=role_name, PolicyName=policy)
    iam.delete_role(RoleName=role_name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        raise SystemExit("No .deployment.json found; nothing to clean up.")
    state = json.loads(STATE_FILE.read_text())
    created: dict[str, Any] = state["created"]
    region = state["region"]

    print(f"Region {region}, prefix {created.get('prefix')}. Resources to delete:")
    for key in ("targetId", "consentPortalId", "gatewayId", "googleProviderName", "cognitoProviderName",
                "portalRoleName", "gatewayRoleName", "cognitoDomain", "userPoolId"):
        if key in created:
            print(f"  {key}: {created[key]}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        raise SystemExit("Aborted.")

    session = boto3.Session(region_name=region)
    control = session.client("bedrock-agentcore-control")
    cognito = session.client("cognito-idp")
    iam = session.client("iam")
    ok = True

    if "targetId" in created:
        ok &= attempt(
            "gateway target",
            lambda: control.delete_gateway_target(gatewayIdentifier=created["gatewayId"], targetId=created["targetId"]),
        )
        wait_gone(
            lambda: control.get_gateway_target(gatewayIdentifier=created["gatewayId"], targetId=created["targetId"]),
            "gateway target",
        )

    if "consentPortalId" in created:
        ok &= attempt(
            "consent portal",
            lambda: control.delete_consent_portal(consentPortalIdentifier=created["consentPortalId"]),
        )
        wait_gone(
            lambda: control.get_consent_portal(consentPortalIdentifier=created["consentPortalId"]),
            "consent portal",
        )

    if "gatewayId" in created:
        ok &= attempt("gateway", lambda: control.delete_gateway(gatewayIdentifier=created["gatewayId"]))
        wait_gone(lambda: control.get_gateway(gatewayIdentifier=created["gatewayId"]), "gateway")

    for key in ("googleProviderName", "cognitoProviderName"):
        if key in created:
            name = created[key]
            ok &= attempt(f"credential provider {name}", lambda: control.delete_oauth2_credential_provider(name=name))

    for key in ("portalRoleName", "gatewayRoleName"):
        if key in created:
            role = created[key]
            ok &= attempt(f"IAM role {role}", lambda: delete_role(iam, role))

    if "cognitoDomain" in created:
        ok &= attempt(
            "Cognito domain",
            lambda: cognito.delete_user_pool_domain(Domain=created["cognitoDomain"], UserPoolId=created["userPoolId"]),
            ignore=("InvalidParameterException",),
        )
    if "userPoolId" in created:
        ok &= attempt("Cognito user pool", lambda: cognito.delete_user_pool(UserPoolId=created["userPoolId"]))

    if ok:
        STATE_FILE.unlink()
        print("\nAll resources deleted; .deployment.json removed.")
    else:
        print("\nSome deletions failed; .deployment.json kept so you can retry.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
