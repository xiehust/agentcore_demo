# 中国区 Runtime 多进程压测

Owner：River。

按用户最终要求，后续运行最高 **100 并发**。本轮先前已完成的 200 并发数据仅保留作附录，
不再追加该档测试；当时实际执行源码也随结果保存。

参考仓库 `21-runtime-v2-beta/multiprocess_coldstart_client.py` 的 spawn 多进程、
独立 SDK 客户端、全局发令屏障和延后结果写入方法；适配中国区 echo 应用协议，
并增加冷/暖阶段隔离、三轮重复、客户端 CPU 采样和逐会话身份检查。

完整方法见 [PLAN.md](PLAN.md)，测试结论与边界见 [REPORT.md](REPORT.md)。

## 复现

管理端需要 Python 3.12+、boto3/botocore 1.43.87 和 `agentcore_cn` profile。
基础镜像使用此前验证过的离线存档：
`../../cn-north-1/results/20260922/ec2/offline-base.tar.gz`。
大体积镜像存档不提交到 Git；内容摘要和下载源保留在 build_config.json。
存档缺失时需先从固定源导出，并核验 image ID、RootFS、Config 与本轮配置一致。

在本目录执行；每轮使用新的 output 路径，避免覆盖证据：

```bash
python3 manage.py setup --region cn-northwest-1 --output results/new-run
python3 manage.py build --region cn-northwest-1 --output results/new-run
python3 manage.py status --region cn-northwest-1 --output results/new-run
# 等待 build 状态 completed、SSM Success 后继续。
python3 manage.py resources --region cn-northwest-1 --output results/new-run
python3 manage.py run --region cn-northwest-1 --output results/new-run
python3 manage.py status --region cn-northwest-1 --output results/new-run
# final.json 出现后收集，再清理。
python3 manage.py collect --region cn-northwest-1 --output results/new-run
python3 analyze.py results/new-run/cn-northwest-1/results
# 可选：按本轮限流诊断补测，批次间隔 180 秒。
python3 manage.py isolated_resources --region cn-northwest-1 --output results/new-run
python3 manage.py isolated_run --region cn-northwest-1 --output results/new-run
# 等待 S3 isolated/final.json；再收集、清理。
python3 manage.py isolated_collect --region cn-northwest-1 --output results/new-run
python3 manage.py cleanup --region cn-northwest-1 --output results/new-run
python3 audit.py results/new-run
```

北京将 region 改为 `cn-north-1`。两区使用独立资源，可以同时测试。
所有真实 Invoke 都由区域内 EC2 执行。EC2 在真实测试前先运行四项离线检查：
真实 spawn 的阶段隔离、单请求进程数、重复实例标记拒绝、进程初始化失败。

## 文件

- manage.py：EC2 / ECR / Runtime 管理、SSM 下发、取回结果、停止并保留实例。
- client.py：进程、线程、屏障、计时、响应校验及会话关闭。
- run.py：验证 EC2 区域及规格，执行各轮，上传证据。
- analyze.py：从原始事件计算 nearest-rank 分位数及样本有效性。
- audit.py：复核执行源码、完整矩阵、跨批会话唯一性及实时清理状态。
- test_client.py：无 AWS 请求的客户端检查，在区域 EC2 上执行。

当前默认每区 13 个临时 Runtime：1/10/50/100 四档 × 3 轮，以及独立的 50 并发停留验证。
计划前三轮 cold/warm 分别 483 次，停留组各 50 次，最多 1,066 次 Invoke；
冷请求失败时跳过该会话的暖请求，因此实际总数应以 raw.json 为准。
每个 session 及时 Stop；无 Invoke 自动重试。会话 TTL 为 1,800 秒，
idle timeout 为 300 秒，给高并发失败诊断留出余量。

当前额外诊断为 2 个 Runtime：50 并发停留、100 并发各一轮，批次间隔 180 秒；
放在 isolated-results/，不混入首轮统计。新的完整复测每区合计创建 15 个临时 Runtime。
本轮历史执行在范围收口前包含 200 并发，因此实际创建了每区 19 个 Runtime，
当时原始完整矩阵共 1,133 个冷请求，追加诊断 350 个；均以执行配置与证据为准。

冷/暖返回时间均包含区内网络及 SDK 开销。并发暖 P99 与原先单会话
500 次串行暖 P99 是不同负载条件，报告中分别展示。
