# MinerHub OS —— 刻录即用的挖矿系统 U 盘

中文 · [English](README_EN.md)

Ubuntu Server 24.04 LTS 精简版，已经集成：

- NVIDIA server 版驱动（使用预编译内核模块，开机即用，不需要 DKMS 编译）
- SRBMiner-MULTI
- MinerHub Agent（开机自动连接面板）

支持 Legacy BIOS 和 UEFI 两种启动方式，8G 及以上 U 盘都能用，首次开机会自动扩容到整个 U 盘。

## 刻录与使用

1. 用 **balenaEtcher**（可以直接选 `.img.xz`）或 **Rufus**（选 DD 镜像模式）把镜像刻到 U 盘。
2. 刻完后在电脑上会多出一个名为 **MINERHUB** 的盘，用记事本打开里面的 `minerhub.txt`：
   - `SERVER=` 填面板地址，例如 `http://192.168.1.10:8800`
   - `FARM_KEY=` 填面板【设置】页里的 Farm Key
   - `PASSWORD=` 填 SSH 密码，建议改掉默认值
3. U 盘插到矿机上，BIOS 里选 U 盘启动。约 1 分钟后矿机会出现在面板里。
4. 在面板里给矿机分配飞行表、超频，然后点【启动挖矿】。

几点说明：

- **同一个配置可以刻任意多根 U 盘。** 矿机名默认是 `rig-网卡MAC后6位`，不会重名。想自定义就填 `RIG_NAME=`。
- **随时可以改配置。** 拔下 U 盘插到电脑上改 `minerhub.txt`，插回去开机即生效。也可以在矿机上执行 `mh config`。
- **怎么认出某根 U 盘属于哪台矿机。** 看 U 盘里的 `rig-info.txt`，这是矿机开机后自动写回的。

## 登录与常用命令

显示器会显示 IP 和矿机名。控制台或 SSH 登录用户名是 `miner`，密码就是 `minerhub.txt` 里的 `PASSWORD`（默认 `minerhub`）。

| 命令 | 作用 |
|---|---|
| `mh status` | 矿机名、IP、面板、Agent / 矿工状态、各卡温度功耗 |
| `mh log` | 实时矿工日志 |
| `mh agent` | Agent 日志 |
| `mh config` | 编辑 U 盘配置并立即应用 |
| `mh update` | 从面板下载最新 agent.py 并重启 |
| `mh update-srb [URL]` | 升级 SRBMiner（不填 URL 就用 GitHub 最新版） |

启动菜单里有第二项 **"no NVIDIA driver / rescue"**，驱动出问题、卡在开机时可以选它进系统排查。

## 针对 U 盘做的优化

- 系统日志、矿工日志、`/tmp` 全部放在内存里，日常几乎不写 U 盘。
- 根分区用 `noatime` 挂载，提交间隔 60 秒；没有 swap。
- 关闭自动更新，并锁定内核和驱动版本，防止后台升级把驱动搞坏。
- 开机时自动修复文件系统（`fsck.repair=yes`），不怕意外断电。
- 内核参数加了 `pci=noaer pcie_aspm=off`，减少转接卡（riser）带来的 PCIe 报错和掉卡。

## 自己重新构建镜像

想换驱动版本、升级 SRBMiner，或者把面板地址直接预填进镜像时，可以自己构建。需要一台 Ubuntu 22.04 / 24.04 x86_64 机器（虚拟机或 VPS 都行），留 15G 空闲空间：

```bash
cd minerhub/os
sudo SERVER=http://192.168.1.10:8800 FARM_KEY=你的FarmKey bash build-image.sh
# 产物在 os/out/minerhub-os-日期.img.xz
```

可选环境变量：

| 变量 | 作用 |
|---|---|
| `NV=580` | 指定驱动大版本（默认自动选最新 server 版） |
| `SRB_URL=...` | 指定 SRBMiner 压缩包 |
| `PASSWORD` | 默认登录密码 |
| `TIMEZONE` | 时区 |
| `IMG_SIZE=6G` | 镜像大小 |
| `MIRROR` | 软件源，国内可以换成 `http://mirrors.aliyun.com/ubuntu` |

## BIOS 设置建议

- 启动顺序选 U 盘。CSM / Legacy 和 UEFI 都可以；关闭 Secure Boot。
- 定时开机：开启 **Wake on RTC / Resume by RTC Alarm**（设为 By OS），以及 **Wake on LAN**。
- 断电恢复：**Restore on AC Power Loss = Power On**，这样停电恢复后会自动开机。
- 有 **Above 4G Decoding** 选项的主板，多卡时务必打开；识别不全或开机卡住时，把 PCIe 速率设为 Gen1 / Gen2。
