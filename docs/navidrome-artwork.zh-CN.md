# 从 Navidrome 媒体库自动读取封面

此功能按本机 `D:\git\navidrome` 改版的实际导出结构实现：读取目录内的图片与 `musicfile` 格式 NFO，不依赖 Navidrome 数据库表、登录凭据或 API。不会访问 STRM 地址、NFO 的 `targetpath`，也不会修改音乐库。

## 启用

更新代码并安装依赖后，在 Maloja 管理页的设置中填写 **Navidrome Media Library**，例如 `C:\Users\JYQ\音乐库`，保持 **Use Local Images** 开启。保存后重启一次，启动时会在后台加载索引。也可在原有启动环境中添加：

```powershell
$env:MALOJA_MEDIA_LIBRARY_PATH = 'C:\Users\JYQ\音乐库'
```

保留你现有的 `MALOJA_DATA_DIRECTORY` 和启动方式。这里的目录必须是 **Maloja 进程所在机器可读取的路径**。没有设置音乐库路径时，原有本地图片和外部封面提供商仍然可用。

Docker 可直接使用发布镜像，并把媒体库只读挂载到容器。例如，在 Debian 的现有 compose 配置上补充：

```yaml
services:
  maloja:
    image: liristy/maloja:3.2.8
    environment:
      MALOJA_MEDIA_LIBRARY_PATH: /music
      MALOJA_STARTPAGE_CHART_IMAGES: "14"
    volumes:
      - "/srv/music:/music:ro"
```

把 `/srv/music` 换成 Debian 上实际存在的音乐库路径，保留原来的数据卷、端口和权限配置；容器内的设置使用 `/music`。短格式挂载兼容旧版 `docker-compose`。已有配置若将 `MALOJA_STARTPAGE_CHART_IMAGES` 设为 `6`，需改成 `14`；环境变量会覆盖新版本默认值。

## 识别规则

支持 `.jpg`、`.jpeg`、`.png`、`.webp`、`.gif` 图片，按以下顺序识别：

| 类型 | 读取方式 |
| --- | --- |
| 歌手 | 只读取媒体歌手目录内的 `artist.*`，不使用 `folder.*`、专辑封面或外部照片 |
| 专辑 | 专辑目录内 `cover.*`、`folder.*`、`front.*`；没有时，仅在单曲图片来源唯一的情况下回退 |
| 单曲 | 与 NFO 同名的 `文件名-cover.*`、`文件名.*`，其次专辑图片 |

例如：

```text
音乐库/
  蔡健雅/
    artist.jpg
    Goodbye & Hello/
      cover.jpg
      空白格 - 蔡健雅.nfo
      空白格 - 蔡健雅.strm
      空白格 - 蔡健雅-cover.jpg
```

NFO 读取 `title`、`artist`、`album`、`albumartist`，以及 `participants/participant` 中的歌手角色。多歌手同时兼容独立 participant 列表和同一 NFO 的 `artist` 显示名（如 `SARA • 刘佳`），不会随意拆分乐队名。原声带也兼容旧收听记录以单曲歌手作为专辑歌手的情况。

歌手照片遵循媒体目录：歌手同名目录的 `artist.*` 优先；没有时按 NFO 将歌手显示名关联到该媒体所在的歌手目录。合唱读取实际存放目录的 `artist.*`，不选排序第一位歌手的照片。若同一歌手关联多个目录且照片来源不同，则保留占位图，避免随机错配。

保留中文和标点，优先按完整歌手集合、标题及专辑匹配。旧收听记录的专辑名称不同，仅在完整歌手集合和曲名能唯一匹配一张单曲图片时回退；多版本存在歧义时不会选图。没有 NFO 时支持 `歌手/专辑/文件` 两层目录，曲名取文件名并去掉末尾的 ` - 歌手`。不会猜测编号或混音名；目前不提取音频文件内嵌图片，普通本地音频需提供旁置图片。

多个不同目录提供同一身份的不同封面时，索引会记录冲突并跳过歧义匹配。启用媒体库后，默认不再为未匹配项目进行外部模糊搜索。如确实需要，可开启 **External Artwork Fallback**。

## 封面优先级与旧数据

专辑和单曲优先级为：当前手动上传 → 最近一次旧版上传 → 媒体库 → 旧式本地图片 → 外部提供商（允许时）→ 占位图。配置媒体库后，歌手照片严格使用其 `artist.*`；旧上传、外部缓存不再覆盖媒体目录，没有对应图片就显示占位图。旧上传文件仍保留在数据目录。

新的手动上传使用完整身份的 SHA-256 文件名，保存于 Maloja 数据目录的 `images/selected/`，不会随图片缓存过期而改变；重复上传替换当前选择。旧版同一目录存在多张上传图片时，按文件修改时间选择最新一张，不再随机轮换。旧式本地图片也改为固定顺序。删除中文后生成的 ASCII 别名（例如 `albums/_.jpg`）不再参与匹配；这类文件无法可靠判断原本属于谁，需要重新上传或使用音乐库封面。

单曲自身的上传或媒体库封面优先于“Use Album Artwork for tracks”设置；没有独立封面时才按该设置回退。Maloja 本身对一个曲目所记录的专辑归属仍由原有的 `album_information_trust` 规则决定。

## 刷新、压缩与维护

索引保存到 Maloja 缓存目录的 `media-artwork.json`。服务启动时读取上次索引，并在后台扫描；访问封面时，每隔默认 300 秒触发一次更新，未修改的 NFO 复用解析结果。媒体库路径不可用时保留旧索引，并记录扫描错误。新库首次扫描期间页面可先显示，其图片请求会自动重试；特别大的库可先执行下面的预处理。

需要立即刷新或提前生成全部缩略图时，在与正式服务相同的数据目录配置下执行：

```console
maloja syncartwork --compress
```

从源码虚拟环境执行也可以：

```powershell
.\.venv\Scripts\python.exe -m maloja syncartwork --compress
```

命令也接受 `--root` 指定本次扫描路径；不会自动修改服务设置。输出包含匹配键数、歧义数、解析错误数和压缩结果。匹配键数包含有专辑限定与无专辑限定的单曲索引，不等于歌曲数量。冲突的图片路径可在索引文件的 `conflicts` 字段查看。

正在运行的服务会在下一次后台刷新时更新内存索引；要立即采用命令刷新后的全部匹配结果，可以重启 Maloja。

缩略图默认最长边 320 像素、WebP 质量 78，首次请求时生成，之后复用。修改源图片、尺寸或质量后会生成新缓存地址；原图不变。动态封面地址不缓存重定向，生成的图片地址可长期缓存。历史缩略图可通过清理 Maloja 的 `cache/images/` 目录回收；不要删除 `images/selected/`，它保存手动选择。清理图片缓存后同时移除 `cache/imagecache.sqlite`，避免旧的外部图片索引引用已删除文件；应在停止 Maloja 后操作。

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| `media_library_path` | 空 | 媒体库根目录 |
| `media_library_scan_interval` | 300 | 扫描间隔，秒，至少 30 |
| `media_library_external_fallback` | false | 启用媒体库时允许外部封面搜索 |
| `image_thumbnail_size` | 320 | 图片最长边，64–1200 |
| `image_quality` | 78 | WebP 质量，1–100 |
| `startpage_chart_images` | 14 | 首页每个榜单的图片数，1–14；桌面每行 7 张，手机每行 4 张 |

环境变量为上述名称大写并加 `MALOJA_` 前缀。首页各时间范围的隐藏图片、视口外图片不再提前提交后台解析任务；完整排行榜仍保留原有展示数量。

## 开发验证

```console
python -m unittest discover -s tests -v
```

测试使用临时数据库与生成的图片，不访问正式 Maloja 数据或在线封面提供商。
