#!/bin/bash
# Set your variables
AGENT_NAME="claude_code_agent"
REGION=$(aws configure get region)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Replace placeholders in the JSON files
sed -i "s/\${REGION}/$REGION/g" assume-role-policy.json role-policy.json
sed -i "s/\${ACCOUNT_ID}/$ACCOUNT_ID/g" assume-role-policy.json role-policy.json

# Create the role
echo "Creating AgentCore execution role..."
aws iam create-role \
  --role-name "agentcore-${AGENT_NAME}-role" \
  --assume-role-policy-document file://assume-role-policy.json
echo "Created AgentCore execution role Arn: arn:aws:iam::${ACCOUNT_ID}:role/agentcore-${AGENT_NAME}-role"

# Attach the inline policy
aws iam put-role-policy \
  --role-name "agentcore-${AGENT_NAME}-role" \
  --policy-name "agentcore-bedrock-policy" \
  --policy-document file://role-policy.json

# Attach managed policy
aws iam attach-role-policy \
  --role-name "agentcore-${AGENT_NAME}-role"  \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess-AWSElasticBeanstalk

# Create ECR Repository
echo "Creating ECR repository..."
aws ecr create-repository \
    --repository-name bedrock_agentcore-${AGENT_NAME} \
    --image-scanning-configuration scanOnPush=true \
    --region us-west-2
echo "Created ECR repository: ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/bedrock_agentcore-${AGENT_NAME}"

# Create Elastic Beanstalk Service Role
echo "Creating aws-elasticbeanstalk-service-role..."
aws iam create-role \
  --role-name aws-elasticbeanstalk-service-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "elasticbeanstalk.amazonaws.com"
        },
        "Action": "sts:AssumeRole"
      }
    ]
  }' \
  --description "Service role for Elastic Beanstalk"

# Attach policies to service role
echo "Attaching policies to service role..."
aws iam attach-role-policy \
  --role-name aws-elasticbeanstalk-service-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSElasticBeanstalkEnhancedHealth

aws iam attach-role-policy \
  --role-name aws-elasticbeanstalk-service-role \
  --policy-arn arn:aws:iam::aws:policy/AWSElasticBeanstalkManagedUpdatesCustomerRolePolicy

# Create Elastic Beanstalk EC2 Role
echo "Creating aws-elasticbeanstalk-ec2-role..."
aws iam create-role \
  --role-name aws-elasticbeanstalk-ec2-role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": "ec2.amazonaws.com"
        },
        "Action": "sts:AssumeRole"
      }
    ]
  }' \
  --description "Allows EC2 instances to call AWS services on your behalf."

# Attach policies to EC2 role
echo "Attaching policies to EC2 role..."
aws iam attach-role-policy \
  --role-name aws-elasticbeanstalk-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AWSElasticBeanstalkMulticontainerDocker

aws iam attach-role-policy \
  --role-name aws-elasticbeanstalk-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AWSElasticBeanstalkWebTier

aws iam attach-role-policy \
  --role-name aws-elasticbeanstalk-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AWSElasticBeanstalkWorkerTier

echo "Setup complete!"
