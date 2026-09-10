# s5cmd：原生 S3 / JuiceFS Gateway 测试结果

- run: `run-4f02515cd8dc488e8a7896c05d8a5ef4`；region: `us-west-2`；success: `False`。
- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。
- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。
- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。

| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |
|---|---:|---|---|---:|---:|---:|
| git-clone | 256 | cold-first-pass | file_batch_seconds | N/A | N/A | N/A |
| git-clone | 256 | cold-first-pass | wall_seconds | N/A | N/A | N/A |
| git-clone | 256 | persist-small-files | file_batch_seconds | N/A | N/A | N/A |
| git-clone | 256 | persist-small-files | wall_seconds | N/A | N/A | N/A |
| git-clone | 256 | warm-repeat | file_batch_seconds | N/A | N/A | N/A |
| git-clone | 256 | warm-repeat | wall_seconds | N/A | N/A | N/A |
| unzip | 256 | cold-first-pass | file_batch_seconds | N/A | N/A | N/A |
| unzip | 256 | cold-first-pass | wall_seconds | N/A | N/A | N/A |
| unzip | 256 | persist-small-files | file_batch_seconds | N/A | N/A | N/A |
| unzip | 256 | persist-small-files | wall_seconds | N/A | N/A | N/A |
| unzip | 256 | warm-repeat | file_batch_seconds | N/A | N/A | N/A |
| unzip | 256 | warm-repeat | wall_seconds | N/A | N/A | N/A |

按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。
没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。
每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。
