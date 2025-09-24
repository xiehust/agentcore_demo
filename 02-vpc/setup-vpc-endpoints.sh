#!/bin/bash

# =============================================================================
# Essential VPC Endpoints Setup Script for Bedrock AgentCore Runtime
# 创建ECR、S3和CloudWatch所需的VPC端点
# =============================================================================

set -e  # 遇到错误时退出

# 配置变量 - 根据需要修改这些值
REGION="us-west-2"
VPC_NAME="agentcore-vpc"

# 如果有现有的资源ID文件，可以加载它们
if [ -f "vpc-resources.txt" ]; then
    source vpc-resources.txt
    echo "已加载现有VPC资源配置"
else
    # 手动设置资源ID（如果没有资源文件）
    echo "请设置以下变量或提供vpc-resources.txt文件："
    echo "VPC_ID, PRIVATE_SUBNET_1_ID, PRIVATE_SUBNET_2_ID, BEDROCK_SG_ID"
    
    # 取消注释并设置实际值
    # VPC_ID="vpc-xxxxxxxxx"
    # PRIVATE_SUBNET_1_ID="subnet-xxxxxxxxx"  
    # PRIVATE_SUBNET_2_ID="subnet-yyyyyyyyy"
    # BEDROCK_SG_ID="sg-xxxxxxxxx"
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

# 检查必需变量
check_variables() {
    log_info "检查必需的变量..."
    
    if [ -z "$VPC_ID" ] || [ -z "$PRIVATE_SUBNET_1_ID" ] || [ -z "$PRIVATE_SUBNET_2_ID" ] || [ -z "$BEDROCK_SG_ID" ]; then
        log_error "缺少必需的变量。请确保设置了以下变量："
        echo "  - VPC_ID"
        echo "  - PRIVATE_SUBNET_1_ID"
        echo "  - PRIVATE_SUBNET_2_ID"
        echo "  - BEDROCK_SG_ID"
        exit 1
    fi
    
    log_success "所有必需变量已设置"
}

# 检查AWS CLI配置
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

# 添加检查现有端点的函数
check_existing_endpoint() {
    local service_name=$1
    local endpoint_name=$2
    
    local existing_endpoint=$(aws ec2 describe-vpc-endpoints \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=$service_name" \
        --query 'VpcEndpoints[0].VpcEndpointId' \
        --output text 2>/dev/null || echo "None")
    
    if [ "$existing_endpoint" != "None" ] && [ "$existing_endpoint" != "" ] && [ "$existing_endpoint" != "null" ]; then
        # 只返回端点ID，不输出日志信息到stdout
        echo $existing_endpoint
    else
        echo ""
    fi
}
# 修改创建端点的函数，例如：
create_ecr_dkr_endpoint() {
    log_info "创建ECR Docker VPC端点..."
    
    # 检查是否已存在
    EXISTING_ENDPOINT=$(check_existing_endpoint "com.amazonaws.${REGION}.ecr.dkr" "ECR Docker端点")
    
    if [ -n "$EXISTING_ENDPOINT" ]; then
        ECR_DKR_ENDPOINT_ID=$EXISTING_ENDPOINT
        log_warning "ECR Docker端点 已存在: $ECR_DKR_ENDPOINT_ID"
        log_success "使用现有ECR Docker VPC端点: $ECR_DKR_ENDPOINT_ID"
        return
    fi
    
    ECR_DKR_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
        --vpc-id $VPC_ID \
        --service-name com.amazonaws.${REGION}.ecr.dkr \
        --vpc-endpoint-type Interface \
        --subnet-ids $PRIVATE_SUBNET_1_ID $PRIVATE_SUBNET_2_ID \
        --security-group-ids $BEDROCK_SG_ID \
        --private-dns-enabled \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${VPC_NAME}-ecr-dkr-endpoint}]" \
        --query 'VpcEndpoint.VpcEndpointId' \
        --output text)
    
    log_success "ECR Docker VPC端点创建成功: $ECR_DKR_ENDPOINT_ID"
}


# 使用现有的Bedrock安全组作为VPC端点安全组
setup_vpc_endpoint_security_group() {
    log_info "使用现有的Bedrock安全组作为VPC端点安全组..."
    VPC_ENDPOINT_SG_ID=$BEDROCK_SG_ID
    log_success "使用现有安全组: $VPC_ENDPOINT_SG_ID"
}

# 创建ECR API VPC端点
create_ecr_api_endpoint() {
    log_info "创建ECR API VPC端点..."
    
    # 检查是否已存在
    EXISTING_ENDPOINT=$(check_existing_endpoint "com.amazonaws.${REGION}.ecr.api" "ECR API端点")
    
    if [ -n "$EXISTING_ENDPOINT" ]; then
        ECR_API_ENDPOINT_ID=$EXISTING_ENDPOINT
        log_warning "ECR API端点 已存在: $ECR_API_ENDPOINT_ID"
        log_success "使用现有ECR API VPC端点: $ECR_API_ENDPOINT_ID"
        return
    fi
    
    ECR_API_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
        --vpc-id $VPC_ID \
        --service-name com.amazonaws.${REGION}.ecr.api \
        --vpc-endpoint-type Interface \
        --subnet-ids $PRIVATE_SUBNET_1_ID $PRIVATE_SUBNET_2_ID \
        --security-group-ids $BEDROCK_SG_ID \
        --private-dns-enabled \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${VPC_NAME}-ecr-api-endpoint}]" \
        --query 'VpcEndpoint.VpcEndpointId' \
        --output text)
    
    log_success "ECR API VPC端点创建成功: $ECR_API_ENDPOINT_ID"
}

# 创建S3 Gateway VPC端点
create_s3_gateway_endpoint() {
    log_info "创建S3 Gateway VPC端点..."
    
    # 检查是否已存在
    EXISTING_ENDPOINT=$(check_existing_endpoint "com.amazonaws.${REGION}.s3" "S3 Gateway端点")
    
    if [ -n "$EXISTING_ENDPOINT" ]; then
        S3_GATEWAY_ENDPOINT_ID=$EXISTING_ENDPOINT
        log_warning "S3 Gateway端点 已存在: $S3_GATEWAY_ENDPOINT_ID"
        log_success "使用现有S3 Gateway VPC端点: $S3_GATEWAY_ENDPOINT_ID"
        return
    fi
    
    # 获取私有子网的路由表ID
    PRIVATE_RT_IDS=$(aws ec2 describe-route-tables \
        --filters "Name=association.subnet-id,Values=$PRIVATE_SUBNET_1_ID,$PRIVATE_SUBNET_2_ID" \
        --query 'RouteTables[].RouteTableId' \
        --output text)
    
    if [ -z "$PRIVATE_RT_IDS" ]; then
        log_error "无法找到私有子网的路由表"
        exit 1
    fi
    
    log_info "私有路由表ID: $PRIVATE_RT_IDS"
    
    S3_GATEWAY_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
        --vpc-id $VPC_ID \
        --service-name com.amazonaws.${REGION}.s3 \
        --vpc-endpoint-type Gateway \
        --route-table-ids $PRIVATE_RT_IDS \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${VPC_NAME}-s3-gateway-endpoint}]" \
        --query 'VpcEndpoint.VpcEndpointId' \
        --output text)
    
    log_success "S3 Gateway VPC端点创建成功: $S3_GATEWAY_ENDPOINT_ID"
}

# 创建CloudWatch Logs VPC端点
create_logs_endpoint() {
    log_info "创建CloudWatch Logs VPC端点..."
    
    # 检查是否已存在
    EXISTING_ENDPOINT=$(check_existing_endpoint "com.amazonaws.${REGION}.logs" "CloudWatch Logs端点")
    
    if [ -n "$EXISTING_ENDPOINT" ]; then
        LOGS_ENDPOINT_ID=$EXISTING_ENDPOINT
        log_warning "CloudWatch Logs端点 已存在: $LOGS_ENDPOINT_ID"
        log_success "使用现有CloudWatch Logs VPC端点: $LOGS_ENDPOINT_ID"
        return
    fi
    
    LOGS_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
        --vpc-id $VPC_ID \
        --service-name com.amazonaws.${REGION}.logs \
        --vpc-endpoint-type Interface \
        --subnet-ids $PRIVATE_SUBNET_1_ID $PRIVATE_SUBNET_2_ID \
        --security-group-ids $BEDROCK_SG_ID \
        --private-dns-enabled \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${VPC_NAME}-logs-endpoint}]" \
        --query 'VpcEndpoint.VpcEndpointId' \
        --output text)
    
    log_success "CloudWatch Logs VPC端点创建成功: $LOGS_ENDPOINT_ID"
}

# 创建Bedrock VPC端点
create_bedrock_endpoint() {
    log_info "创建Bedrock VPC端点..."
    
    # 检查是否已存在
    EXISTING_ENDPOINT=$(check_existing_endpoint "com.amazonaws.${REGION}.bedrock-runtime" "Bedrock端点")
    
    if [ -n "$EXISTING_ENDPOINT" ]; then
        BEDROCK_ENDPOINT_ID=$EXISTING_ENDPOINT
        log_warning "Bedrock端点 已存在: $BEDROCK_ENDPOINT_ID"
        log_success "使用现有Bedrock VPC端点: $BEDROCK_ENDPOINT_ID"
        return
    fi
    
    BEDROCK_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
        --vpc-id $VPC_ID \
        --service-name com.amazonaws.${REGION}.bedrock-runtime \
        --vpc-endpoint-type Interface \
        --subnet-ids $PRIVATE_SUBNET_1_ID $PRIVATE_SUBNET_2_ID \
        --security-group-ids $BEDROCK_SG_ID \
        --private-dns-enabled \
        --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${VPC_NAME}-bedrock-endpoint}]" \
        --query 'VpcEndpoint.VpcEndpointId' \
        --output text)
    
    log_success "Bedrock VPC端点创建成功: $BEDROCK_ENDPOINT_ID"
}

# 验证VPC端点状态
verify_endpoints() {
    log_info "验证VPC端点状态..."
    
    # 等待端点变为可用状态
    log_info "等待VPC端点变为可用状态（这可能需要几分钟）..."
    
    # 检查所有接口端点的状态
    INTERFACE_ENDPOINTS=($ECR_DKR_ENDPOINT_ID $ECR_API_ENDPOINT_ID $LOGS_ENDPOINT_ID $BEDROCK_ENDPOINT_ID)
    
    for endpoint_id in "${INTERFACE_ENDPOINTS[@]}"; do
        if [ -n "$endpoint_id" ] && [ "$endpoint_id" != "None" ]; then
            log_info "等待端点 $endpoint_id 变为可用..."
            
            # 自定义等待逻辑，因为 aws ec2 wait vpc-endpoint-available 不存在
            max_attempts=30
            attempt=0
            while [ $attempt -lt $max_attempts ]; do
                endpoint_state=$(aws ec2 describe-vpc-endpoints \
                    --vpc-endpoint-ids $endpoint_id \
                    --query 'VpcEndpoints[0].State' \
                    --output text 2>/dev/null || echo "failed")
                
                if [ "$endpoint_state" = "available" ]; then
                    log_success "端点 $endpoint_id 已可用"
                    break
                elif [ "$endpoint_state" = "failed" ]; then
                    log_error "端点 $endpoint_id 创建失败"
                    exit 1
                else
                    log_info "端点 $endpoint_id 当前状态: $endpoint_state，等待中..."
                    sleep 30
                    ((attempt++))
                fi
            done
            
            if [ $attempt -eq $max_attempts ]; then
                log_warning "端点 $endpoint_id 等待超时，但继续执行"
            fi
        fi
    done
    
    # 检查网关端点状态
    if [ -n "$S3_GATEWAY_ENDPOINT_ID" ] && [ "$S3_GATEWAY_ENDPOINT_ID" != "None" ]; then
        S3_STATE=$(aws ec2 describe-vpc-endpoints \
            --vpc-endpoint-ids $S3_GATEWAY_ENDPOINT_ID \
            --query 'VpcEndpoints[0].State' \
            --output text)
        
        if [ "$S3_STATE" = "available" ]; then
            log_success "S3 Gateway端点已可用"
        else
            log_warning "S3 Gateway端点状态: $S3_STATE"
        fi
    fi
}

# 更新Bedrock安全组规则
update_bedrock_security_group() {
    log_info "更新Bedrock安全组以允许访问VPC端点..."
    
    # 获取VPC的CIDR块
    VPC_CIDR=$(aws ec2 describe-vpcs \
        --vpc-ids $VPC_ID \
        --query 'Vpcs[0].CidrBlock' \
        --output text)
    
    if [ -z "$VPC_CIDR" ] || [ "$VPC_CIDR" = "None" ]; then
        log_error "无法获取VPC CIDR块"
        exit 1
    fi
    
    log_info "VPC CIDR块: $VPC_CIDR"
    
    # 检查是否已存在HTTPS入站规则
    EXISTING_RULE=$(aws ec2 describe-security-groups \
        --group-ids $BEDROCK_SG_ID \
        --query "SecurityGroups[0].IpPermissions[?FromPort==\`443\` && ToPort==\`443\` && IpProtocol==\`tcp\`]" \
        --output text 2>/dev/null || echo "")
    
    if [ -n "$EXISTING_RULE" ]; then
        log_warning "安全组已存在HTTPS (443) 入站规则"
    else
        log_info "添加HTTPS (443) 入站规则到安全组..."
        
        # 添加允许来自VPC内部的HTTPS流量规则
        aws ec2 authorize-security-group-ingress \
            --group-id $BEDROCK_SG_ID \
            --protocol tcp \
            --port 443 \
            --cidr $VPC_CIDR \
            --tag-specifications "ResourceType=security-group-rule,Tags=[{Key=Name,Value=${VPC_NAME}-vpc-endpoints-https}]" 2>/dev/null || {
            
            # 如果标签添加失败，尝试不带标签的版本（某些AWS区域不支持安全组规则标签）
            aws ec2 authorize-security-group-ingress \
                --group-id $BEDROCK_SG_ID \
                --protocol tcp \
                --port 443 \
                --cidr $VPC_CIDR
        }
        
        log_success "已添加HTTPS (443) 入站规则: $VPC_CIDR -> $BEDROCK_SG_ID"
    fi
    
    # 验证安全组规则
    log_info "验证安全组规则..."
    HTTPS_RULES=$(aws ec2 describe-security-groups \
        --group-ids $BEDROCK_SG_ID \
        --query "SecurityGroups[0].IpPermissions[?FromPort==\`443\` && ToPort==\`443\` && IpProtocol==\`tcp\`].IpRanges[].CidrIp" \
        --output text)
    
    if [ -n "$HTTPS_RULES" ]; then
        log_success "安全组HTTPS规则验证成功: $HTTPS_RULES"
    else
        log_warning "无法验证HTTPS安全组规则"
    fi
    
    log_success "Bedrock安全组已更新"
}

# 输出配置摘要
output_summary() {
    log_success "=== 必要VPC端点配置完成 ==="
    echo ""
    echo "已创建的VPC端点:"
    echo "├── ECR Docker: $ECR_DKR_ENDPOINT_ID"
    echo "├── ECR API: $ECR_API_ENDPOINT_ID"
    echo "├── S3 Gateway: $S3_GATEWAY_ENDPOINT_ID"
    echo "├── CloudWatch Logs: $LOGS_ENDPOINT_ID"
    echo "└── Bedrock Runtime: $BEDROCK_ENDPOINT_ID"
    echo ""
    echo "VPC端点安全组: $VPC_ENDPOINT_SG_ID"
    echo ""
    echo "配置详情:"
    echo "├── 区域: $REGION"
    echo "├── VPC: $VPC_ID"
    echo "├── 私有子网1: $PRIVATE_SUBNET_1_ID"
    echo "├── 私有子网2: $PRIVATE_SUBNET_2_ID"
    echo "└── Bedrock安全组: $BEDROCK_SG_ID"
    echo ""
    
    # 保存VPC端点信息到文件
    cat >> vpc-resources.txt << EOF

# Essential VPC Endpoints
VPC_ENDPOINT_SG_ID=$VPC_ENDPOINT_SG_ID
ECR_DKR_ENDPOINT_ID=$ECR_DKR_ENDPOINT_ID
ECR_API_ENDPOINT_ID=$ECR_API_ENDPOINT_ID
S3_GATEWAY_ENDPOINT_ID=$S3_GATEWAY_ENDPOINT_ID
LOGS_ENDPOINT_ID=$LOGS_ENDPOINT_ID
BEDROCK_ENDPOINT_ID=$BEDROCK_ENDPOINT_ID
EOF
    
    log_success "VPC端点信息已追加到 vpc-resources.txt"
    
    echo ""
    echo "服务映射:"
    echo "├── ECR Docker Registry: com.amazonaws.${REGION}.ecr.dkr"
    echo "├── ECR API: com.amazonaws.${REGION}.ecr.api"
    echo "├── S3 (ECR镜像层存储): com.amazonaws.${REGION}.s3"
    echo "├── CloudWatch Logs: com.amazonaws.${REGION}.logs"
    echo "└── Bedrock Runtime: com.amazonaws.${REGION}.bedrock-runtime"
    echo ""
    echo "安全组要求:"
    echo "├── VPC端点安全组必须允许HTTPS (443) 入站流量"
    echo "├── 来源: VPC内部 ($VPC_CIDR)"
    echo "└── 协议: TCP/443"
    echo ""
    echo "下一步："
    echo "1. 验证所有端点都处于'available'状态"
    echo "2. 确认安全组规则允许HTTPS流量"
    echo "3. 在私有子网中部署Bedrock AgentCore Runtime"
    echo "4. 测试应用程序对ECR、S3和CloudWatch的访问"
    echo ""
}

# 主函数
main() {
    log_info "开始配置必要的VPC端点（ECR、S3、CloudWatch）..."
    
    # 设置错误处理
    trap 'log_error "脚本执行失败"; exit 1' ERR
    
    check_aws_cli
    check_variables
    setup_vpc_endpoint_security_group
    create_ecr_dkr_endpoint
    create_ecr_api_endpoint
    create_s3_gateway_endpoint
    create_logs_endpoint
    create_bedrock_endpoint
    verify_endpoints
    update_bedrock_security_group
    output_summary
    
    log_success "必要VPC端点配置完成！"
}

# 执行主函数
main "$@"
