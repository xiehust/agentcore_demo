# s5cmd：原生 S3 / JuiceFS Gateway 测试结果

- run: `run-3f3452eba0764c34ab2aa10c6c6cc35c`；region: `us-west-2`；success: `True`。
- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。
- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。
- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。

| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |
|---|---:|---|---|---:|---:|---:|
| git-clone | 128 | cold-first-pass | file_batch_seconds | 9.1795 | 16.7866 | 0.55x |
| git-clone | 128 | cold-first-pass | wall_seconds | 10.7111 | 18.2669 | 0.59x |
| git-clone | 128 | persist-small-files | file_batch_seconds | 5.7770 | 18.5904 | 0.31x |
| git-clone | 128 | persist-small-files | wall_seconds | 7.1633 | 19.9439 | 0.36x |
| git-clone | 128 | warm-repeat | file_batch_seconds | 9.4480 | 14.7562 | 0.64x |
| git-clone | 128 | warm-repeat | wall_seconds | 11.0726 | 16.2057 | 0.68x |
| unzip | 128 | cold-first-pass | file_batch_seconds | 9.2324 | 16.5993 | 0.56x |
| unzip | 128 | cold-first-pass | wall_seconds | 10.8113 | 18.0539 | 0.60x |
| unzip | 128 | persist-small-files | file_batch_seconds | 5.6722 | 18.5949 | 0.31x |
| unzip | 128 | persist-small-files | wall_seconds | 6.9885 | 19.9483 | 0.35x |
| unzip | 128 | warm-repeat | file_batch_seconds | 9.2797 | 14.7847 | 0.63x |
| unzip | 128 | warm-repeat | wall_seconds | 10.8072 | 16.2143 | 0.67x |

按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。
没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。
每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。
