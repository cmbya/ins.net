# ins.net

本地运行的 Instagram 帖子归档器。使用自己的 Instagram Cookie，手动添加需要归档的博主，保存帖子图片与视频。

## 功能

- 手动维护监控博主，不读取或导入关注列表。
- 每位博主独立设置同步范围：最新 20 条，或全部历史分批补齐。
- 每位博主独立设置自动同步间隔（最短 30 分钟）和每轮最多下载数（1–200 条）。系统配置中的数值作为新博主默认值。达到上限后，未下载项保留在本地待办中，后续接着处理。
- 博主列表显示昵称、头像、用户名和完整归档的帖子数；Instagram 无法提供昵称或头像时显示用户名和默认头像。
- 同一账号按帖子短码去重。媒体文件已经完整归档时直接跳过；同步扫描到部分下载或媒体文件缺失的帖子时会补齐。
- 数据看板提供作品总数、作者作品数、文件大小和每日同步曲线。双击作者可以删除该作者的作品记录，记录采用隐藏标记并保留去重状态；归档文件不会被删除。
- 同步记录提供发布时间、同步日期、账号、作者、标题、来源、状态筛选和分页，可播放图片/视频、打开原帖、批量删除或恢复记录。
- 移出博主列表只停止监控，作品记录和文件仍保留；“删除作品记录”单独操作，两者均不会删除 NAS 文件。
- Instagram 授权页管理 Cookie 状态、账号备注、博主作品的保存目录，并可主动检查授权。
- 系统配置可设全局调度开关、新博主默认间隔与上限、默认目录和日志保留天数。
- 系统日志可按日期、账号、级别、关键词和任务筛选，展示每次任务的汇总及逐条扫描/下载/跳过/失败信息。Cookie 值会在采集器错误输出中遮盖。
- 轮播中的图片和视频按原媒体逐项保存；帖子中的视频照常处理，不单独扫描 Reels。
- 可设置归档根目录下的子目录。归档根目录由 Compose 映射到 `/archive` 的 NAS 文件夹决定。

## 飞牛桌面 Compose 部署（不用 SSH）

1. 在飞牛文件管理器确认归档文件夹 `/vol2/1000/docker/INS/insnet` 存在。Compose 会把它挂载到容器的 `/archive`。
2. 打开 **Docker → Compose → 新增项目**，选择创建 YAML，把 [compose.fnos.yaml](compose.fnos.yaml) 内容粘贴进去。
3. 修改 `INS_PASSWORD` 为至少 12 位的独立管理密码，并确认 `/vol2/1000/docker/INS/insnet:/archive` 左侧路径存在。
4. 保存并构建、启动。使用飞牛自带的 `bridge` 网络，浏览器打开 `http://NAS_IP:18088`。
5. 更新已有项目时选择重新构建镜像并拉取 `main` 最新代码。只重启容器不会更新代码。保留 `insnet-data` 卷，它保存账号 Cookie、同步配置和归档记录。

容器构建需要网络访问 GitHub、PyPI 和 Debian 软件源。数据库与 Cookie 放在 Docker 命名卷 `insnet-data`；媒体文件写入 NAS 绑定目录。网页中的“保存子目录”是 `/archive` 下的相对路径，例如 `Instagram/备份`。如果主机端口 18088 被占用，可将 Compose 左侧端口换成其他空闲端口，例如 `18081:18080`。

## 命令行 Compose 部署

需要 Docker Compose、可访问 Instagram 的网络和足够的归档空间。

```sh
git clone https://github.com/cmbya/ins.net.git
cd ins.net
cp .env.example .env
# 编辑 .env 中的 INS_PASSWORD
mkdir -p data archive
docker compose up -d --build
```

浏览器打开 `http://NAS_IP:18080`。通过公网访问时，请在 HTTPS 反向代理后使用；管理密码和 Cookie 属于敏感凭据。数据库/Cookie 位于 `./data`，媒体位于 `./archive`，迁移前备份这两处目录。

## 使用

1. 从自己的浏览器导出 Instagram **Netscape cookies.txt** 文件，需包含 `instagram.com` 的 `sessionid` 和 `csrftoken`。Cookie 相当于登录凭据，不要分享或提交到 Git。
2. 登录后添加 Instagram 账号和 Cookie。
3. 在“博主列表”输入用户名添加。列表会在成功读取到帖子信息后更新昵称、头像和归档计数；读取不到时显示用户名和默认头像。
4. 为每个博主选择“最新 20 条”或“全部历史（分批）”，设置同步间隔和每轮下载上限，然后保存。暂停“自动同步”不会删除博主或文件，仍可手动点“立即同步”。
5. “全部历史”会每轮读取一个有界的历史页，并保存分页游标；每轮下载上限只限制需要新下载或补齐的帖子。已完整归档的帖子只跳过，不占下载上限。遇到反复失败的媒体时，系统会把待下载帖子先处理，并降低限速错误后的重试频率。
7. 在 Instagram 授权页按账号设置 `/archive` 下博主作品的保存子目录；系统配置里的默认目录供新归档使用。系统日志可查最近任务详情。

## 开发测试

```sh
python3 -m unittest discover -s tests -v
```

来源项目思路参考 MIT 协议的 [jianzhichu/dysync.net](https://github.com/jianzhichu/dysync.net)。采集依赖 `gallery-dl`，视频可能由 `yt-dlp` 和 `ffmpeg` 处理。本站不是 Meta/Instagram 官方产品。请仅归档你有权限查看的内容，并遵守平台条款。
