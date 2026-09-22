# 北京区 AgentCore 验证

所有功能与性能测试均在北京 `cn-north-1` 的新建 EC2 上运行，
当前工作站仅编写/传输代码和管理 AWS 资源。

- [计划](PLAN.md)
- [完整报告](REPORT.md)
- `lab.py`：资源管理、SSM 执行、采集与清理。
- `build_runtime.py`：在北京 EC2 构建、检查并推送 ARM64 镜像。
- `results/20260922/`：北京区独立证据，不覆盖宁夏结果。

复用的测试代码：

- [Runtime benchmark](../runtime/benchmark.py)：100 冷 / 500 暖 / 50 并发。
- [Code Interpreter](../code_interpreter/verify_code_interpreter.py)：原功能清单及 1/10/50 并发。
- [EC2 测试入口](../ec2_benchmark/run_benchmarks.py)：增加 100 次 CI 串行冷请求并按阶段执行功能、超时、PUBLIC 包源和 EFS。
- [EFS 验证](../code_interpreter/efs/verify_efs.py)：从北京 EC2 发起三个会话的挂载与持久化验证。

## 本次执行顺序

从仓库根目录运行。脚本固定使用本次测试的账号与日期目录，
复测时应先配置新目录和新资源，不能直接覆盖已完成的结果。

```bash
python3 24-China-regions/cn-north-1/lab.py setup
python3 24-China-regions/cn-north-1/lab.py build
python3 24-China-regions/cn-north-1/lab.py efs_setup
python3 24-China-regions/cn-north-1/lab.py resources
python3 24-China-regions/cn-north-1/lab.py run
python3 24-China-regions/cn-north-1/lab.py status
python3 24-China-regions/cn-north-1/lab.py collect
python3 24-China-regions/cn-north-1/lab.py cleanup
```

镜像构建和 EFS 资源准备可以同时进行。`resources` 等待 EFS 完成，并创建
Runtime 和 PUBLIC 解释器、设置实例权限。`run` 的所有实际验证由 SSM 在北京 EC2
执行；状态显示 completed 代表流程结束，各测试是否通过以结果文件为准。

## 镜像准备

北京 EC2 直连 Docker Hub 超时，因此通过北京私有 S3 bucket 传输已固定的基础镜像
文件。EC2 验证压缩包 SHA-256、镜像 ID、RootFS 和运行相关配置后，在本机离线构建、
运行容器检查并推送北京 ECR。没有在当前工作站运行容器或性能测试。
Docker 版本对空值/可选 inspect 字段的表示差异保留在构建记录中。

构建、安装依赖及服务 CREATING → READY 的时间不计入冷启动采样。

## 计时与保留方式

测试范围和统计方法见计划。SDK 自动重试关闭，保留错误和异常值。
“冷启动”和“从 0”指新用户会话，无法据此确认服务内部没有预热池。
EC2 为 t4g.small（ARM64、2 vCPU / 2 GiB），与宁夏 t3.small 的架构不同，
区域间比较不能把全部差异归因于网络。

测试后停止并保留 EC2、EBS、SSM 角色/instance profile 和安全组。
删除临时 Runtime、ECR、Code Interpreter、EFS、传输 bucket 和测试专用权限。
服务 ENI 如延迟释放，保留相关执行角色，记录并有界重试；不强制解绑 AWS 服务接口。
