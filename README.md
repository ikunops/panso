# panso — 网盘资源搜索与索引系统

一个自托管的网盘分享链接检索工具：**实时聚合（pansou + Telegram 频道）+ 本地 SQLite 索引 + 链接验活 + 内容级去重 + 可视化 Web 界面**。

仓库只包含源码，不含任何索引数据、抓取缓存或导出清单。

## 功能

- **实时搜索**：pansou 聚合接口 + Telegram 公开频道（`t.me/s/`），SSE 流式返回，边搜边出
- **本地索引**：SQLite 存储分享链接，支持关键词/网盘/体积/时间/可获取状态筛选
- **链接验活**：夸克 / 百度 / 阿里 / 迅雷等分享状态机（可访问 / 需提取码 / 码错误 / 失效）
- **深度探测**：读取分享目录树，统计文件数、目录数、体积、视频数并分级
- **内容级去重**：按视频/文档清单生成指纹，识别“同课不同分享”，保留最干净的一份
- **我的资源库**：收藏、分类、标签、失效清单、导出
- **别名扩展**：把口语简称展开成标题里真实出现的写法（如 肖四 → 肖秀荣四套卷）

## 文件说明

| 文件 | 作用 |
|---|---|
| `pan_web.py` | Web 服务 + 前端页面（标准库 `http.server`，单文件） |
| `search_sources.py` | 搜索源引擎：pansou / TG / 自定义 JSON 源、查询变体、别名 |
| `live_search.py` | 早期实时搜索实现（保留兼容） |
| `crawl_channels.py` | TG 频道增量爬取（游标 + 主页搜索） |
| `crawl_full.py` | TG 频道全量爬取（多线程 + 游标） |
| `revalidate.py` | 批量复检链接有效性 / 探测目录 |
| `verify_access.py` | 单链接访问状态机（夸克/百度/阿里） |
| `NetDiskLinkValidator.py` | 异步批量验活（httpx + bs4） |
| `probe_deep.py` | 分享目录树探测与质量分级 |
| `content_dedup.py` | 夸克文件清单 + 内容指纹 |
| `fingerprint_v2.py` | 通用指纹（视频/文档）、同内容判定、漏斗选优 |
| `dedupe_v3.py` | 保守标题去重 |
| `dedupe_by_content.py` / `dedupe_items_by_content.py` | 按内容指纹去重（收藏库 / 索引库） |
| `fingerprint_saved.py` / `fingerprint_items.py` | 批量生成指纹缓存 |
| `migrate_meta.py` / `migrate_access.py` | 旧库字段回填 |
| `init_db.py` | 初始化空索引库 |

## 快速开始

要求 Python 3.10+。

```bash
pip install -r requirements.txt

# 1) 初始化空库
python init_db.py

# 2) 配置 pansou 地址（二选一）
#    a. 环境变量
export PANSOU_URL="http://127.0.0.1:8888/api/search?kw={kw}"      # Windows: set PANSOU_URL=...
#    b. 配置文件
cp search_sources.example.json search_sources.json                 # 然后改里面的 url

# 3) 启动 Web
python pan_web.py --port 8899 --host 0.0.0.0
```

打开 `http://127.0.0.1:8899`。首次启动时索引库是空的，实时搜索可以直接用；本地库要靠爬取积累。

## 可选：积累本地索引

```bash
# 增量爬取 TG 频道（默认每频道 3 页）
python crawl_channels.py --pages 3

# 全量爬取（较慢，建议后台跑）
python crawl_full.py

# 批量复检 + 目录探测
python revalidate.py --limit 3000 --probe-limit 400
```

`crawl_full.py` / `crawl_channels.py` 使用 `search_sources.py` 里的 `TG_CHANNELS_DEFAULT` 频道列表，可按需增删。

## 搜索源配置

`search_sources.json` 会在启动时与内置默认源合并。最小配置示例见 `search_sources.example.json`。

- `kind: pansou` —— 请求 `url`，解析 `data.merged_by_type`
- `kind: tg` —— 直接抓取 `t.me/s/<channel>?q=<kw>`
- `kind: json` —— 任意 JSON 接口，用 `list_path` / `fields` 描述字段位置

也可以在页面「设置」里增删源，保存后写入 `search_sources.json`。

## 数据与隐私

本仓库**不包含**以下内容，且已在 `.gitignore` 中排除：

- `pan_index.db`（索引与收藏数据库）
- `content_fp_*.json` / `tg_*.json` 等抓取与指纹缓存
- 任何导出的资源清单（`*.tsv` / `*.xlsx` / `*.txt`）
- 本地 `search_sources.json`（含你的实例地址）

## 免责声明

本项目只做**公开分享链接的检索与整理**，不存储、不托管、不转存任何文件内容，也不提供账号池或破解能力。请仅用于个人学习与资料检索，遵守所用网盘的服务条款及当地版权法规。
