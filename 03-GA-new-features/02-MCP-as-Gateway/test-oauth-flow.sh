#!/bin/bash
# Script to demonstrate OAuth flow

# Source the environment file
if [ ! -f .cognito-user-auth.env ]; then
    echo "✗ Configuration file .cognito-user-auth.env not found"
    exit 1
fi

source .cognito-user-auth.env

echo "================================"
echo "OAuth Flow Information"
echo "================================"
echo ""
echo "Step 1: Direct user to Authorization URL"
echo "---------------------------------------"
echo "${AUTHORIZATION_URL}?client_id=${CLIENT_ID}&response_type=code&scope=openid+email+profile+${RESOURCE_SERVER_IDENTIFIER}/stream&redirect_uri=${REDIRECT_URL}"
echo ""
echo "Step 2: After user authorizes, they will be redirected to:"
echo "---------------------------------------"
echo "${REDIRECT_URL}?code=AUTHORIZATION_CODE"
echo ""
echo "Step 3: Exchange authorization code for tokens"
echo "---------------------------------------"
echo "POST $TOKEN_URL"
echo "Content-Type: application/x-www-form-urlencoded"
echo "Authorization: Basic \$(echo -n ${CLIENT_ID}:${CLIENT_SECRET} | base64)"
echo ""
echo "Body:"
echo "grant_type=authorization_code"
echo "code=AUTHORIZATION_CODE"
echo "redirect_uri=${REDIRECT_URL}"
echo ""
echo "Example curl command (replace AUTHORIZATION_CODE):"
echo "---------------------------------------"
echo "curl -X POST '$TOKEN_URL' \\"
echo "  -H 'Content-Type: application/x-www-form-urlencoded' \\"
echo "  -u '${CLIENT_ID}:${CLIENT_SECRET}' \\"
echo "  -d 'grant_type=authorization_code&code=AUTHORIZATION_CODE&redirect_uri=${REDIRECT_URL}'"
echo ""
echo "================================"
