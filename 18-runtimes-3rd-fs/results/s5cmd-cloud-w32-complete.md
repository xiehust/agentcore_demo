# s5cmd：原生 S3 / JuiceFS Gateway 测试结果

- run: `run-0c373755284b4648a12191c5ca26fe4a`；region: `us-west-2`；success: `True`。
- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。
- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。
- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。

| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |
|---|---:|---|---|---:|---:|---:|
| git-clone | 32 | cold-first-pass | file_batch_seconds | 9.4800 | 14.8379 | 0.64x |
| git-clone | 32 | cold-first-pass | wall_seconds | 11.0131 | 16.3860 | 0.67x |
| git-clone | 32 | persist-small-files | file_batch_seconds | 6.7762 | 17.7876 | 0.38x |
| git-clone | 32 | persist-small-files | wall_seconds | 8.1406 | 19.1068 | 0.43x |
| git-clone | 32 | warm-repeat | file_batch_seconds | 9.5913 | 12.8809 | 0.74x |
| git-clone | 32 | warm-repeat | wall_seconds | 11.2339 | 14.3193 | 0.78x |
| unzip | 32 | cold-first-pass | file_batch_seconds | 9.4036 | 14.6425 | 0.64x |
| unzip | 32 | cold-first-pass | wall_seconds | 10.9411 | 16.1490 | 0.68x |
| unzip | 32 | persist-small-files | file_batch_seconds | 6.8923 | 17.6374 | 0.39x |
| unzip | 32 | persist-small-files | wall_seconds | 8.1980 | 18.9381 | 0.43x |
| unzip | 32 | warm-repeat | file_batch_seconds | 9.3449 | 12.9804 | 0.72x |
| unzip | 32 | warm-repeat | wall_seconds | 10.8805 | 14.4052 | 0.76x |

按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。
没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。
每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。
