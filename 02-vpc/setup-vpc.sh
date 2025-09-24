#!/bin/bash

# =============================================================================
# Amazon Bedrock AgentCore VPC Network Setup Script
# 创建包含2个私有子网的高可用VPC架构
# =============================================================================

set -e  # 遇到错误时退出

# 配置变量 - 根据需要修改这些值
VPC_NAME="agentcore-vpc"
VPC_CIDR="10.0.0.0/16"
REGION="us-west-2"
AZ1="${REGION}a"
AZ2="${REGION}b"

# 子网CIDR配置
PUBLIC_SUBNET_1_CIDR="10.0.1.0/24"
PUBLIC_SUBNET_2_CIDR="10.0.2.0/24"
PRIVATE_SUBNET_1_CIDR="10.0.11.0/24"
PRIVATE_SUBNET_2_CIDR="10.0.12.0/24"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查AWS CLI是否配置
check_aws_cli() {
    log_info "检查AWS CLI配置..."
    if ! aws sts get-caller-identity > /dev/null 2>&1; then
        log_error "AWS CLI未正确配置。请运行 'aws configure' 进行配置。"
        exit 1
    fi
    log_success "AWS CLI配置正常"
}

# 创建VPC
create_vpc() {
    log_info "创建VPC..."
    VPC_ID=$(aws ec2 create-vpc \
        --cidr-block $VPC_CIDR \
        --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$VPC_NAME}]" \
        --query 'Vpc.VpcId' \
        --output text)
    
    log_success "VPC创建成功: $VPC_ID"
    
    # 启用DNS主机名和DNS解析
    aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames
    aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-support
    log_success "VPC DNS设置完成"
}

# 创建Internet网关
create_internet_gateway() {
    log_info "创建Internet网关..."
    IGW_ID=$(aws ec2 create-internet-gateway \
        --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${VPC_NAME}-igw}]" \
        --query 'InternetGateway.InternetGatewayId' \
        --output text)
    
    log_success "Internet网关创建成功: $IGW_ID"
    
    # 附加到VPC
    aws ec2 attach-internet-gateway \
        --internet-gateway-id $IGW_ID \
        --vpc-id $VPC_ID
    
    log_success "Internet网关已附加到VPC"
}

# 创建子网
create_subnets() {
    log_info "创建子网..."
    
    # 公有子网1
    PUBLIC_SUBNET_1_ID=$(aws ec2 create-subnet \
        --vpc-id $VPC_ID \
        --cidr-block $PUBLIC_SUBNET_1_CIDR \
        --availability-zone $AZ1 \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-public-subnet-1a}]" \
        --query 'Subnet.SubnetId' \
        --output text)
    log_success "公有子网1创建成功: $PUBLIC_SUBNET_1_ID"
    
    # 公有子网2
    PUBLIC_SUBNET_2_ID=$(aws ec2 create-subnet \
        --vpc-id $VPC_ID \
        --cidr-block $PUBLIC_SUBNET_2_CIDR \
        --availability-zone $AZ2 \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-public-subnet-1b}]" \
        --query 'Subnet.SubnetId' \
        --output text)
    log_success "公有子网2创建成功: $PUBLIC_SUBNET_2_ID"
    
    # 私有子网1
    PRIVATE_SUBNET_1_ID=$(aws ec2 create-subnet \
        --vpc-id $VPC_ID \
        --cidr-block $PRIVATE_SUBNET_1_CIDR \
        --availability-zone $AZ1 \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-private-subnet-1a}]" \
        --query 'Subnet.SubnetId' \
        --output text)
    log_success "私有子网1创建成功: $PRIVATE_SUBNET_1_ID"
    
    # 私有子网2
    PRIVATE_SUBNET_2_ID=$(aws ec2 create-subnet \
        --vpc-id $VPC_ID \
        --cidr-block $PRIVATE_SUBNET_2_CIDR \
        --availability-zone $AZ2 \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${VPC_NAME}-private-subnet-1b}]" \
        --query 'Subnet.SubnetId' \
        --output text)
    log_success "私有子网2创建成功: $PRIVATE_SUBNET_2_ID"
    
    # 启用公有子网的自动分配公网IP
    aws ec2 modify-subnet-attribute --subnet-id $PUBLIC_SUBNET_1_ID --map-public-ip-on-launch
    aws ec2 modify-subnet-attribute --subnet-id $PUBLIC_SUBNET_2_ID --map-public-ip-on-launch
    log_success "公有子网自动分配公网IP已启用"
}

# 创建弹性IP和NAT网关
create_nat_gateways() {
    log_info "创建弹性IP..."
    
    # 弹性IP 1
    EIP_1_ALLOC_ID=$(aws ec2 allocate-address \
        --domain vpc \
        --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${VPC_NAME}-nat-eip-1a}]" \
        --query 'AllocationId' \
        --output text)
    log_success "弹性IP 1创建成功: $EIP_1_ALLOC_ID"
    
    # 弹性IP 2
    EIP_2_ALLOC_ID=$(aws ec2 allocate-address \
        --domain vpc \
        --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${VPC_NAME}-nat-eip-1b}]" \
        --query 'AllocationId' \
        --output text)
    log_success "弹性IP 2创建成功: $EIP_2_ALLOC_ID"
    
    log_info "创建NAT网关..."
    
    # NAT网关1
    NAT_GW_1_ID=$(aws ec2 create-nat-gateway \
        --subnet-id $PUBLIC_SUBNET_1_ID \
        --allocation-id $EIP_1_ALLOC_ID \
        --query 'NatGateway.NatGatewayId' \
        --output text)
    log_success "NAT网关1创建成功: $NAT_GW_1_ID"
    
    # NAT网关2
    NAT_GW_2_ID=$(aws ec2 create-nat-gateway \
        --subnet-id $PUBLIC_SUBNET_2_ID \
        --allocation-id $EIP_2_ALLOC_ID \
        --query 'NatGateway.NatGatewayId' \
        --output text)
    log_success "NAT网关2创建成功: $NAT_GW_2_ID"
    
    # 等待NAT网关变为可用状态
    log_info "等待NAT网关变为可用状态..."
    aws ec2 wait nat-gateway-available --nat-gateway-ids $NAT_GW_1_ID
    aws ec2 wait nat-gateway-available --nat-gateway-ids $NAT_GW_2_ID
    log_success "NAT网关已可用"
    
    # 添加标签
    aws ec2 create-tags --resources $NAT_GW_1_ID --tags Key=Name,Value=${VPC_NAME}-nat-gateway-1a
    aws ec2 create-tags --resources $NAT_GW_2_ID --tags Key=Name,Value=${VPC_NAME}-nat-gateway-1b
    log_success "NAT网关标签添加完成"
}

# 创建路由表
create_route_tables() {
    log_info "创建路由表..."
    
    # 公有路由表
    PUBLIC_RT_ID=$(aws ec2 create-route-table \
        --vpc-id $VPC_ID \
        --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-public-rt}]" \
        --query 'RouteTable.RouteTableId' \
        --output text)
    log_success "公有路由表创建成功: $PUBLIC_RT_ID"
    
    # 私有路由表1
    PRIVATE_RT_1_ID=$(aws ec2 create-route-table \
        --vpc-id $VPC_ID \
        --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-private-rt-1a}]" \
        --query 'RouteTable.RouteTableId' \
        --output text)
    log_success "私有路由表1创建成功: $PRIVATE_RT_1_ID"
    
    # 私有路由表2
    PRIVATE_RT_2_ID=$(aws ec2 create-route-table \
        --vpc-id $VPC_ID \
        --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${VPC_NAME}-private-rt-1b}]" \
        --query 'RouteTable.RouteTableId' \
        --output text)
    log_success "私有路由表2创建成功: $PRIVATE_RT_2_ID"
}

# 配置路由
configure_routes() {
    log_info "配置路由..."
    
    # 公有路由表 - 添加指向IGW的默认路由
    aws ec2 create-route \
        --route-table-id $PUBLIC_RT_ID \
        --destination-cidr-block 0.0.0.0/0 \
        --gateway-id $IGW_ID
    log_success "公有路由表默认路由配置完成"
    
    # 私有路由表1 - 添加指向NAT网关1的默认路由
    aws ec2 create-route \
        --route-table-id $PRIVATE_RT_1_ID \
        --destination-cidr-block 0.0.0.0/0 \
        --nat-gateway-id $NAT_GW_1_ID
    log_success "私有路由表1默认路由配置完成"
    
    # 私有路由表2 - 添加指向NAT网关2的默认路由
    aws ec2 create-route \
        --route-table-id $PRIVATE_RT_2_ID \
        --destination-cidr-block 0.0.0.0/0 \
        --nat-gateway-id $NAT_GW_2_ID
    log_success "私有路由表2默认路由配置完成"
}

# 关联子网到路由表
associate_subnets() {
    log_info "关联子网到路由表..."
    
    # 关联公有子网
    aws ec2 associate-route-table --subnet-id $PUBLIC_SUBNET_1_ID --route-table-id $PUBLIC_RT_ID
    aws ec2 associate-route-table --subnet-id $PUBLIC_SUBNET_2_ID --route-table-id $PUBLIC_RT_ID
    log_success "公有子网关联完成"
    
    # 关联私有子网
    aws ec2 associate-route-table --subnet-id $PRIVATE_SUBNET_1_ID --route-table-id $PRIVATE_RT_1_ID
    aws ec2 associate-route-table --subnet-id $PRIVATE_SUBNET_2_ID --route-table-id $PRIVATE_RT_2_ID
    log_success "私有子网关联完成"
}

# 创建安全组
create_security_groups() {
    log_info "创建安全组..."
    
    # Bedrock Agent安全组
    BEDROCK_SG_ID=$(aws ec2 create-security-group \
        --group-name ${VPC_NAME}-bedrock-agent-sg \
        --description "Security group for Bedrock AgentCore Runtime" \
        --vpc-id $VPC_ID \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=${VPC_NAME}-bedrock-agent-sg}]" \
        --query 'GroupId' \
        --output text)
    log_success "Bedrock安全组创建成功: $BEDROCK_SG_ID"
}

# 输出配置摘要
output_summary() {
    log_success "=== VPC网络配置完成 ==="
    echo ""
    echo "VPC配置摘要:"
    echo "├── VPC ID: $VPC_ID"
    echo "├── VPC CIDR: $VPC_CIDR"
    echo "├── Internet网关: $IGW_ID"
    echo "├── 公有子网1 ($AZ1): $PUBLIC_SUBNET_1_ID"
    echo "├── 公有子网2 ($AZ2): $PUBLIC_SUBNET_2_ID"
    echo "├── 私有子网1 ($AZ1): $PRIVATE_SUBNET_1_ID"
    echo "├── 私有子网2 ($AZ2): $PRIVATE_SUBNET_2_ID"
    echo "├── NAT网关1: $NAT_GW_1_ID"
    echo "├── NAT网关2: $NAT_GW_2_ID"
    echo "├── 公有路由表: $PUBLIC_RT_ID"
    echo "├── 私有路由表1: $PRIVATE_RT_1_ID"
    echo "├── 私有路由表2: $PRIVATE_RT_2_ID"
    echo "└── Bedrock安全组: $BEDROCK_SG_ID"
    echo ""
    echo "下一步："
    echo "1. 在私有子网中部署Bedrock AgentCore Runtime"
    echo "2. 使用安全组: $BEDROCK_SG_ID"
    echo "3. 选择私有子网: $PRIVATE_SUBNET_1_ID 或 $PRIVATE_SUBNET_2_ID"
    echo ""
    
    # 保存资源ID到文件
    cat > vpc-resources.txt << EOF
VPC_ID=$VPC_ID
IGW_ID=$IGW_ID
PUBLIC_SUBNET_1_ID=$PUBLIC_SUBNET_1_ID
PUBLIC_SUBNET_2_ID=$PUBLIC_SUBNET_2_ID
PRIVATE_SUBNET_1_ID=$PRIVATE_SUBNET_1_ID
PRIVATE_SUBNET_2_ID=$PRIVATE_SUBNET_2_ID
NAT_GW_1_ID=$NAT_GW_1_ID
NAT_GW_2_ID=$NAT_GW_2_ID
PUBLIC_RT_ID=$PUBLIC_RT_ID
PRIVATE_RT_1_ID=$PRIVATE_RT_1_ID
PRIVATE_RT_2_ID=$PRIVATE_RT_2_ID
BEDROCK_SG_ID=$BEDROCK_SG_ID
EIP_1_ALLOC_ID=$EIP_1_ALLOC_ID
EIP_2_ALLOC_ID=$EIP_2_ALLOC_ID
EOF
    
    log_success "资源ID已保存到 vpc-resources.txt"
}

# 清理函数（发生错误时使用）
cleanup() {
    log_error "脚本执行失败，开始清理资源..."
    # 这里可以添加清理逻辑
    # 注意：NAT网关和弹性IP需要谨慎清理，因为会产生费用
}

# 主函数
main() {
    log_info "开始创建Amazon Bedrock VPC网络架构..."
    
    # 设置错误处理
    trap cleanup ERR
    
    check_aws_cli
    create_vpc
    create_internet_gateway
    create_subnets
    create_nat_gateways
    create_route_tables
    configure_routes
    associate_subnets
    create_security_groups
    output_summary
    log_success "VPC网络架构创建完成！"
}

# 执行主函数
main "$@"
