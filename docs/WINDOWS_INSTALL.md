# Windows 本机使用

使用源码 ZIP 时，在已启用 Linux 容器的 Docker Desktop 中启动引擎，按照仓库根目录 README 中的命令初始化本机实例。首次启动成功后双击桌面上的「天衍系统」，直接进入本机工作空间。已有实例可从源码目录运行 `python scripts/project.py shortcut` 补建图标。

使用自行构建的原生安装器时，安装程序会注册本机服务。安装后从桌面或开始菜单的「天衍系统」进入，按界面提示设置自己的管理员。程序版本目录与实例数据目录独立，数据库和凭据由该实例生成。卸载前先保存自己的备份。

维护者可在干净的 Git 检出中用 `python scripts/native/build_windows.py --help` 查看原生构建参数。构建需要 Windows x64、匹配锁定版本的构建环境以及上游分发文件。
