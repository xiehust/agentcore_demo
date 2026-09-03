#!/usr/bin/env bash
# Deploy the GitHub App token broker Lambda and restrict invocation to the AgentCore runtime execution role.
#
# Usage:
#   REGION=us-east-2 GITHUB_APP_ID=123456 GITHUB_INSTALLATION_ID=7890123 \
#   APP_KEY_PEM=./github-app.private-key.pem RUNTIME_EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/<runtime-role> \
#   ALLOWED_REPOS=org/repo1,org/repo2 ./deploy.sh
set -euo pipefail
: "${REGION:?}" "${GITHUB_APP_ID:?}" "${GITHUB_INSTALLATION_ID:?}" "${APP_KEY_PEM:?}" "${RUNTIME_EXEC_ROLE_ARN:?}"
FN=${FN:-github-app-token-broker}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
HERE=$(cd "$(dirname "$0")" && pwd)
BUILD=$(mktemp -d)

# 1. Private key -> Secrets Manager (never leaves this account boundary afterwards)
SECRET_ARN=$(aws secretsmanager create-secret --region "$REGION" --name "${FN}/app-private-key" \
  --secret-string "file://${APP_KEY_PEM}" --query ARN --output text 2>/dev/null \
  || aws secretsmanager describe-secret --region "$REGION" --secret-id "${FN}/app-private-key" --query ARN --output text)

# 2. Lambda role: read that one secret + logs
cat >"$BUILD/trust.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
ROLE_ARN=$(aws iam create-role --role-name "${FN}-role" --assume-role-policy-document "file://$BUILD/trust.json" --query Role.Arn --output text 2>/dev/null \
  || aws iam get-role --role-name "${FN}-role" --query Role.Arn --output text)
aws iam attach-role-policy --role-name "${FN}-role" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam put-role-policy --role-name "${FN}-role" --policy-name read-app-key --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"${SECRET_ARN}\"}]}"
sleep 8  # IAM propagation

# 3. Package with PyJWT[crypto]
pip install --quiet --target "$BUILD/pkg" "PyJWT[crypto]" --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12 --implementation cp
cp "$HERE/lambda_function.py" "$BUILD/pkg/"
(cd "$BUILD/pkg" && zip -qr ../broker.zip .)

ENV="Variables={GITHUB_APP_ID=${GITHUB_APP_ID},GITHUB_INSTALLATION_ID=${GITHUB_INSTALLATION_ID},GITHUB_APP_KEY_SECRET_ARN=${SECRET_ARN},ALLOWED_REPOS=${ALLOWED_REPOS:-}}"
aws lambda create-function --region "$REGION" --function-name "$FN" --runtime python3.12 --architectures arm64 \
  --role "$ROLE_ARN" --handler lambda_function.lambda_handler --zip-file "fileb://$BUILD/broker.zip" \
  --timeout 20 --environment "$ENV" >/dev/null 2>&1 \
  || { aws lambda update-function-code --region "$REGION" --function-name "$FN" --zip-file "fileb://$BUILD/broker.zip" >/dev/null; \
       aws lambda wait function-updated --region "$REGION" --function-name "$FN"; \
       aws lambda update-function-configuration --region "$REGION" --function-name "$FN" --environment "$ENV" >/dev/null; }

# 4. Only the runtime execution role may invoke
aws lambda add-permission --region "$REGION" --function-name "$FN" --statement-id agentcore-runtime \
  --action lambda:InvokeFunction --principal "$RUNTIME_EXEC_ROLE_ARN" >/dev/null 2>&1 || true

echo "BROKER_FUNCTION_ARN=arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FN}"
echo "Grant the runtime execution role: lambda:InvokeFunction on that ARN, then in the sandbox:"
echo "  export GIT_ASKPASS=\$PWD/demo/secretless_auth/git_askpass.py GIT_TERMINAL_PROMPT=0 TOKEN_SOURCE=broker BROKER_FUNCTION_ARN=..."
