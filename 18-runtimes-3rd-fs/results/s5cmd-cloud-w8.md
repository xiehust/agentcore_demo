# s5cmd：原生 S3 / JuiceFS Gateway 测试结果

- run: `run-057b548ca6e14f098ee72c6fe2f94c65`；region: `us-west-2`；success: `True`。
- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。
- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。
- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。

| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |
|---|---:|---|---|---:|---:|---:|
| git-clone | 8 | cold-first-pass | file_batch_seconds | 34.0951 | 25.5184 | 1.34x |
| git-clone | 8 | cold-first-pass | wall_seconds | 36.1800 | 27.5710 | 1.31x |
| git-clone | 8 | persist-small-files | file_batch_seconds | 24.5213 | 32.4825 | 0.75x |
| git-clone | 8 | persist-small-files | wall_seconds | 26.3976 | 34.2045 | 0.77x |
| git-clone | 8 | warm-repeat | file_batch_seconds | 33.0394 | 12.0529 | 2.74x |
| git-clone | 8 | warm-repeat | wall_seconds | 35.1999 | 13.9980 | 2.51x |
| unzip | 8 | cold-first-pass | file_batch_seconds | 33.8664 | 24.9729 | 1.36x |
| unzip | 8 | cold-first-pass | wall_seconds | 35.9449 | 27.0310 | 1.33x |
| unzip | 8 | persist-small-files | file_batch_seconds | 24.6705 | 31.4834 | 0.78x |
| unzip | 8 | persist-small-files | wall_seconds | 26.4490 | 33.2501 | 0.80x |
| unzip | 8 | warm-repeat | file_batch_seconds | 32.6339 | 11.8046 | 2.76x |
| unzip | 8 | warm-repeat | wall_seconds | 34.7453 | 13.7601 | 2.53x |

按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。
没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。
每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。
