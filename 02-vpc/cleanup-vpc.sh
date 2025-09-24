#!/bin/bash

# =============================================================================
# Amazon Bedrock AgentCore VPC Network Cleanup Script
# 清理由setup-vpc.sh和setup-vpc-endpoints.sh创建的资源
# =============================================================================

set -e  # 遇到错误时退出

# 配置变量 - 根据需要修改这些值
REGION="us-west-2"

# 如果有现有的资源ID文件，加载它们
if [ -f "vpc-resources.txt" ]; then
    source vpc-resources.txt
    echo "已加载现有VPC资源配置"
else
    echo "错误: 找不到vpc-resources.txt文件，无法清理资源"
    echo "请确保在执行清理脚本之前，已创建资源并生成了vpc-resources.txt文件"
    exit 1
fi

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
    
    # 获取当前区域
    CURRENT_REGION=$(aws configure get region 2>/dev/null || echo "")
    if [ -n "$CURRENT_REGION" ] && [ "$CURRENT_REGION" != "$REGION" ]; then
        log_warning "当前AWS CLI区域 ($CURRENT_REGION) 与脚本设置的区域 ($REGION) 不同"
        read -p "是否继续使用脚本设置的区域 $REGION? (y/n): " confirm
        if [ "$confirm" != "y" ]; then
            REGION=$CURRENT_REGION
            log_info "使用当前AWS CLI区域: $REGION"
        fi
    fi
    
    log_success "AWS CLI配置正常，使用区域: $REGION"
}

# 确认删除所有资源
confirm_deletion() {
    log_warning "警告: 此脚本将删除以下VPC资源:"
    echo "├── VPC ID: $VPC_ID"
    echo "├── Internet网关: $IGW_ID"
    echo "├── 公有子网1: $PUBLIC_SUBNET_1_ID"
    echo "├── 公有子网2: $PUBLIC_SUBNET_2_ID"
    echo "├── 私有子网1: $PRIVATE_SUBNET_1_ID"
    echo "├── 私有子网2: $PRIVATE_SUBNET_2_ID"
    echo "├── NAT网关1: $NAT_GW_1_ID"
    echo "├── NAT网关2: $NAT_GW_2_ID"
    echo "├── 公有路由表: $PUBLIC_RT_ID"
    echo "├── 私有路由表1: $PRIVATE_RT_1_ID"
    echo "├── 私有路由表2: $PRIVATE_RT_2_ID"
    echo "├── 弹性IP1: $EIP_1_ALLOC_ID"
    echo "├── 弹性IP2: $EIP_2_ALLOC_ID"
    echo "├── Bedrock安全组: $BEDROCK_SG_ID"
    echo ""
    echo "VPC端点:"
    echo "├── ECR Docker: $ECR_DKR_ENDPOINT_ID"
    echo "├── ECR API: $ECR_API_ENDPOINT_ID"
    echo "├── S3 Gateway: $S3_GATEWAY_ENDPOINT_ID"
    echo "├── CloudWatch Logs: $LOGS_ENDPOINT_ID"
    echo "└── Bedrock Runtime: $BEDROCK_ENDPOINT_ID"
    echo ""
    
    read -p "确定要删除以上所有资源吗? (输入'yes'确认): " confirmation
    if [ "$confirmation" != "yes" ]; then
        log_info "操作已取消"
        exit 0
    fi
    
    log_info "已确认删除操作，开始清理资源..."
}

# 删除VPC端点
delete_vpc_endpoints() {
    log_info "开始删除VPC端点..."
    
    # 要删除的端点ID列表
    ENDPOINTS=(
        "$BEDROCK_ENDPOINT_ID"
        "$LOGS_ENDPOINT_ID"
        "$S3_GATEWAY_ENDPOINT_ID"
        "$ECR_API_ENDPOINT_ID"
        "$ECR_DKR_ENDPOINT_ID"
    )
    
    # 删除所有端点
    for endpoint_id in "${ENDPOINTS[@]}"; do
        if [ -n "$endpoint_id" ] && [ "$endpoint_id" != "None" ]; then
            log_info "删除VPC端点: $endpoint_id..."
            
            if aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$endpoint_id" > /dev/null 2>&1; then
                log_success "已删除VPC端点: $endpoint_id"
            else
                log_warning "删除VPC端点失败或已不存在: $endpoint_id"
            fi
        fi
    done
    
    log_success "VPC端点删除完成"
}

# 主要删除VPC资源的函数
delete_vpc_resources() {
    log_info "开始删除VPC资源..."
    
    # 步骤1: 删除NAT网关
    if [ -n "$NAT_GW_1_ID" ] && [ "$NAT_GW_1_ID" != "None" ]; then
        log_info "删除NAT网关1: $NAT_GW_1_ID..."
        aws ec2 delete-nat-gateway --nat-gateway-id "$NAT_GW_1_ID" > /dev/null 2>&1 || log_warning "删除NAT网关1失败或已不存在"
    fi
    
    if [ -n "$NAT_GW_2_ID" ] && [ "$NAT_GW_2_ID" != "None" ]; then
        log_info "删除NAT网关2: $NAT_GW_2_ID..."
        aws ec2 delete-nat-gateway --nat-gateway-id "$NAT_GW_2_ID" > /dev/null 2>&1 || log_warning "删除NAT网关2失败或已不存在"
    fi
    
    # 等待NAT网关删除完成
    if [ -n "$NAT_GW_1_ID" ] || [ -n "$NAT_GW_2_ID" ]; then
        log_info "等待NAT网关删除完成（这可能需要几分钟）..."
        sleep 60  # 给NAT网关删除一些时间
    fi
    
    # 步骤2: 释放弹性IP
    if [ -n "$EIP_1_ALLOC_ID" ] && [ "$EIP_1_ALLOC_ID" != "None" ]; then
        log_info "释放弹性IP1: $EIP_1_ALLOC_ID..."
        aws ec2 release-address --allocation-id "$EIP_1_ALLOC_ID" > /dev/null 2>&1 || log_warning "释放弹性IP1失败或已不存在"
        log_success "已释放弹性IP1"
    fi
    
    if [ -n "$EIP_2_ALLOC_ID" ] && [ "$EIP_2_ALLOC_ID" != "None" ]; then
        log_info "释放弹性IP2: $EIP_2_ALLOC_ID..."
        aws ec2 release-address --allocation-id "$EIP_2_ALLOC_ID" > /dev/null 2>&1 || log_warning "释放弹性IP2失败或已不存在"
        log_success "已释放弹性IP2"
    fi
    
    # 步骤3: 删除安全组
    if [ -n "$BEDROCK_SG_ID" ] && [ "$BEDROCK_SG_ID" != "None" ]; then
        log_info "删除安全组: $BEDROCK_SG_ID..."
        aws ec2 delete-security-group --group-id "$BEDROCK_SG_ID" > /dev/null 2>&1 || log_warning "删除安全组失败或已不存在"
        log_success "已删除安全组"
    fi
    
    # 步骤4: 从路由表中分离子网
    # 公有子网
    if [ -n "$PUBLIC_SUBNET_1_ID" ] && [ "$PUBLIC_SUBNET_1_ID" != "None" ]; then
        ASSOC_ID=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$PUBLIC_SUBNET_1_ID" --query "RouteTables[].Associations[?SubnetId=='$PUBLIC_SUBNET_1_ID'].RouteTableAssociationId" --output text)
        if [ -n "$ASSOC_ID" ] && [ "$ASSOC_ID" != "None" ]; then
            log_info "解除公有子网1关联: $ASSOC_ID..."
            aws ec2 disassociate-route-table --association-id "$ASSOC_ID" > /dev/null 2>&1 || log_warning "解除公有子网1关联失败或已不存在"
        fi
    fi
    
    if [ -n "$PUBLIC_SUBNET_2_ID" ] && [ "$PUBLIC_SUBNET_2_ID" != "None" ]; then
        ASSOC_ID=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$PUBLIC_SUBNET_2_ID" --query "RouteTables[].Associations[?SubnetId=='$PUBLIC_SUBNET_2_ID'].RouteTableAssociationId" --output text)
        if [ -n "$ASSOC_ID" ] && [ "$ASSOC_ID" != "None" ]; then
            log_info "解除公有子网2关联: $ASSOC_ID..."
            aws ec2 disassociate-route-table --association-id "$ASSOC_ID" > /dev/null 2>&1 || log_warning "解除公有子网2关联失败或已不存在"
        fi
    fi
    
    # 私有子网
    if [ -n "$PRIVATE_SUBNET_1_ID" ] && [ "$PRIVATE_SUBNET_1_ID" != "None" ]; then
        ASSOC_ID=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$PRIVATE_SUBNET_1_ID" --query "RouteTables[].Associations[?SubnetId=='$PRIVATE_SUBNET_1_ID'].RouteTableAssociationId" --output text)
        if [ -n "$ASSOC_ID" ] && [ "$ASSOC_ID" != "None" ]; then
            log_info "解除私有子网1关联: $ASSOC_ID..."
            aws ec2 disassociate-route-table --association-id "$ASSOC_ID" > /dev/null 2>&1 || log_warning "解除私有子网1关联失败或已不存在"
        fi
    fi
    
    if [ -n "$PRIVATE_SUBNET_2_ID" ] && [ "$PRIVATE_SUBNET_2_ID" != "None" ]; then
        ASSOC_ID=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$PRIVATE_SUBNET_2_ID" --query "RouteTables[].Associations[?SubnetId=='$PRIVATE_SUBNET_2_ID'].RouteTableAssociationId" --output text)
        if [ -n "$ASSOC_ID" ] && [ "$ASSOC_ID" != "None" ]; then
            log_info "解除私有子网2关联: $ASSOC_ID..."
            aws ec2 disassociate-route-table --association-id "$ASSOC_ID" > /dev/null 2>&1 || log_warning "解除私有子网2关联失败或已不存在"
        fi
    fi
    
    # 步骤5: 删除路由表
    if [ -n "$PUBLIC_RT_ID" ] && [ "$PUBLIC_RT_ID" != "None" ]; then
        log_info "删除公有路由表: $PUBLIC_RT_ID..."
        aws ec2 delete-route-table --route-table-id "$PUBLIC_RT_ID" > /dev/null 2>&1 || log_warning "删除公有路由表失败或已不存在"
        log_success "已删除公有路由表"
    fi
    
    if [ -n "$PRIVATE_RT_1_ID" ] && [ "$PRIVATE_RT_1_ID" != "None" ]; then
        log_info "删除私有路由表1: $PRIVATE_RT_1_ID..."
        aws ec2 delete-route-table --route-table-id "$PRIVATE_RT_1_ID" > /dev/null 2>&1 || log_warning "删除私有路由表1失败或已不存在"
        log_success "已删除私有路由表1"
    fi
    
    if [ -n "$PRIVATE_RT_2_ID" ] && [ "$PRIVATE_RT_2_ID" != "None" ]; then
        log_info "删除私有路由表2: $PRIVATE_RT_2_ID..."
        aws ec2 delete-route-table --route-table-id "$PRIVATE_RT_2_ID" > /dev/null 2>&1 || log_warning "删除私有路由表2失败或已不存在"
        log_success "已删除私有路由表2"
    fi
    
    # 步骤6: 删除子网
    if [ -n "$PUBLIC_SUBNET_1_ID" ] && [ "$PUBLIC_SUBNET_1_ID" != "None" ]; then
        log_info "删除公有子网1: $PUBLIC_SUBNET_1_ID..."
        aws ec2 delete-subnet --subnet-id "$PUBLIC_SUBNET_1_ID" > /dev/null 2>&1 || log_warning "删除公有子网1失败或已不存在"
        log_success "已删除公有子网1"
    fi
    
    if [ -n "$PUBLIC_SUBNET_2_ID" ] && [ "$PUBLIC_SUBNET_2_ID" != "None" ]; then
        log_info "删除公有子网2: $PUBLIC_SUBNET_2_ID..."
        aws ec2 delete-subnet --subnet-id "$PUBLIC_SUBNET_2_ID" > /dev/null 2>&1 || log_warning "删除公有子网2失败或已不存在"
        log_success "已删除公有子网2"
    fi
    
    if [ -n "$PRIVATE_SUBNET_1_ID" ] && [ "$PRIVATE_SUBNET_1_ID" != "None" ]; then
        log_info "删除私有子网1: $PRIVATE_SUBNET_1_ID..."
        aws ec2 delete-subnet --subnet-id "$PRIVATE_SUBNET_1_ID" > /dev/null 2>&1 || log_warning "删除私有子网1失败或已不存在"
        log_success "已删除私有子网1"
    fi
    
    if [ -n "$PRIVATE_SUBNET_2_ID" ] && [ "$PRIVATE_SUBNET_2_ID" != "None" ]; then
        log_info "删除私有子网2: $PRIVATE_SUBNET_2_ID..."
        aws ec2 delete-subnet --subnet-id "$PRIVATE_SUBNET_2_ID" > /dev/null 2>&1 || log_warning "删除私有子网2失败或已不存在"
        log_success "已删除私有子网2"
    fi
    
    # 步骤7: 从VPC分离Internet网关
    if [ -n "$IGW_ID" ] && [ "$IGW_ID" != "None" ] && [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
        log_info "分离Internet网关: $IGW_ID 从VPC: $VPC_ID..."
        aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" > /dev/null 2>&1 || log_warning "分离Internet网关失败或已不存在"
        log_success "已分离Internet网关"
    fi
    
    # 步骤8: 删除Internet网关
    if [ -n "$IGW_ID" ] && [ "$IGW_ID" != "None" ]; then
        log_info "删除Internet网关: $IGW_ID..."
        aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID" > /dev/null 2>&1 || log_warning "删除Internet网关失败或已不存在"
        log_success "已删除Internet网关"
    fi
    
    # 步骤9: 删除VPC
    if [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
        log_info "删除VPC: $VPC_ID..."
        aws ec2 delete-vpc --vpc-id "$VPC_ID" > /dev/null 2>&1 || log_warning "删除VPC失败或已不存在"
        log_success "已删除VPC"
    fi
    
    log_success "VPC资源删除完成"
}

# 主函数
main() {
    log_info "开始清理Amazon Bedrock VPC资源..."
    
    # 设置错误处理
    trap 'log_error "脚本执行失败"; exit 1' ERR
    
    check_aws_cli
    confirm_deletion
    delete_vpc_endpoints
    delete_vpc_resources
    
    log_success "所有VPC资源已成功清理！"
    
    # 备份并清理资源文件
    if [ -f "vpc-resources.txt" ]; then
        cp vpc-resources.txt vpc-resources.backup.txt
        log_info "已将资源文件备份为 vpc-resources.backup.txt"
        > vpc-resources.txt
        log_info "已清空资源文件 vpc-resources.txt"
    fi
}

# 执行主函数
main "$@"