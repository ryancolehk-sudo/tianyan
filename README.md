# 天衍系统

天衍是可在个人电脑上运行的 A 股研究工作台，包含行情、资讯、复盘和可选的 AI 分析。新建真实实例时数据库为空。应用在本机运行，模型和需要授权的行情服务由每位部署者自行接入。

## 本机安装

准备 Python 3.10 或更高版本，以及支持 Linux 容器的 Docker 和 Docker Compose v2。首次构建需要联网下载依赖和镜像。解压完整的源码 ZIP，或克隆项目。在含有 `compose.yaml` 的目录执行：

```sh
python scripts/project.py verify-locks
python scripts/project.py init --mode real --project tianyan-local --port 8088
python scripts/project.py start
python scripts/project.py bootstrap --username owner
python scripts/project.py check
```

在终端按提示为自己的管理员设定密码，再打开 [本机工作台](http://127.0.0.1:8088)。默认仅监听本机。8088 已被占用时，在首次初始化时指定其他空闲端口，并用该端口访问。日常启动和停止分别运行 `python scripts/project.py start` 和 `python scripts/project.py stop`。已初始化的实例沿用原目录与配置。

从 ZIP 安装时，启动器会对照 `source-manifest.json` 校验文件。也可以先运行 `python scripts/source_distribution.py check`。从 Git 安装时，启动器记录干净工作区的提交版本。

## 使用自己的模型

1. 以本机管理员身份进入「数据管理 → 接入管理」，在「已接入模型」选择「添加模型」。
2. 选择服务协议，填写自己供应商提供的公网 HTTPS 接口、模型 ID 和 API Key。支持 OpenAI 兼容的 Chat Completions、Responses 及 Anthropic Messages 协议。
3. 「测试调用」或「测试并保存」会向所选接口发送一条短测试请求。成功后选择「设为默认并启用」，再按需开启相应的 AI 功能。供应商按自己的账户和服务条款计费。

首次安装默认关闭 AI。跳过这一步也能登录和使用无需模型的功能。启用后，相关提示词和任务所需数据由本机后端发往所选服务。当前模型连接要求公网 HTTPS，拒绝回环和内网地址。

## 行情与本机数据

需要行情时，在接入管理中添加自己有权限使用的数据源。Tushare 默认使用官方 HTTPS 服务。使用自己获准访问的其他公网 HTTPS 接口时，可将 `JL_TUSHARE_ENDPOINT` 写入本实例的 `.env`，重新启动后再配置连接。数据权限、额度与再分发许可由各供应商分别管理。

`init` 在本机生成数据库密码。模型凭据通过后端写入本实例的加密凭据存储，主密钥由本实例生成。保存好自己的 `.env`、数据库卷、凭据卷和备份，分享代码或提交问题时请移除这些内容。可选配置见 [.env.example](.env.example)。随源码保留的第三方声明见 [第三方组件](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目原创代码以 [MIT 许可证](LICENSE) 发布。随附的第三方组件适用各自的许可，行情数据与模型服务的使用和再分发权限由相应供应商授予。详见 [第三方组件声明](THIRD_PARTY_NOTICES.md)。
