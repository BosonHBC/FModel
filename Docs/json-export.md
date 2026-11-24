# FModel 批量 JSON 与 AI 命令行导出

## 交付内容

本版本基于 `pbz/nov-2026`，新增共享的 `FModel.Export` 核心、独立控制台 `FModel.Cli`，并将 GUI 的文件夹 Properties 导出接入该核心。Windows 文件版本为 `4.4.4.1`，导出协议/工具标识为 `4.4.4-json-export-1`。

Release 包提供 `FModel.exe`、`FModel.Cli.exe`、`.data` 原生依赖目录和 `profiles.example.json`。两种入口使用同一解析与写盘逻辑；CLI 不启动 WPF、Snooper、渲染器或游戏，不要求 GUI 正在运行。发布脚本生成 Windows x64 自包含程序，不依赖源码目录或额外安装 .NET。

AES 和 `.usmap` 没有打包。它们只从你本地已有配置读取，不联网获取，不在日志、报告或示例里输出真实密钥。新 Release 没有自动覆盖旧程序，也不会自行导出整个 Content。

## GUI 使用

打开新版 GUI，进入 Settings → Batch JSON Export：

- Existing files：默认 `skip`，可选择 `resume` 或明确覆盖的 `overwrite`。
- Workers：默认 2。PBZ 已验证的上限是 4；输入更大的数字时实际并发受限。其他未验证预设使用单 worker。
- Soft memory budget：默认 4096 MiB。大包独占执行，内存压力下限流；这是软预算，不保证任何单包都能装进该值。
- Compact JSON：默认关闭。开启后仅减少缩进和空白，不减少对象或属性。

右键文件夹 → Export Folder Properties (.json) 即可使用新路径；多个包的 Properties 导出也走新核心。单文件查看/保存、Raw Data、模型、音频、贴图等原有流程未被替换。混合目录中的非 `.uasset/.umap` 项保留旧导出逻辑，因此它们不享受新 JSON 流水线的全部加速与续导能力。

进度和真实结果显示在日志中，任务结束可查看输出目录的 `.fmodel-export/latest-report.json`。默认完成日志为 `.fmodel-export/manifest.jsonl`。GUI 和 CLI 使用同一输出根目录锁，不能同时向同一目录导出；旧版 FModel 不识别该锁，不要与新版同时写同一输出目录。

## CLI 常用命令

以下默认发布目录为 `I:/PBZ/Releases/fmodel-json-export`。CLI profile `pbz` 可匹配本机 PBZ GUI 配置；也可以指定发布包中的无密钥 profile 示例。示例 profile 的相对路径以 profile 文件所在目录为基准。

帮助与配置枚举不会初始化资源容器：

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' --help
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' --version
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' profiles list --format json
```

按虚拟目录枚举，不是磁盘目录或 `/Game` 路径：

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' list --profile pbz --profile-file 'I:/PBZ/Releases/fmodel-json-export/profiles.example.json' --path 'ProjectPBZ/Content/Maps/B08_09_10' --recursive --format json
```

用户决定开始全量导出时，可以执行以下命令。示例会保留原来已有的非空 JSON，不会写入游戏目录：

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' export-json --profile pbz --profile-file 'I:/PBZ/Releases/fmodel-json-export/profiles.example.json' --path 'ProjectPBZ/Content' --recursive --output 'I:/PBZ/FModelOutputs/Exports' --workers 2 --memory-mb 4096 --existing skip --events jsonl --report 'I:/PBZ/Analysis/content-export-report.json'
```

严格续导只复用指纹匹配的成功记录：

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' export-json --profile pbz --path 'ProjectPBZ/Content' --recursive --output 'I:/PBZ/FModelOutputs/Exports' --workers 2 --existing resume --events jsonl
```

补导报告中的 failed/partial 包：

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' export-json --profile pbz --retry-report 'I:/PBZ/Analysis/content-export-report.json' --output 'I:/PBZ/FModelOutputs/Exports' --workers 2 --events jsonl --report 'I:/PBZ/Analysis/content-export-retry-report.json'
```

重试必须使用与原报告相同的输入配置和输出根目录。配置/usmap 已改变时不要使用旧 retry-report，而应使用新任务的 `--existing resume`。取消或尚未调度的包也应通过原目录/清单的 resume 继续，不属于 retry-report 的 failed/partial 选择范围。

只导出清单中的包：先用 `list --format paths` 输出路径清单，再用 `--files-from` 读取；每行一个含 `.uasset/.umap` 扩展名的完整虚拟包路径。

```powershell
& 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe' export-json --profile pbz --files-from 'I:/PBZ/Analysis/selected-packages.txt' --output 'I:/PBZ/SelectedExports' --existing skip --events jsonl
```

## 已有文件策略

`skip`：默认。加载包之前跳过非空最终 JSON。有匹配成功记录时为 `skipped`，没有可信记录的旧文件为 `skipped-unverified`。它以保留既有工作为目的，不能保证旧文件对应当前游戏或映射版本，也不会默认读取数十 GiB 旧 JSON 验证内容。

`resume`：只跳过与源容器、包、usmap、解析选项、工具版本、格式及输出长度相符的成功记录。无记录、变更、failed/partial/cancelled 都重新解析。快速输入指纹包含 `.utoc` 内容 SHA-256、资源文件大小/时间和 usmap SHA-256；默认不对全部 `.ucas/.pak` 做全量哈希。

`overwrite`：明确重新导出选中包。写入同目录唯一 `.tmp` 后才发布最终 JSON；异常或取消保留以前的完整文件。`partial` 是语法完整但解析有诊断的结果，允许保存用于分析，报告会明确标记，并在下次运行时重新尝试；它不能当作完整成功。

`--verify-existing`：流式检查已有 JSON 语法和顶层数组，发现损坏时重新导出。这不能证明 UE 对象属性全部解析正确。

`--hash-outputs`：成功结果记录 SHA-256，续导时校验实际输出。启用时无历史输出哈希的 resume 结果会重新导出。

`--hash-inputs`：对源容器数据做完整 SHA-256，代价可能很高；游戏或数据变化时优先保留新的输出目录。不要在导出期间更新游戏、AES、映射或原生库。

目录结构默认保留；`--flat` 的重名会明确报错而不是覆盖。报告、manifest 和输出不得位于游戏安装目录，路径穿越与输出重解析点会被拒绝。临时文件不会被当成已完成结果；异常关闭可能遗留唯一 `.tmp`，续导忽略它们而不清理用户未知文件。

## AI 自动化与结果判断

AI 可直接启动 CLI、后台运行并观察 JSONL stdout，不需要截图或鼠标操作。`--events jsonl` 时 stdout 每行均为合法 JSON；正常日志/原生诊断写 stderr。事件含 `schemaVersion`、`jobId`、阶段、进度、计数和实际 workers；最后一行 summary 含退出码、报告位置、未处理数和耗时。

每包状态包括 `success`、`skipped`、`skipped-unverified`、`partial`、`failed`、`cancelled`。`partial` 表示解析器缺少映射或遇到读取错误，即使 JSON 文件已经生成，也不算完整成功。报告包含全部选中包的已处理结果，`unprocessed` 单独表示还未处理的包。

退出码：0=按用户指定的已有策略完成；1=参数/初始化失败；2=完成但有 failed/partial 或未归属解析错误；3=取消；4=磁盘、权限、记录写入等执行期任务级故障。旧文件 skipped-unverified 可按 skip 策略退出 0，但仍保持未验证标识。

`Ctrl+C` 可协作取消。AI 没有交互终端时，可传 `--cancel-file 'I:/PBZ/Analysis/export.cancel'`，需要停止时创建该文件；下一次启动前删除自己的取消标记或使用新路径。取消在调度/序列化边界生效，不承诺打断正在执行的不可取消原生读取；强制终止也可以靠 manifest/resume 恢复，但不会产生正常结束报告。

显式参数优先于 CLI profile，CLI profile 优先于只读 GUI 配置。可用 `--input`、`--game`、`--mapping`、`--data-directory` 定位全部本地输入；密钥可通过 `--keys-file` 的 GUID→AES JSON 字典或进程环境变量 `FMODEL_AES_KEY` 提供，避免把真实密钥写到命令行。默认读取 `%APPDATA%/FModel/AppSettings.json`，但 CLI 从不写回。

## 构建与验证

仓库新增核心回归、CLI 进程集成和实际 PBZ 基准项目。无需额外安装测试框架：

```powershell
dotnet run --project 'I:/FModel/FModel.Export.Tests/FModel.Export.Tests.csproj' -c Release -p:CUE4PARSE_SKIP_NATIVE=true
dotnet run --project 'I:/FModel/FModel.Export.Tests/FModel.Export.Tests.csproj' -c Release -p:CUE4PARSE_SKIP_NATIVE=true -- --cli 'I:/PBZ/Releases/fmodel-json-export/FModel.Cli.exe'
dotnet run --project 'I:/FModel/FModel.Export.Benchmarks/FModel.Export.Benchmarks.csproj' -c Release -p:CUE4PARSE_SKIP_NATIVE=true
powershell -NoProfile -File 'I:/FModel/Scripts/Publish-JsonExport.ps1'
```

PBZ 基准需要本机配置和既有庞镇 JSON/路径清单，仅写新测试目录。旧路径复现了完整 JSON 字符串、Dispatcher、AvalonEdit TextDocument 和 UI 写盘，不包括整个旧应用启动或交互。测试按旧、新单 worker、新双 worker 顺序执行，磁盘缓存为热缓存；不能将样本倍数直接推算成全量 Content 耗时，也不能将 skip 时间当成真正重新解析的速度。

已测试庞镇根地图、普通单元、FastGeo、HLOD 和环境资产。当前 usmap 对 ReallinkPerfCamera 的缺失映射仍存在，提速改造不修复这些解析器问题。
