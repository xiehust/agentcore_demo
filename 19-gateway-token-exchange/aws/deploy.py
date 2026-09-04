#!/usr/bin/env python3
"""Deploy and verify a real AgentCore Gateway OBO token-exchange demo."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_SOURCE = ROOT / "aws" / "lambda_function.py"
STATE_FILE = ROOT / ".deployment.json"
SERVICE = "bedrock-agentcore-control"
SIGNING_SERVICE = "bedrock-agentcore"


def run(command: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"Command failed: {' '.join(command[:3])}\n{completed.stderr.strip()}")
    return completed.stdout


def aws_json(profile: str, region: str, service: str, operation: str, *args: str) -> dict[str, Any]:
    command = [
        "aws",
        service,
        operation,
        *args,
        "--region",
        region,
        "--profile",
        profile,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    output = run(command)
    return json.loads(output) if output.strip() else {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def export_credentials(profile: str) -> dict[str, str]:
    return json.loads(
        run(["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"])
    )


def sigv4_post(
    profile: str,
    region: str,
    path: str,
    payload: dict[str, Any],
    *,
    method: str = "POST",
) -> dict[str, Any]:
    credentials = export_credentials(profile)
    host = f"bedrock-agentcore-control.{region}.amazonaws.com"
    endpoint = f"https://{host}{path}"
    body = json.dumps(payload, separators=(",", ":")).encode()
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-date": amz_date,
    }
    session_token = credentials.get("SessionToken")
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{key}:{headers[key].strip()}\n" for key in signed_header_names)
    canonical_request = "\n".join(
        (
            method,
            urllib.parse.quote(path, safe="/-_.~"),
            "",
            canonical_headers,
            ";".join(signed_header_names),
            payload_hash,
        )
    )
    credential_scope = f"{date_stamp}/{region}/{SIGNING_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )

    def sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    date_key = sign(("AWS4" + credentials["SecretAccessKey"]).encode(), date_stamp)
    region_key = sign(date_key, region)
    service_key = sign(region_key, SIGNING_SERVICE)
    signing_key = sign(service_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={credentials['AccessKeyId']}/{credential_scope}, "
        f"SignedHeaders={';'.join(signed_header_names)}, Signature={signature}"
    )
    request_headers = {**headers, "authorization": authorization}
    request = urllib.request.Request(endpoint, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode()
        raise RuntimeError(f"AgentCore API {path} failed ({exc.code}): {details}") from exc


def wait_for(
    read: Any,
    ready: Any,
    *,
    description: str,
    attempts: int = 60,
    delay: int = 5,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(attempts):
        last = read()
        if ready(last):
            return last
        if last.get("status") in {"FAILED", "CREATE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL"}:
            raise RuntimeError(f"{description} failed: {json.dumps(last)}")
        time.sleep(delay)
    raise TimeoutError(f"Timed out waiting for {description}: {json.dumps(last)}")


def http_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], Any]:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode()
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            events = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
            payload = events[-1]
        else:
            payload = json.loads(raw) if raw else {}
        return payload, response.headers


def invoke_lambda_action(
    profile: str,
    region: str,
    function_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        output_path = Path(temporary) / "lambda-response.json"
        metadata = aws_json(
            profile,
            region,
            "lambda",
            "invoke",
            "--function-name",
            function_name,
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            json.dumps(payload),
            str(output_path),
        )
        if metadata.get("FunctionError"):
            raise RuntimeError(f"Lambda action failed: {output_path.read_text()}")
        envelope = json.loads(output_path.read_text())
    if envelope.get("statusCode") != 200:
        raise RuntimeError(f"Lambda action returned {envelope.get('statusCode')}: {envelope.get('body')}")
    return json.loads(envelope["body"])


def gateway_call(url: str, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    payload, _ = http_json(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    return payload


def deploy(profile: str, region: str) -> dict[str, Any]:
    if STATE_FILE.exists():
        raise RuntimeError(f"Deployment state already exists: {STATE_FILE}; clean it up before redeploying")
    identity = aws_json(profile, region, "sts", "get-caller-identity")
    account = identity["Account"]
    suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    prefix = f"agentcore-obo-demo-{suffix}"
    state: dict[str, Any] = {
        "profile": profile,
        "region": region,
        "account": account,
        "prefix": prefix,
        "created": {},
    }
    save_state(state)

    key = aws_json(
        profile,
        region,
        "kms",
        "create-key",
        "--description",
        f"{prefix} JWT signing key",
        "--key-usage",
        "SIGN_VERIFY",
        "--customer-master-key-spec",
        "RSA_2048",
        "--tags",
        f"TagKey=Project,TagValue=AgentCoreOBOdemo",
    )["KeyMetadata"]
    state["created"]["kmsKeyArn"] = key["Arn"]
    alias = f"alias/{prefix}"
    aws_json(profile, region, "kms", "create-alias", "--alias-name", alias, "--target-key-id", key["KeyId"])
    state["created"]["kmsAlias"] = alias
    save_state(state)

    role_name = f"{prefix}-lambda-role"
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    role = aws_json(
        profile,
        region,
        "iam",
        "create-role",
        "--role-name",
        role_name,
        "--assume-role-policy-document",
        json.dumps(trust),
        "--tags",
        "Key=Project,Value=AgentCoreOBOdemo",
    )["Role"]
    state["created"]["lambdaRoleName"] = role_name
    state["created"]["lambdaRoleArn"] = role["Arn"]
    save_state(state)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{prefix}:*",
            },
            {
                "Effect": "Allow",
                "Action": ["kms:Sign", "kms:Verify", "kms:GetPublicKey"],
                "Resource": key["Arn"],
            },
        ],
    }
    aws_json(
        profile,
        region,
        "iam",
        "put-role-policy",
        "--role-name",
        role_name,
        "--policy-name",
        "agentcore-obo-demo-runtime",
        "--policy-document",
        json.dumps(policy),
    )

    gateway_role_name = f"{prefix}-gateway-role"
    gateway_trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"
                    },
                },
            }
        ],
    }
    gateway_role = aws_json(
        profile,
        region,
        "iam",
        "create-role",
        "--role-name",
        gateway_role_name,
        "--assume-role-policy-document",
        json.dumps(gateway_trust),
        "--tags",
        "Key=Project,Value=AgentCoreOBOdemo",
    )["Role"]
    state["created"]["gatewayRoleName"] = gateway_role_name
    state["created"]["gatewayRoleArn"] = gateway_role["Arn"]
    save_state(state)
    gateway_policy = {
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
    aws_json(
        profile,
        region,
        "iam",
        "put-role-policy",
        "--role-name",
        gateway_role_name,
        "--policy-name",
        "agentcore-obo-demo-gateway",
        "--policy-document",
        json.dumps(gateway_policy),
    )

    client_id = "agentcore-obo-demo-client"
    client_secret = secrets.token_urlsafe(36)
    function_name = prefix
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "function.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            package.write(LAMBDA_SOURCE, "lambda_function.py")
        environment = {
            "Variables": {
                "KMS_KEY_ARN": key["Arn"],
                "KEY_KID": prefix,
                "ISSUER": "https://placeholder.invalid",
                "CLIENT_ID": client_id,
                "CLIENT_SECRET": client_secret,
            }
        }
        last_error: Exception | None = None
        for _ in range(12):
            try:
                function = aws_json(
                    profile,
                    region,
                    "lambda",
                    "create-function",
                    "--function-name",
                    function_name,
                    "--runtime",
                    "python3.12",
                    "--role",
                    role["Arn"],
                    "--handler",
                    "lambda_function.handler",
                    "--zip-file",
                    f"fileb://{archive}",
                    "--timeout",
                    "20",
                    "--memory-size",
                    "256",
                    "--environment",
                    json.dumps(environment),
                    "--tags",
                    "Project=AgentCoreOBOdemo",
                )
                break
            except RuntimeError as exc:
                last_error = exc
                time.sleep(5)
        else:
            raise RuntimeError(f"Could not create Lambda after IAM propagation: {last_error}")
    state["created"]["lambdaFunctionName"] = function_name
    state["created"]["lambdaFunctionArn"] = function["FunctionArn"]
    save_state(state)

    run(
        [
            "aws",
            "lambda",
            "wait",
            "function-active-v2",
            "--function-name",
            function_name,
            "--region",
            region,
            "--profile",
            profile,
        ]
    )
    api = aws_json(
        profile,
        region,
        "apigatewayv2",
        "create-api",
        "--name",
        f"{prefix}-http-api",
        "--protocol-type",
        "HTTP",
        "--target",
        function["FunctionArn"],
        "--tags",
        "Project=AgentCoreOBOdemo",
    )
    api_id = api["ApiId"]
    issuer = api["ApiEndpoint"].rstrip("/")
    aws_json(
        profile,
        region,
        "lambda",
        "add-permission",
        "--function-name",
        function_name,
        "--statement-id",
        "ApiGatewayInvoke",
        "--action",
        "lambda:InvokeFunction",
        "--principal",
        "apigateway.amazonaws.com",
        "--source-arn",
        f"arn:aws:execute-api:{region}:{account}:{api_id}/*/*",
    )
    environment = {
        "Variables": {
            "KMS_KEY_ARN": key["Arn"],
            "KEY_KID": prefix,
            "ISSUER": issuer,
            "CLIENT_ID": client_id,
            "CLIENT_SECRET": client_secret,
        }
    }
    aws_json(
        profile,
        region,
        "lambda",
        "update-function-configuration",
        "--function-name",
        function_name,
        "--environment",
        json.dumps(environment),
    )
    run(
        [
            "aws",
            "lambda",
            "wait",
            "function-updated-v2",
            "--function-name",
            function_name,
            "--region",
            region,
            "--profile",
            profile,
        ]
    )
    state["created"]["apiGatewayId"] = api_id
    state["created"]["apiEndpoint"] = issuer
    state["created"]["issuer"] = issuer
    save_state(state)

    for _ in range(30):
        try:
            health, _ = http_json(f"{issuer}/health")
            if health.get("status") == "ok":
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("API Gateway endpoint did not become healthy")

    provider_name = f"{prefix}-provider"
    provider = sigv4_post(
        profile,
        region,
        "/identities/CreateOauth2CredentialProvider",
        {
            "name": provider_name,
            "credentialProviderVendor": "CustomOauth2",
            "oauth2ProviderConfigInput": {
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": {"discoveryUrl": f"{issuer}/.well-known/openid-configuration"},
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
                    "onBehalfOfTokenExchangeConfig": {
                        "grantType": "TOKEN_EXCHANGE",
                        "tokenExchangeGrantTypeConfig": {"actorTokenContent": "NONE"},
                    },
                }
            },
            "tags": {"Project": "AgentCoreOBOdemo"},
        },
    )
    provider_arn = provider["credentialProviderArn"]
    state["created"]["oauthProviderName"] = provider_name
    state["created"]["oauthProviderArn"] = provider_arn
    save_state(state)

    gateway_name = f"{prefix}-gateway"
    gateway_role_arn = gateway_role["Arn"]
    gateway = aws_json(
        profile,
        region,
        SERVICE,
        "create-gateway",
        "--cli-input-json",
        json.dumps(
            {
                "name": gateway_name,
                "description": "Real RFC 8693 token exchange demo",
                "roleArn": gateway_role_arn,
                "protocolType": "MCP",
                "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-06-18"]}},
                "authorizerType": "CUSTOM_JWT",
                "authorizerConfiguration": {
                    "customJWTAuthorizer": {
                        "discoveryUrl": f"{issuer}/.well-known/openid-configuration",
                        "allowedAudience": ["agentcore-gateway-demo"],
                    }
                },
                "exceptionLevel": "DEBUG",
                "tags": {"Project": "AgentCoreOBOdemo"},
            }
        ),
    )
    gateway_id = gateway["gatewayId"]
    state["created"]["gatewayId"] = gateway_id
    state["created"]["gatewayArn"] = gateway["gatewayArn"]
    state["created"]["gatewayUrl"] = gateway["gatewayUrl"]
    save_state(state)
    wait_for(
        lambda: aws_json(profile, region, SERVICE, "get-gateway", "--gateway-identifier", gateway_id),
        lambda item: item.get("status") == "READY",
        description="Gateway",
    )

    target = sigv4_post(
        profile,
        region,
        f"/gateways/{gateway_id}/targets/",
        {
            "name": "token-exchange-mcp-target",
            "description": "MCP target protected by an RFC 8693 exchanged token",
            "targetConfiguration": {
                "mcp": {
                    "mcpServer": {
                        "endpoint": f"{issuer}/mcp",
                        "listingMode": "DYNAMIC",
                    }
                }
            },
            "credentialProviderConfigurations": [
                {
                    "credentialProviderType": "OAUTH",
                    "credentialProvider": {
                        "oauthCredentialProvider": {
                            "providerArn": provider_arn,
                            "scopes": ["mcp:invoke"],
                            "grantType": "TOKEN_EXCHANGE",
                            "customParameters": {
                                "audience": "mcp-server",
                                "subject_token_type": ACCESS_TOKEN_TYPE,
                            },
                        }
                    },
                }
            ],
        },
    )
    target_id = target["targetId"]
    state["created"]["targetId"] = target_id
    save_state(state)
    wait_for(
        lambda: aws_json(
            profile,
            region,
            SERVICE,
            "get-gateway-target",
            "--gateway-identifier",
            gateway_id,
            "--target-id",
            target_id,
        ),
        lambda item: item.get("status") == "READY",
        description="Gateway target",
    )

    token_response = invoke_lambda_action(
        profile,
        region,
        function_name,
        {"action": "issue_user_token", "subject": "alice"},
    )
    user_token = token_response["access_token"]
    initialize = gateway_call(
        gateway["gatewayUrl"],
        user_token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agentcore-obo-demo-client", "version": "1.0.0"},
        },
    )
    tools = gateway_call(gateway["gatewayUrl"], user_token, "tools/list", {})
    tool_name = tools["result"]["tools"][0]["name"]
    whoami = gateway_call(
        gateway["gatewayUrl"],
        user_token,
        "tools/call",
        {"name": tool_name, "arguments": {}},
    )
    state["verification"] = {
        "initialize": initialize,
        "tools": tools,
        "whoami": whoami,
        "verifiedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save_state(state)
    return state


ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default="us-west-2")
    args = parser.parse_args()
    state = deploy(args.profile, args.region)
    safe_output = {
        "account": state["account"],
        "region": state["region"],
        "gatewayUrl": state["created"]["gatewayUrl"],
        "apiEndpoint": state["created"]["apiEndpoint"],
        "targetId": state["created"]["targetId"],
        "whoami": state["verification"]["whoami"],
        "stateFile": str(STATE_FILE),
    }
    print(json.dumps(safe_output, indent=2))


if __name__ == "__main__":
    main()
