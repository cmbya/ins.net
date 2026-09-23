# ins.net

将 Instagram 的关注博主帖子、Reels 和自己已保存的帖子归档到本地。源项目构思参考 [dysync.net](https://github.com/jianzhichu/dysync.net)；此版本采用 Python 标准库、[gallery-dl](https://github.com/mikf/gallery-dl) 和 SQLite 重新实现。暂不支持快拍、点赞列表或 fnOS FPK 安装包。

## 功能

- 用 Instagram 登录 Cookie 导入关注列表，手动选择要监控的博主；也可直接添加博主。
- 监控博主帖子和 Reels；归档账号自己保存的帖子。
- 同一帖子只保留一份记录，同时标注“关注博主”与“已保存”等来源。
- 轮播图片、视频逐项保留原文件，在页面内切换；视频支持播放和拖动。
- 自动同步间隔默认 6 小时，页面可随时手动同步；博主默认只扫描每个来源最新 30 条，可为单个博主打开“全部历史”；已保存列表扫描全部历史。
- SQLite 记录同步状态；下载失败的媒体下一次继续补齐。

## 在飞牛桌面直接部署（不用 SSH）

1. 在飞牛桌面打开 **Docker → Compose → 新增项目**，项目名称填 `insnet`，保存路径可选你常用的项目文件夹。
2. 选择**创建 YAML / docker-compose.yml**，将 [compose.fnos.yaml](compose.fnos.yaml) 的内容完整复制进去。
3. 把 `INS_PASSWORD` 的示例文字替换成自己的至少 12 位密码；确认主机端口 `18080` 未被占用，然后保存并构建、启动。
4. 在浏览器打开 `http://NAS_IP:18080`，用设置的管理密码登录。

这份配置使用 Docker 自带的 `bridge` 网络（`network_mode: bridge`），从公开 GitHub 仓库构建镜像；NAS 首次构建需要连接 GitHub、PyPI 和 Debian 软件源。下载数据保存在 Docker 命名卷 `insnet-data`，重启和更新项目时会保留；**删除项目时不要勾选删除数据卷**。命名卷由 Docker 管理，不会直接显示在飞牛文件管理器的 `/vol2/1000` 中。端口占用时，把 `18080:18080` 左侧改为其他空闲端口，例如 `18081:18080`，浏览器也使用新端口。

## 命令行部署（其他 Linux 环境）

需要 Docker Compose、能访问 Instagram 的网络，以及容器所在设备足够的存储空间。`gallery-dl`、`yt-dlp` 和 `ffmpeg` 在镜像里安装。

```sh
git clone https://github.com/cmbya/ins.net.git
cd ins.net
cp .env.example .env
# 编辑 .env 的 INS_PASSWORD，至少 12 位
mkdir -p data
sudo chown -R 10001:10001 data
docker compose up -d --build
```

在浏览器打开 `http://NAS_IP:18080`。公开网络访问请通过 HTTPS 反向代理；管理密码和 Cookie 不适合通过明文 HTTP 在公网传送。数据放在 `./data`，迁移时备份这个目录。端口、挂载位置可以在 `compose.yaml` 中修改。在 fnOS 的 Docker Compose 项目中也可直接导入这个目录。

## 添加账号并使用

1. 从你自己的浏览器导出 Instagram 的 **Netscape cookies.txt 格式**文件，文件必须包含 `instagram.com` 域名下的 `sessionid`。它相当于登录凭据，请勿提交到 Git 或分享给他人。
2. 登录 ins.net 后，添加账号用户名和 Cookie 文件。
3. 点击“导入关注列表”，勾选想归档的博主，再点“全部同步”。也可手动添加博主；“同步已保存”不依赖导入关注列表。
4. 浏览“归档记录”：轮播按原始顺序切换，每张图或每段视频作为独立文件保存在 `data/media/<账号>/<博主>/<日期>_<短码>/`。

`INS_MAX_POSTS` 限制一次对每个博主来源扫描的帖子数（默认 30），`INS_INTERVAL_HOURS` 设置定时同步周期。博主“全部历史”模式每次会重新枚举整个历史列表，适合首次补档，完成后可以关闭。已保存列表每次扫描全部历史，首次同步可能较慢。当 Cookie 过期、Instagram 要求验证或限制访问时，用同一用户名和新 Cookie 在“添加账号 / 更新 Cookie”中保存即可。请只归档你有权限查看的内容，并遵守平台条款。

## 技术说明

同步通过 `gallery-dl` 的 Instagram extractor 读取关注列表、`/posts/`、`/reels/`、`/<账号>/saved/`；媒体下载在暂存目录完成，再移动到归档目录。记录按账号和帖子短码去重，媒体按 `media_id` 去重。程序不会把图片合成视频，也不会另外保存视频封面或音轨。登录和媒体接口都需要管理密码；Cookie 存在数据目录，不写入数据库或日志。

```sh
python3 -m unittest discover -s tests -v
```

## 来源

项目思路来自 MIT 协议的 [jianzhichu/dysync.net](https://github.com/jianzhichu/dysync.net)。采集依赖 gallery-dl；视频格式可能由 yt-dlp / ffmpeg 处理。本站并非 Meta/Instagram 官方产品。
