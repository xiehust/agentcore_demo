# s5cmd：原生 S3 / JuiceFS Gateway 测试结果

- run: `run-8cff6e8ef8c84a809c9e1e767eef8382`；region: `us-west-2`；success: `True`。
- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。
- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。
- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。

| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |
|---|---:|---|---|---:|---:|---:|
| git-clone | 64 | cold-first-pass | file_batch_seconds | 15.5283 | 16.6139 | 0.93x |
| git-clone | 64 | cold-first-pass | wall_seconds | 17.5742 | 18.6521 | 0.94x |
| git-clone | 64 | persist-small-files | file_batch_seconds | 9.2994 | 18.3640 | 0.51x |
| git-clone | 64 | persist-small-files | wall_seconds | 11.0993 | 20.1022 | 0.55x |
| git-clone | 64 | warm-repeat | file_batch_seconds | 15.4660 | 14.7087 | 1.05x |
| git-clone | 64 | warm-repeat | wall_seconds | 17.6233 | 16.6767 | 1.06x |
| unzip | 64 | cold-first-pass | file_batch_seconds | 15.5506 | 16.3642 | 0.95x |
| unzip | 64 | cold-first-pass | wall_seconds | 17.5745 | 18.4233 | 0.95x |
| unzip | 64 | persist-small-files | file_batch_seconds | 9.2749 | 18.4657 | 0.50x |
| unzip | 64 | persist-small-files | wall_seconds | 11.0473 | 20.1920 | 0.55x |
| unzip | 64 | warm-repeat | file_batch_seconds | 15.6955 | 14.6055 | 1.07x |
| unzip | 64 | warm-repeat | wall_seconds | 17.7539 | 16.5300 | 1.07x |

按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。
没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。
每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。
