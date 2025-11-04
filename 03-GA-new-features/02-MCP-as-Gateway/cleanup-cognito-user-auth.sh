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
