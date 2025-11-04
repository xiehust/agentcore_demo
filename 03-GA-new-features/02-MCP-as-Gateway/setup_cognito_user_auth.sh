#!/bin/bash

REGION=$1

if [ -z "$REGION" ]; then
  echo "Usage: $0 <region>"
  echo "Example: $0 us-east-1"
  exit 1
fi

# Set default passwords
TEMP_PASSWORD="TempPass123!"
PERMANENT_PASSWORD="UserPass123!"

# QuickSight OAuth callback URL
REDIRECT_URL="https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback"

# 1. Create User Pool and capture Pool ID directly
echo "Creating User Pool..."
export POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "MyUserPool" \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}' \
  --region $REGION | jq -r '.UserPool.Id')

echo "✓ User Pool ID: $POOL_ID"

# 2. Create Domain for OAuth endpoints
echo "Creating Cognito Domain..."
DOMAIN_PREFIX="user-auth-$(date +%s)"
aws cognito-idp create-user-pool-domain \
  --domain $DOMAIN_PREFIX \
  --user-pool-id $POOL_ID \
  --region $REGION > /dev/null

echo "✓ Domain Prefix: $DOMAIN_PREFIX"

# Generate OAuth URLs
AUTHORIZATION_URL="https://${DOMAIN_PREFIX}.auth.${REGION}.amazoncognito.com/oauth2/authorize"
TOKEN_URL="https://${DOMAIN_PREFIX}.auth.${REGION}.amazoncognito.com/oauth2/token"
DISCOVERY_URL="https://cognito-idp.$REGION.amazonaws.com/$POOL_ID/.well-known/openid-configuration"

echo "✓ Authorization URL: $AUTHORIZATION_URL"
echo "✓ Token URL: $TOKEN_URL"

# 3. Create Resource Server for custom scopes
echo "Creating Resource Server for custom scopes..."
RESOURCE_SERVER_IDENTIFIER="mcp-api"
aws cognito-idp create-resource-server \
  --user-pool-id $POOL_ID \
  --identifier $RESOURCE_SERVER_IDENTIFIER \
  --name "MCP API Resource Server" \
  --scopes \
    ScopeName=stream,ScopeDescription="MCP stream access" \
  --region $REGION > /dev/null

echo "✓ Resource Server created: $RESOURCE_SERVER_IDENTIFIER"

# 4. Create App Client with OAuth support and custom scopes
echo "Creating OAuth App Client..."
export CLIENT_RESPONSE=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $POOL_ID \
  --client-name "MyOAuthClient" \
  --generate-secret \
  --callback-urls "$REDIRECT_URL" \
  --allowed-o-auth-flows "code" "implicit" \
  --allowed-o-auth-scopes "openid" "email" "profile" "${RESOURCE_SERVER_IDENTIFIER}/stream" \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers "COGNITO" \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --region $REGION)

export CLIENT_ID=$(echo $CLIENT_RESPONSE | jq -r '.UserPoolClient.ClientId')
export CLIENT_SECRET=$(echo $CLIENT_RESPONSE | jq -r '.UserPoolClient.ClientSecret')

echo "✓ Client ID: $CLIENT_ID"
echo "✓ Client Secret: $CLIENT_SECRET"

# 5. Create User
echo "Creating test user..."
aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username "testuser" \
  --temporary-password "$TEMP_PASSWORD" \
  --user-attributes Name=email,Value=testuser@example.com Name=email_verified,Value=true \
  --region $REGION \
  --message-action SUPPRESS > /dev/null

echo "✓ Test user created with username: testuser"

# 6. Set Permanent Password
echo "Setting permanent password..."
aws cognito-idp admin-set-user-password \
  --user-pool-id $POOL_ID \
  --username "testuser" \
  --password "$PERMANENT_PASSWORD" \
  --region $REGION \
  --permanent > /dev/null

echo "✓ Permanent password set"

# 7. Test user authentication to get bearer token
echo "Testing user authentication..."
export BEARER_TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME='testuser',PASSWORD="$PERMANENT_PASSWORD" \
  --region $REGION | jq -r '.AuthenticationResult.AccessToken')

if [ "$BEARER_TOKEN" != "null" ] && [ -n "$BEARER_TOKEN" ]; then
  echo "✓ User authentication successful!"
  echo "Bearer Token: ${BEARER_TOKEN:0:50}..."
else
  echo "✗ Failed to authenticate user"
fi

# Output the required values
echo ""
echo "================================"
echo "OAuth User Authentication Configuration"
echo "================================"
echo ""
echo "Pool ID: $POOL_ID"
echo "Region: $REGION"
echo "Domain Prefix: $DOMAIN_PREFIX"
echo ""
echo "OAuth Endpoints:"
echo "  Authorization URL: $AUTHORIZATION_URL"
echo "  Token URL: $TOKEN_URL"
echo "  Discovery URL: $DISCOVERY_URL"
echo ""
echo "App Client Configuration:"
echo "  Client ID: $CLIENT_ID"
echo "  Client Secret: $CLIENT_SECRET"
echo "  Redirect URL: $REDIRECT_URL"
echo ""
echo "OAuth Scopes:"
echo "  - openid"
echo "  - email"
echo "  - profile"
echo "  - mcp:stream (custom scope)"
echo ""
echo "Resource Server:"
echo "  Identifier: $RESOURCE_SERVER_IDENTIFIER"
echo ""
echo "Test User Credentials:"
echo "  Username: testuser"
echo "  Password: $PERMANENT_PASSWORD"
echo "  Email: testuser@example.com"
echo ""
echo "Bearer Token (from direct auth):"
echo "  ${BEARER_TOKEN:0:50}..."
echo ""

# Write configuration to file
cat > .cognito-user-auth.env << EOF
================================
OAuth User Authentication Configuration
================================

Pool ID: $POOL_ID
Region: $REGION
Domain Prefix: $DOMAIN_PREFIX

OAuth Endpoints:
  Authorization URL: $AUTHORIZATION_URL
  Token URL: $TOKEN_URL
  Discovery URL: $DISCOVERY_URL

App Client Configuration:
  Client ID: $CLIENT_ID
  Client Secret: $CLIENT_SECRET
  Redirect URL: $REDIRECT_URL

OAuth Scopes:
  - openid
  - email
  - profile
  - mcp:stream (custom scope)

Resource Server:
  Identifier: $RESOURCE_SERVER_IDENTIFIER

Test User Credentials:
  Username: testuser
  Password: $PERMANENT_PASSWORD
  Email: testuser@example.com

Bearer Token (from direct auth):
  ${BEARER_TOKEN:0:50}...

================================
Raw Configuration (for scripts)
================================
POOL_ID=$POOL_ID
REGION=$REGION
DOMAIN_PREFIX=$DOMAIN_PREFIX
AUTHORIZATION_URL=$AUTHORIZATION_URL
TOKEN_URL=$TOKEN_URL
DISCOVERY_URL=$DISCOVERY_URL
CLIENT_ID=$CLIENT_ID
CLIENT_SECRET=$CLIENT_SECRET
REDIRECT_URL=$REDIRECT_URL
RESOURCE_SERVER_IDENTIFIER=$RESOURCE_SERVER_IDENTIFIER
USERNAME=testuser
PASSWORD=$PERMANENT_PASSWORD
BEARER_TOKEN=$BEARER_TOKEN
EOF

echo "✓ Configuration saved to .cognito-user-auth.env"

# Create a cleanup script
cat > cleanup-cognito-user-auth.sh << 'EOF'
#!/bin/bash
echo "Cleaning up Cognito User Auth resources..."

# Source the environment file to get variables
if [ -f .cognito-user-auth.env ]; then
    source .cognito-user-auth.env
fi

if [ -n "$POOL_ID" ] && [ -n "$REGION" ] && [ -n "$DOMAIN_PREFIX" ]; then
    echo "Deleting domain..."
    aws cognito-idp delete-user-pool-domain --domain $DOMAIN_PREFIX --user-pool-id $POOL_ID --region $REGION 2>/dev/null
    sleep 2
    echo "Deleting user pool..."
    aws cognito-idp delete-user-pool --user-pool-id $POOL_ID --region $REGION 2>/dev/null
    echo "✓ Resources deleted"
else
    echo "✗ Could not find required variables in .cognito-user-auth.env"
fi

rm -f .cognito-user-auth.env cleanup-cognito-user-auth.sh test-user-auth.sh test-oauth-flow.sh
echo "✓ Cleanup files removed"
EOF

chmod +x cleanup-cognito-user-auth.sh
echo "✓ Cleanup script created: ./cleanup-cognito-user-auth.sh"

# Create test script for direct user authentication
cat > test-user-auth.sh << 'EOF'
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
EOF

chmod +x test-user-auth.sh
echo "✓ User auth test script created: ./test-user-auth.sh"

# Create OAuth flow test script
cat > test-oauth-flow.sh << 'EOF'
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
EOF

chmod +x test-oauth-flow.sh
echo "✓ OAuth flow test script created: ./test-oauth-flow.sh"

echo ""
echo "================================================"
echo "Setup complete! Use these files:"
echo "  - .cognito-user-auth.env: Full configuration details"
echo "  - cleanup-cognito-user-auth.sh: Delete all resources"  
echo "  - test-user-auth.sh: Test direct user authentication"
echo "  - test-oauth-flow.sh: View OAuth flow instructions"
echo "================================================"
echo ""
echo "To test OAuth flow:"
echo "  1. Run './test-oauth-flow.sh' to see OAuth endpoints"
echo "  2. Visit the Authorization URL in a browser"
echo "  3. Use the authorization code to exchange for tokens"
echo ""
echo "To test direct authentication:"
echo "  Run './test-user-auth.sh'"
