# 贡献流程

提交 PR 和合并目标分支时使用维护者指定的 `git-delivery-workflow` 技能。发布源码前运行仓库静态检查、依赖锁校验、暂存内容与 Git 历史安全检查。用 `python scripts/source_distribution.py write` 从已审阅的暂存区刷新源码清单，再暂存清单并运行 `check`。

只使用 Codex 内置浏览器验收网页。源码与测试夹具使用合成数据，本机配置、账户、凭据、真实业务数据和私人素材保存在分发目录外。
