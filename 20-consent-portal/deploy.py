#!/usr/bin/env python3
"""Deploy an AgentCore Consent Portal demo: Cognito inbound, Gateway, Google Calendar 3LO target.

The script is re-entrant. Every step records what it created in .deployment.json and is
skipped on the next run, so you can stop at the Google Console step, register the callback
URL, and run the script again to finish.

Steps:
    1. Cognito user pool, Hosted UI domain, two app clients, one test user
    2. Gateway service role + CUSTOM_JWT Gateway trusting the Cognito pool
    3. OAuth2 credential provider for Cognito (the consent portal's primary IdP)
    4. Consent portal execution role
    5. Consent portal (wait for ACTIVE), register <portalUrl>/callback on the Cognito client
    6. GoogleOauth2 credential provider (needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env)
    7. Gateway target for Google Calendar with AUTHORIZATION_CODE outbound auth
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any, Callable

import boto3
import botocore.exceptions
import dotenv

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".deployment.json"
OPENAPI_FILE = ROOT / "google_calendar_openapi.json"

TAGS = {"Project": "AgentCoreConsentPortalDemo"}
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PORTAL_SCOPES = ["openid", "email", "profile"]
TEST_USERNAME = "testuser"
GOOGLE_PROVIDER_NAME = "google-calendar"
TARGET_NAME = "google-calendar"


# ── helpers ────────────────────────────────────────────────────────────────────


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"created": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(12))
    # Guarantee every Cognito default-policy character class.
    return f"Aa1!{body}"


def wait_for(
    read: Callable[[], dict[str, Any]],
    ready: Callable[[dict[str, Any]], bool],
    *,
    description: str,
    failed: set[str],
    attempts: int = 60,
    delay: int = 5,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(attempts):
        last = read()
        if ready(last):
            return last
        if last.get("status") in failed:
            raise RuntimeError(f"{description} failed: {json.dumps(last, default=str)}")
        time.sleep(delay)
    raise TimeoutError(f"Timed out waiting for {description}: {json.dumps(last, default=str)}")


def retry_iam_propagation(action: Callable[[], Any], *, attempts: int = 12, delay: int = 5) -> Any:
    """IAM roles are eventually consistent; retry validation failures caused by a fresh role."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return action()
        except botocore.exceptions.ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in {"ValidationException", "AccessDeniedException", "InvalidParameterValueException"}:
                raise
            last = error
            time.sleep(delay)
    assert last is not None
    raise last


# ── deployment ─────────────────────────────────────────────────────────────────


class Deployment:
    def __init__(self, region: str) -> None:
        self.region = region
        self.session = boto3.Session(region_name=region)
        self.account = self.session.client("sts").get_caller_identity()["Account"]
        self.cognito = self.session.client("cognito-idp")
        self.iam = self.session.client("iam")
        self.control = self.session.client("bedrock-agentcore-control")
        self.state = load_state()
        self.state.setdefault("region", region)
        self.state.setdefault("account", self.account)
        if self.state["region"] != region:
            raise SystemExit(
                f".deployment.json belongs to region {self.state['region']}; run with --region {self.state['region']} "
                "or run cleanup.py first."
            )
        self.created: dict[str, Any] = self.state["created"]
        if "prefix" not in self.created:
            self.created["prefix"] = f"consent-demo-{secrets.token_hex(3)}"
            self.save()

    @property
    def prefix(self) -> str:
        return self.created["prefix"]

    def save(self) -> None:
        save_state(self.state)

    def discovery_url(self) -> str:
        return (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{self.created['userPoolId']}/.well-known/openid-configuration"
        )

    # 1 ───────────────────────────────────────────────────────────────────────
    def cognito_setup(self) -> None:
        step("Step 1: Cognito user pool (inbound IdP + consent portal primary IdP)")
        if "userPoolId" not in self.created:
            pool = self.cognito.create_user_pool(
                PoolName=self.prefix,
                UserPoolTags=TAGS,
                AutoVerifiedAttributes=["email"],
                Schema=[{"Name": "email", "Required": True, "Mutable": True}],
            )["UserPool"]
            self.created["userPoolId"] = pool["Id"]
            self.save()
        pool_id = self.created["userPoolId"]
        print(f"  User pool: {pool_id}")

        if "cognitoDomain" not in self.created:
            domain = self.prefix
            self.cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
            self.created["cognitoDomain"] = domain
            self.save()
        print(f"  Hosted UI domain: https://{self.created['cognitoDomain']}.auth.{self.region}.amazoncognito.com")

        if "apiClientId" not in self.created:
            client = self.cognito.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName=f"{self.prefix}-api",
                GenerateSecret=False,
                ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            )["UserPoolClient"]
            self.created["apiClientId"] = client["ClientId"]
            self.save()
        print(f"  API client (USER_PASSWORD_AUTH, no secret): {self.created['apiClientId']}")

        if "portalClientId" not in self.created:
            client = self.cognito.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName=f"{self.prefix}-portal",
                GenerateSecret=True,
                ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
                SupportedIdentityProviders=["COGNITO"],
                AllowedOAuthFlows=["code"],
                AllowedOAuthScopes=PORTAL_SCOPES,
                AllowedOAuthFlowsUserPoolClient=True,
                # Real value is <portalUrl>/callback, which only exists after the portal is ACTIVE.
                CallbackURLs=["https://placeholder.invalid/callback"],
            )["UserPoolClient"]
            self.created["portalClientId"] = client["ClientId"]
            self.save()
        print(f"  Portal client (authorization code + secret): {self.created['portalClientId']}")

        if "testUser" not in self.created:
            password = random_password()
            self.cognito.admin_create_user(
                UserPoolId=pool_id,
                Username=TEST_USERNAME,
                TemporaryPassword=password,
                UserAttributes=[
                    {"Name": "email", "Value": f"{TEST_USERNAME}@example.com"},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",
            )
            self.cognito.admin_set_user_password(
                UserPoolId=pool_id, Username=TEST_USERNAME, Password=password, Permanent=True
            )
            self.created["testUser"] = {"username": TEST_USERNAME, "password": password}
            self.save()
        print(f"  Test user: {TEST_USERNAME} (password stored in .deployment.json)")

    # 2 ───────────────────────────────────────────────────────────────────────
    def gateway_setup(self) -> None:
        step("Step 2: Gateway (CUSTOM_JWT trusting the Cognito pool)")
        role_name = f"{self.prefix}-gateway-role"
        if "gatewayRoleArn" not in self.created:
            trust = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": self.account},
                            "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                        },
                    }
                ],
            }
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
            )["Role"]
            self.iam.put_role_policy(
                RoleName=role_name,
                PolicyName="gateway-outbound-auth",
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "bedrock-agentcore:GetWorkloadAccessToken",
                                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                                    "bedrock-agentcore:GetResourceOauth2Token",
                                    "secretsmanager:GetSecretValue",
                                ],
                                "Resource": "*",
                            }
                        ],
                    }
                ),
            )
            self.created["gatewayRoleName"] = role_name
            self.created["gatewayRoleArn"] = role["Arn"]
            self.save()
            time.sleep(10)
        print(f"  Gateway role: {self.created['gatewayRoleArn']}")

        if "gatewayId" not in self.created:
            gateway = retry_iam_propagation(
                lambda: self.control.create_gateway(
                    name=self.prefix,
                    description="Consent portal demo: Google Calendar via 3LO",
                    roleArn=self.created["gatewayRoleArn"],
                    protocolType="MCP",
                    # AUTHORIZATION_CODE targets return the consent URL via MCP URL elicitation,
                    # which the gateway only performs on MCP 2025-11-25 or newer.
                    protocolConfiguration={
                        "mcp": {
                            "searchType": "SEMANTIC",
                            "supportedVersions": ["2025-11-25", "2025-06-18", "2025-03-26"],
                        }
                    },
                    authorizerType="CUSTOM_JWT",
                    authorizerConfiguration={
                        "customJWTAuthorizer": {
                            "discoveryUrl": self.discovery_url(),
                            "allowedClients": [self.created["apiClientId"], self.created["portalClientId"]],
                        }
                    },
                    exceptionLevel="DEBUG",
                    tags=TAGS,
                )
            )
            self.created["gatewayId"] = gateway["gatewayId"]
            self.created["gatewayArn"] = gateway["gatewayArn"]
            self.created["gatewayUrl"] = gateway["gatewayUrl"]
            self.save()
        gateway = wait_for(
            lambda: self.control.get_gateway(gatewayIdentifier=self.created["gatewayId"]),
            lambda g: g["status"] == "READY",
            description="gateway",
            failed={"FAILED", "CREATE_UNSUCCESSFUL"},
        )
        self.created["gatewayUrl"] = gateway["gatewayUrl"]
        self.save()
        print(f"  Gateway: {self.created['gatewayId']}  {self.created['gatewayUrl']}")

    # 3 ───────────────────────────────────────────────────────────────────────
    def cognito_provider_setup(self) -> None:
        step("Step 3: OAuth2 credential provider for Cognito (portal primary IdP)")
        if "cognitoProviderArn" not in self.created:
            client = self.cognito.describe_user_pool_client(
                UserPoolId=self.created["userPoolId"], ClientId=self.created["portalClientId"]
            )["UserPoolClient"]
            provider = self.control.create_oauth2_credential_provider(
                name=f"{self.prefix}-cognito",
                credentialProviderVendor="CustomOauth2",
                oauth2ProviderConfigInput={
                    "customOauth2ProviderConfig": {
                        "oauthDiscovery": {"discoveryUrl": self.discovery_url()},
                        "clientId": client["ClientId"],
                        "clientSecret": client["ClientSecret"],
                    }
                },
                tags=TAGS,
            )
            self.created["cognitoProviderName"] = provider["name"]
            self.created["cognitoProviderArn"] = provider["credentialProviderArn"]
            self.save()
        print(f"  Provider: {self.created['cognitoProviderArn']}")

    # 4 ───────────────────────────────────────────────────────────────────────
    def portal_role_setup(self) -> None:
        step("Step 4: Consent portal execution role")
        role_name = f"{self.prefix}-portal-role"
        if "portalRoleArn" not in self.created:
            # The portal ARN is unknown until the portal exists; the SourceArn condition is
            # tightened in step 5.
            trust = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ConsentPortalAssumeRolePolicy",
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {"StringEquals": {"aws:SourceAccount": self.account}},
                    }
                ],
            }
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
            )["Role"]
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:GetGateway",
                            "bedrock-agentcore:GetGatewayTarget",
                            "bedrock-agentcore:ListGatewayTargets",
                        ],
                        "Resource": self.created["gatewayArn"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:GetOauth2CredentialProvider",
                            "bedrock-agentcore:ListOauth2CredentialProviders",
                        ],
                        # The developer guide scopes this to token-vault/default/oauth2credentialprovider/*,
                        # but the portal's GetOauth2CredentialProvider call is evaluated against the vault
                        # itself and is denied with that pattern (verified 2026-09 via CloudTrail). Grant
                        # the vault and everything under it.
                        "Resource": [
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default",
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:token-vault/default/*",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:CompleteResourceTokenAuth",
                            "bedrock-agentcore:GetResourceOauth2Token",
                            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": [
                            f"arn:aws:secretsmanager:{self.region}:{self.account}:"
                            "secret:bedrock-agentcore-identity!default/oauth2/*"
                        ],
                        "Condition": {
                            "StringEquals": {
                                "aws:ResourceTag/aws:secretsmanager:owningService": "bedrock-agentcore-identity"
                            }
                        },
                    },
                ],
            }
            self.iam.put_role_policy(
                RoleName=role_name, PolicyName="consent-portal", PolicyDocument=json.dumps(policy)
            )
            self.created["portalRoleName"] = role_name
            self.created["portalRoleArn"] = role["Arn"]
            self.save()
            time.sleep(10)
        print(f"  Execution role: {self.created['portalRoleArn']}")

    # 5 ───────────────────────────────────────────────────────────────────────
    def portal_setup(self) -> None:
        step("Step 5: Consent portal")
        if "consentPortalId" not in self.created:
            portal = retry_iam_propagation(
                lambda: self.control.create_consent_portal(
                    name=self.prefix,
                    description="Google Calendar consent portal demo",
                    executionRoleArn=self.created["portalRoleArn"],
                    idpConfig={
                        "credentialProviderArn": self.created["cognitoProviderArn"],
                        "scopes": PORTAL_SCOPES,
                    },
                    sources=[{"identifier": self.created["gatewayId"], "type": "agentcore-gateway"}],
                    tags=TAGS,
                )
            )
            self.created["consentPortalId"] = portal["consentPortalId"]
            self.created["consentPortalArn"] = portal["consentPortalArn"]
            self.save()
        portal = wait_for(
            lambda: self.control.get_consent_portal(consentPortalIdentifier=self.created["consentPortalId"]),
            lambda p: p["status"] == "ACTIVE",
            description="consent portal",
            failed={"FAILED", "UPDATE_FAILED"},
        )
        portal_url = portal["portalUrl"].rstrip("/")
        self.created["portalUrl"] = portal_url
        self.save()
        print(f"  Portal: {self.created['consentPortalId']}")
        print(f"  Portal URL: {portal_url}")

        if not self.created.get("portalCallbackRegistered"):
            client = self.cognito.describe_user_pool_client(
                UserPoolId=self.created["userPoolId"], ClientId=self.created["portalClientId"]
            )["UserPoolClient"]
            self.cognito.update_user_pool_client(
                UserPoolId=client["UserPoolId"],
                ClientId=client["ClientId"],
                ClientName=client["ClientName"],
                ExplicitAuthFlows=client["ExplicitAuthFlows"],
                SupportedIdentityProviders=client["SupportedIdentityProviders"],
                AllowedOAuthFlows=client["AllowedOAuthFlows"],
                AllowedOAuthScopes=client["AllowedOAuthScopes"],
                AllowedOAuthFlowsUserPoolClient=True,
                CallbackURLs=[f"{portal_url}/callback"],
            )
            self.created["portalCallbackRegistered"] = True
            self.save()
        print(f"  Cognito callback URL registered: {portal_url}/callback")

        if not self.created.get("portalRoleTrustTightened"):
            trust = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ConsentPortalAssumeRolePolicy",
                        "Effect": "Allow",
                        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": self.account},
                            "ArnLike": {"aws:SourceArn": self.created["consentPortalArn"]},
                        },
                    }
                ],
            }
            self.iam.update_assume_role_policy(
                RoleName=self.created["portalRoleName"], PolicyDocument=json.dumps(trust)
            )
            self.created["portalRoleTrustTightened"] = True
            self.save()
        print("  Execution role trust policy scoped to the portal ARN")

    # 6 ───────────────────────────────────────────────────────────────────────
    def google_provider_setup(self) -> bool:
        step("Step 6: GoogleOauth2 credential provider (outbound, Google Calendar)")
        if "googleProviderArn" not in self.created:
            dotenv.load_dotenv(ROOT / ".env", override=True)
            client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip().strip('"')
            client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip().strip('"')
            if not client_id or not client_secret:
                print("  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not found in .env.")
                print("  Copy .env.example to .env, fill in the Google OAuth client, then re-run deploy.py.")
                return False
            provider = self.control.create_oauth2_credential_provider(
                name=GOOGLE_PROVIDER_NAME,
                credentialProviderVendor="GoogleOauth2",
                oauth2ProviderConfigInput={
                    "googleOauth2ProviderConfig": {"clientId": client_id, "clientSecret": client_secret}
                },
                tags=TAGS,
            )
            self.created["googleProviderName"] = provider["name"]
            self.created["googleProviderArn"] = provider["credentialProviderArn"]
            self.created["googleCallbackUrl"] = provider["callbackUrl"]
            self.save()
        print(f"  Provider: {self.created['googleProviderArn']}")
        print(f"  Google redirect URI: {self.created['googleCallbackUrl']}")
        return True

    # 7 ───────────────────────────────────────────────────────────────────────
    def target_setup(self) -> None:
        step("Step 7: Gateway target (Google Calendar, AUTHORIZATION_CODE)")
        return_url = f"{self.created['portalUrl']}/connect/callback"
        if "targetId" not in self.created:
            target = self.control.create_gateway_target(
                gatewayIdentifier=self.created["gatewayId"],
                name=TARGET_NAME,
                description="Google Calendar (read-only) on behalf of the signed-in user",
                targetConfiguration={"mcp": {"openApiSchema": {"inlinePayload": OPENAPI_FILE.read_text()}}},
                credentialProviderConfigurations=[
                    {
                        "credentialProviderType": "OAUTH",
                        "credentialProvider": {
                            "oauthCredentialProvider": {
                                "providerArn": self.created["googleProviderArn"],
                                "grantType": "AUTHORIZATION_CODE",
                                "scopes": [CALENDAR_SCOPE],
                                "defaultReturnUrl": return_url,
                                # Ask Google for a refresh token so the vault can renew access tokens.
                                "customParameters": {"access_type": "offline", "prompt": "consent"},
                            }
                        },
                    }
                ],
            )
            self.created["targetId"] = target["targetId"]
            self.save()
        target = wait_for(
            lambda: self.control.get_gateway_target(
                gatewayIdentifier=self.created["gatewayId"], targetId=self.created["targetId"]
            ),
            lambda t: t["status"] == "READY",
            description="gateway target",
            failed={"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"},
        )
        print(f"  Target: {target['targetId']} ({target['status']})")
        print(f"  defaultReturnUrl: {return_url}")

    # ─────────────────────────────────────────────────────────────────────────
    def summary(self, complete: bool) -> None:
        step("Summary")
        user = self.created["testUser"]
        print(f"  Region / account : {self.region} / {self.account}")
        print(f"  Gateway URL      : {self.created.get('gatewayUrl')}")
        print(f"  Portal URL       : {self.created.get('portalUrl')}")
        print(f"  Test user        : {user['username']} / {user['password']}")
        if not complete:
            print("\n  Deployment is paused before the Google Calendar target. Re-run deploy.py to continue.")
            return
        print("\n  ACTION REQUIRED in Google Cloud Console (once, before the first Connect):")
        print("  APIs & Services -> Credentials -> your OAuth 2.0 Client -> Authorized redirect URIs, add")
        print(f"      {self.created['googleCallbackUrl']}")
        print("  Also enable the Google Calendar API and add your Google account as a test user.")
        print("\n  Next steps:")
        print(f"  1. Open {self.created['portalUrl']} in a browser and sign in as {user['username']}.")
        print(f"  2. On Connections, find '{TARGET_NAME}' and choose Connect; approve at Google.")
        print("     (new targets can take up to 5 minutes to appear)")
        print("  3. Run: python invoke.py   -> lists the next 7 days of events through the gateway")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=boto3.Session().region_name or "us-west-2")
    args = parser.parse_args()

    deployment = Deployment(args.region)
    print(f"Region: {deployment.region}\nAccount: {deployment.account}\nPrefix: {deployment.prefix}")
    deployment.cognito_setup()
    deployment.gateway_setup()
    deployment.cognito_provider_setup()
    deployment.portal_role_setup()
    deployment.portal_setup()
    complete = deployment.google_provider_setup()
    if complete:
        deployment.target_setup()
    deployment.summary(complete)
    sys.exit(0 if complete else 2)


if __name__ == "__main__":
    main()
