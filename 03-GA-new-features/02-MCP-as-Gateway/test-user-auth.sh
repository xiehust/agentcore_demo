#!/bin/bash
# Test script to authenticate user and get access token

# Source the environment file
if [ ! -f .cognito-user-auth.env ]; then
    echo "✗ Configuration file .cognito-user-auth.env not found"
    exit 1
fi

source .cognito-user-auth.env

echo "Testing user authentication..."
echo "Username: $USERNAME"
echo "Region: $REGION"

# Authenticate and get new token
TOKEN_RESPONSE=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$USERNAME",PASSWORD="$PASSWORD" \
  --region $REGION)

NEW_ACCESS_TOKEN=$(echo $TOKEN_RESPONSE | jq -r '.AuthenticationResult.AccessToken')

if [ "$NEW_ACCESS_TOKEN" != "null" ] && [ -n "$NEW_ACCESS_TOKEN" ]; then
  echo "✓ Authentication successful!"
  echo ""
  echo "Access Token:"
  echo $NEW_ACCESS_TOKEN
  echo ""
  echo "Token payload (decoded):"
  echo $NEW_ACCESS_TOKEN | cut -d'.' -f2 | base64 -d 2>/dev/null | jq .
else
  echo "✗ Authentication failed"
  echo $TOKEN_RESPONSE | jq
fi
