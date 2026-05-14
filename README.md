# Google Drive → STRM 同步方案

## 目录结构

```
gdrive_strm_sync/
├── strm_sync.py   # 主同步脚本
├── strm_sync.sh   # Shell 入口（防并发、日志）
└── README.md      # 本文档
```

---

## 快速开始

### 1. 安装依赖

```bash
pip3 install requests
# rclone 需已安装并配置好 Google Drive 远端
```

### 2. 修改配置

编辑 `strm_sync.py` 顶部的 `CONFIG` 字典：

```python
CONFIG = {
    "rclone_remote": "gdrive:Media",        # rclone 远端:路径
    "rclone_mount":  "/mnt/gdrive/Media",   # rclone 挂载点（播放用）
    "local_media":   "/local/media",         # 本地媒体根目录（Emby 指向这里）
    "emby_host":     "http://localhost:8096",
    "emby_api_key":  "你的 Emby API Key",
}
```

**获取 Emby API Key：**  
Emby 管理后台 → 用户 → 你的用户 → 安全 → 访问令牌

### 3. 给脚本加执行权限

```bash
chmod +x strm_sync.sh
```

### 4. 初次全量迁移

```bash
# 第一次运行，同步所有元数据 + 生成所有 STRM
python3 strm_sync.py --full-cleanup
```

耗时取决于库的大小，大库可能需要数十分钟（主要是 rclone lsf 列文件）。

---

## Cron 定时任务配置

```bash
crontab -e
```

推荐配置：

```cron
# 每 2 小时做一次增量同步（适合频繁新增内容）
0 */2 * * * /path/to/strm_sync.sh incremental >> /var/log/strm_sync.log 2>&1

# 每周日凌晨 3 点做一次全量同步（清理已删除的孤立 STRM）
0 3 * * 0 /path/to/strm_sync.sh full >> /var/log/strm_sync.log 2>&1
```

---

## 与刮削工具集成

如果你的刮削工具执行完后能运行一个命令，在刮削工具的"完成后执行"或 webhook 里加：

```bash
/path/to/strm_sync.sh incremental
```

这样新内容刮削完就立刻同步，不用等定时任务。

---

## Emby 配置调整

在 Emby 管理后台，把媒体库路径从 rclone 挂载目录改成本地目录：

| 配置项 | 旧值 | 新值 |
|--------|------|------|
| 媒体库路径 | `/mnt/gdrive/Media/Movies` | `/local/media/Movies` |
| 媒体库路径 | `/mnt/gdrive/Media/TV` | `/local/media/TV` |

同时建议在 Emby 中关闭"实时监控"（因为现在是脚本主动通知），减少不必要的扫描。

---

## 工作原理

```
扫库时（无 API 调用）：
  Emby → 读 /local/media/Movies/Avatar/Avatar.strm（1 行文本）
       → 读 /local/media/Movies/Avatar/Avatar.nfo（本地）
       → 读 /local/media/Movies/Avatar/poster.jpg（本地）

播放时（按需 API 调用）：
  Emby → 读 STRM 内容 → /mnt/gdrive/Media/Movies/Avatar/Avatar.mkv
       → rclone 按需从 Google Drive 拉取视频流
```

---

## 常见问题

**Q: 第一次迁移要多久？**  
A: 主要耗时在 `rclone lsf` 列文件。1 万个文件约 2-5 分钟。元数据同步取决于文件大小和数量。

**Q: STRM 文件内容应该是挂载路径还是 HTTP URL？**  
A: 如果 rclone 是挂载模式（`rclone mount`），填挂载路径即可。
如果你用 `rclone serve http/webdav`，填对应 HTTP URL。

**Q: 刮削工具写入元数据后多久 Emby 能看到？**  
A: 等下一次 cron 触发（默认 2 小时），或手动运行一次 `strm_sync.sh incremental`。

**Q: 如何验证 STRM 是否正确？**  
```bash
cat "/local/media/Movies/某电影/某电影.strm"
# 应该输出类似：/mnt/gdrive/Media/Movies/某电影/某电影.mkv
ls "/local/media/Movies/某电影/"
# 应该能看到 .strm .nfo poster.jpg 等
```
