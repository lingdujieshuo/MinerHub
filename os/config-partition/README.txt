MinerHub OS
===========
这个盘是 MinerHub 挖矿系统 U 盘的配置分区。

1. 用记事本打开 minerhub.txt，确认 SERVER（面板地址）和 FARM_KEY 正确。
2. 把 U 盘插到矿机上开机（BIOS 选 U 盘启动，UEFI / Legacy 都支持）。
3. 约 1 分钟后矿机会出现在 MinerHub 面板里，矿机名默认是 rig-网卡MAC后6位。
4. 在面板里分配飞行表、超频，点【启动挖矿】。

rig-info.txt 是矿机开机后自动写回的信息（矿机名、MAC），方便你认出这根 U 盘属于哪台矿机。

控制台 / SSH 登录：用户名 miner，密码见 minerhub.txt 中的 PASSWORD。
常用命令：mh status（状态） mh log（矿工日志） mh config（改配置） mh update（从面板更新 Agent）
