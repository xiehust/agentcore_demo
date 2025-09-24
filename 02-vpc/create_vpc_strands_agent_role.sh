#!/bin/bash

# Script to create vpc_strands_agent IAM role for Bedrock AgentCore Runtime
# Usage: ./create_vpc_strands_agent_role.sh [ACCOUNT_ID] [REGION]

set -e

# Get account ID and region from parameters or defaults
ACCOUNT_ID=${1:-$(aws sts get-caller-identity --query Account --output text)}
REGION=${2:-us-west-2}
ROLE_NAME="vpc_strands_agent"

echo "Creating IAM role: $ROLE_NAME"
echo "Account ID: $ACCOUNT_ID"
echo "Region: $REGION"

# Create trust policy document
cat > trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AssumeRolePolicy",
            "Effect": "Allow",
            "Principal": {
                "Service": "bedrock-agentcore.amazonaws.com"
            },
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {
                    "aws:SourceAccount": "$ACCOUNT_ID"
                },
                "ArnLike": {
                    "aws:SourceArn": "arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:*"
                }
            }
        }
    ]
}
EOF

# Create the IAM role
echo "Creating IAM role..."
aws iam create-role \
    --role-name $ROLE_NAME \
    --assume-role-policy-document file://trust-policy.json \
    --description "IAM role for Bedrock AgentCore Runtime"

# Attach managed policies
echo "Attaching managed policies..."
aws iam attach-role-policy \
    --role-name $ROLE_NAME \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess

aws iam attach-role-policy \
    --role-name $ROLE_NAME \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockAgentCoreMemoryBedrockModelInferenceExecutionRolePolicy

aws iam attach-role-policy \
    --role-name $ROLE_NAME \
    --policy-arn arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess

# Create ECR permissions policy
cat > ecr-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer"
            ],
            "Resource": "*"
        }
    ]
}
EOF

# Attach ECR permissions policy
echo "Adding ECR permissions..."
aws iam put-role-policy \
    --role-name $ROLE_NAME \
    --policy-name ECRAccessPolicy \
    --policy-document file://ecr-policy.json

# Clean up temporary files
rm trust-policy.json ecr-policy.json

echo "Successfully created IAM role: arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME"
echo "Role is ready for use with Bedrock AgentCore Runtime"
