# astrbot_plugin_soutu_search · AstrBot 搜图插件

为 [AstrBot](https://github.com/Soulter/AstrBot) 提供搜图能力：

- **以图搜图**：发送图片 → 调用 [soutubot.moe](https://soutubot.moe)（搜图Bot酱）做相似图检索
- **关键词搜图**：`/搜图 <关键词>` → 调用 [Safebooru](https://safebooru.org) DAPI 按标签检索

> 默认采用「**只回文字与来源链接、不发送缩略图**」的安全策略，避免在群聊中触发平台风控。

---

## 环境要求

- AstrBot `>=4.26.0,<5`
- Python 依赖：`aiohttp>=3.9.0`、`aiofiles>=23.0.0`（见 `requirements.txt`）

---

## 安装

1. 将本目录放入 AstrBot 的插件目录（通常为 `data/plugins/`）：
   ```
    data/plugins/astrbot_plugin_soutu_search/
   ```
2. 在 AstrBot 面板中安装依赖（或 `pip install -r requirements.txt` 到 AstrBot 运行环境）。
3. 重载插件。

---

## 指令用法

| 指令 | 说明 |
|---|---|
| `/搜图` + 附图 | 以图搜图（也支持引用一张图片后发送 `/搜图`，或 `/搜图 <图片链接>`） |
| `/搜图 <关键词>` | 关键词搜图，例如 `/搜图 cat_ears` |
| `/搜图帮助` | 输出用法说明 |
| 直接发图片 | 触发自动搜图（可在配置中关闭） |

- 直接发送图片（不写指令）时会**自动搜图**，受「自动搜图」开关与「冷却时间」限制。
- `/搜图` 不带图片也不带关键词时，返回帮助文本。

---

## 配置项（`_conf_schema.json`）

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable_auto_search` | bool | `true` | 是否监听图片消息自动搜图 |
| `nsfw_send_image` | bool | `false` | **是否在回复里发送缩略图。默认关闭：只回文字+链接** |
| `search_factor` | string(`1.2`/`1.4`) | `1.2` | soutubot 搜索模式（1.4=严格） |
| `result_count` | int | `3` | 返回结果条数 |
| `min_score` | int | `28` | 低于此分的结果不展示 |
| `auto_search_cooldown` | int | `30` | 同会话自动搜图冷却（秒） |
| `cache_ttl` | int | `3600` | 结果缓存时长（秒），设为 0 关闭缓存 |
| `request_timeout` | int | `30` | 请求超时（秒） |
| `max_reply_chars` | int | `1200` | 回复正文长度上限（字符），超出按字符边界截断并追加提示；`0` 表示不限制 |
| `max_image_bytes` | int | `10485760` | 单张图片大小上限（字节，默认 10MB），超出拒绝 |
| `safebooru_rating` | string(`safe`/`all`) | `safe` | 关键词搜图评级过滤 |
| `soutu_base_url` | string | `https://soutubot.moe` | 以图搜图服务地址 |
| `safebooru_base_url` | string | `https://safebooru.org` | 关键词搜图服务地址 |
| `access_mode` | string(`all`/`whitelist`/`blacklist`) | `all` | 访问控制模式（详见下方「访问控制」） |
| `whitelist` | list | `[]` | 白名单：允许使用本插件的会话/群（**空列表 = 全部拒绝**） |
| `blacklist` | list | `[]` | 黑名单：禁止使用本插件的会话/群（**空列表 = 不限制**） |
| `access_scope` | string(`all`/`auto`) | `all` | 限制范围：`all`=指令+自动搜图；`auto`=仅自动搜图 |

### 🚦 访问控制

用于把插件限定在指定群 / 会话使用（例如只在某个群里开放，或屏蔽某些捣乱群）。

#### 三种模式语义

| `access_mode` | 含义 | 列表为空时 |
|---|---|---|
| `all`（默认） | **不限制**，`whitelist` / `blacklist` **一律被忽略** | 无影响（不限制） |
| `whitelist` | **仅**列表内的会话可用，列表外一律不可用 | **fail-closed：全部会话都不可用** ⚠️ |
| `blacklist` | 列表内的会话不可用，列表外可用 | 不限制（放行全部） |

> **为什么 whitelist 空值要 fail-closed？**
> 白名单是「白名单安全模型」——只信任显式列出的对象。若空列表被解释为「放行全部」，
> 那么一次误配置（比如清空了列表却忘了关模式）就会让插件对**所有**群开放，属于危险的
> 失效开放（fail-open）。因此这里**刻意**选择 fail-closed：**列表为空 = 谁都不放行**，
> 迫使配置者先把白名单填对再启用。这是安全默认原则（拿不准就拒绝）的体现。

> **为什么 blacklist 空值不限制？**
> 黑名单是「黑名单安全模型」——只屏蔽显式列出的对象。空黑名单的自然语义就是「没有要屏蔽
> 的任何东西」，即放行全部。若空黑名单也 fail-closed，会导致「刚打开插件还没填名单就全体
> 被拒」的反直觉行为。因此白/黑名单在**空值处理上刻意不对称**，各自符合其安全模型直觉。

#### 限制范围（`access_scope`）

| `access_scope` | 受限的对象 |
|---|---|
| `all`（默认） | **指令**（`/搜图`、`/搜图帮助`）与**自动搜图**都受限 |
| `auto` | **仅自动搜图**受限；**指令照常响应** |

- 典型用法 `access_scope=auto`：群里不希望机器人看到图片就自动刷图，但仍允许群友**手动**
  发 `/搜图` 主动搜。

#### 受限时分别怎么表现？

- **自动搜图受限** → **静默跳过**，不回任何消息（避免在群里刷屏造成骚扰）。
- **指令受限** → 回一句简短提示「🚫 本会话未启用搜图功能，如需使用请联系管理员。」，
  让用户知道是**权限限制**而非插件故障。

#### 会话标识怎么填？（匹配规则）

判定时会构造一个**候选标识集合**，其中**任一**命中列表即视为命中：

1. `unified_msg_origin`（会话唯一 ID，简称 **umo**）—— 最精确，推荐；
2. `group_id`（**群号**）—— 私聊时通常为空，自动跳过；
3. **发送者 ID** —— 尽力获取（私聊场景下便于「按人限制」；取不到则跳过）。

> 匹配前会对候选值与列表项做**字符串化 + `strip()`**，并**忽略无效项**。
> 因此群号既可以写成数字 `123456`，也可以写成字符串 `"123456"`。

#### 填写与容错规则（重要）

名单配置（`whitelist` / `blacklist`）在**每次判定时**动态读取并归一化，规则如下：

| 输入形态 | 处理方式 |
|---|---|
| `list` / `tuple` / `set` | 逐项归一化（嵌套序列递归展开；嵌套字符串同样按分隔符拆分） |
| **字符串** | 按**逗号 / 空白 / 换行**分割为多项：`"123456"` → 一项；`"123456, 789012"` → 两项 |
| 其它类型（`int` / `dict` / …） | **打日志告警**（`访问控制名单配置类型异常`）后按**空**处理，**不会静默吞掉配置错误** |

单项归一化时会**跳过**下列"空值 / 非法值"，保证不会误配成有效标识：

- `None`（避免 `str(None) == "None"` 被当成合法 ID）；
- 布尔值 `True` / `False`（`"True"` / `"False"` 不是会话 ID）；
- 数值 `0`（`0` / `0.0` 不是会话 ID）；
- `strip()` 后为空串的项。

> 归一化后：**白名单为空集合 → fail-closed（全部拒绝）**；**黑名单为空集合 → 不限制（全部放行）**。
> 也就是说 `whitelist: [None]` 等价于空白名单，会拒绝所有会话——这正是我们要的 fail-closed 行为。
> 字符串形态的支持则是为了**避免把"误写成字符串"的黑名单静默丢弃**（那会导致危险的 fail-open）。

#### 填写示例

**① 只允许两个群使用（按群号）**
```jsonc
{
  "access_mode": "whitelist",
  "whitelist": ["123456789", "987654321"],
  "access_scope": "all"
}
```

**② 屏蔽某个捣乱群（按群号）**
```jsonc
{
  "access_mode": "blacklist",
  "blacklist": ["111222333"]
}
```

**③ 按会话唯一 ID（umo）精确限制**——umo 形如 `aiocqhttp:GroupMessage:123456789`
```jsonc
{
  "access_mode": "whitelist",
  "whitelist": ["aiocqhttp:GroupMessage:123456789"]
}
```

**④ 仅限制自动搜图，指令仍可用（群内不自动刷图）**
```jsonc
{
  "access_mode": "blacklist",
  "blacklist": ["111222333"],
  "access_scope": "auto"
}
```

### ⚠️ 安全提示

`soutubot.moe` 的命中源以 `nhentai` / `ehentai`（R18 内容）为主。
`nsfw_send_image=false`（默认）时，回复**只包含文字与来源详情链接，不含任何 `Image` 组件或缩略图 URL**。
请谨慎开启，避免群聊被平台风控。

### 🔒 图片输入安全约束

`core/image_source.py` 对图片来源做三重校验，防止 SSRF 与本地任意文件读取：

1. **协议白名单**：URL 仅允许 `http` / `https`；`ftp:` / `gopher:` / `file:` 等一律拒绝
   （`file:` 走本地文件分支，受第 4 条约束）。
2. **内网/保留地址拦截（含 DNS 解析，fail-closed）**：目标 host 分两层校验——
   - **第一层（字面量，免 DNS）**：识别标准 IP 及**混淆形式** IPv4（十进制 `2130706433`、
     十六进制 `0x7f000001`、八进制 `0177.0.0.1`、短写 `127.1`）与 IPv4-mapped IPv6
     （`::ffff:127.0.0.1`）；命中 `127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、
     `169.254.0.0/16`、`localhost`、`::1`、`0.0.0.0` 等即拒绝。
   - **第二层（域名 DNS 解析）**：对域名执行 `socket.getaddrinfo`（在异步路径中放入线程池执行，
     **不阻塞事件循环**），**逐一**校验解析出的所有 IP，任一落在上述地址段即拒绝；可拦截
     `127.0.0.1.nip.io` 这类通配 DNS。
   - **fail-closed**：**DNS 解析失败（无结果或异常）一律视为拒绝**，不放行。这同时兜住
     `2130706433` 等「在 Windows 上解析失败但在 glibc 上可解析」的跨平台形态。
3. **重定向逐跳校验**：**禁用自动重定向**，手动处理 3xx；对每个 `Location` 重新做
   协议白名单 + 解析后 IP 校验（最多 5 跳），杜绝「公网 → 内网」的重定向绕过。
4. **本地文件目录白名单**：本地路径 / `file://` 解析为绝对路径后，必须位于**允许的根目录之内**，
   否则拒绝。生产环境下允许的根目录为 **AstrBot 数据目录**（`ImageSource(allowed_roots=[data_dir])`）。
   ⚠️ 若插件未配置 `allowed_roots`，则**默认拒绝一切本地文件**（安全默认）。

> 安全默认原则：**拿不准就拒绝**。DNS 解析失败、无法判定归属、路径越界，一律拒绝并降级为友好提示。

### 🖼 图片内容校验

- **大小上限**：超过 `max_image_bytes`（默认 10MB）直接拒绝。
- **魔数校验**：按文件头判定真实类型（JPEG `FFD8FF`、PNG `89504E47`、GIF `474946`、
  WEBP `RIFF....WEBP`、BMP `424D`），非图片字节一律拒绝。
- **MIME 以魔数推断为准**：不沿用来源声明的 MIME（例如声明 `image/jpeg` 实为 PNG，则按 PNG 处理）。

### 🔁 错误处理不对称（有意为之）

- **soutubot.moe**：接口正常时必定返回 JSON。若返回 **HTTP 200 + 非 JSON**（典型如 Cloudflare
  挑战页），客户端**会抛出可读的 `RuntimeError`**（提示「非 JSON，可能被 Cloudflare 拦截」），
  不静默返回空——否则用户会误以为「没搜到」。
- **Safebooru**：无结果时会返回空响应体 / 空数组，属正常情形，因此**优雅返回空结果 + warning**，
  不抛异常。

这一不对称是刻意设计：前者「非 JSON 一定是异常」，后者「非 JSON 可能是正常无结果」。

---

## 架构

采用 **provider 适配器模式**，以图搜图与关键词搜图两个图源完全解耦：

```
astrbot_plugin_soutu_search/
├── main.py                      # 插件入口：指令注册、事件监听、结果回复
├── metadata.yaml                # 插件元数据
├── _conf_schema.json            # 配置 Schema
├── requirements.txt             # 依赖声明
├── LICENSE                      # MIT 许可证
├── .gitignore                   # Git 忽略规则
├── core/
│   ├── __init__.py
│   ├── image_source.py          # 图片获取与规范化（URL/本地/data URI → bytes）
│   ├── soutu_client.py          # 搜图Bot酱 以图搜图 provider（响应解析 + schema 容错）
│   ├── safebooru_client.py      # Safebooru 关键词搜图 provider
│   ├── cache.py                 # TTL 缓存（key: 图片 sha256 / 关键词）
│   └── formatter.py             # 统一结果模型与消息链格式化
├── tests/
│   ├── test_core.py             # 核心单元测试（无需 AstrBot 本体）
│   ├── test_hardening.py        # 安全/截断/判重等加固回归测试
│   └── test_access_control.py   # 群/会话黑白名单访问控制测试
└── README.md
```

### 公共接口

`core.formatter` 定义了两个统一数据模型：

```python
@dataclass
class SearchResult:
    title: str                 # 标题
    source: str                # 中文可读来源名（如「NH本子」「E站」）
    url: str                   # 详情页链接
    thumbnail: str | None      # 缩略图 URL（NSFW 关闭时不展示）
    score: float | None        # 相似度（0-100）
    extra: dict                # page_no / rating / tags / low_confidence / tier 等

@dataclass
class SourceOutcome:
    results: list[SearchResult]
    warnings: list[str]        # partial、低置信度等提示
    meta: dict                 # schema_version / hit_count / partial 等
```

两个 provider 均返回 `SourceOutcome`：

- `SoutuClient.search(image, *, filename, mime, factor, top_k) -> SourceOutcome`
- `SafebooruClient.search_by_tags(tags, *, limit, page, rating) -> SourceOutcome`

`soutu_client.parse_soutu_response` 与 `safebooru_client.parse_safebooru_response`
均为**纯函数**，便于离线单测。

---

## 业务规则

### soutubot.moe（以图搜图）

- 命中判定：`results[].path_segments` 非空才算命中
- 置信度阈值：`factor == 1.4` 时为 **35**，否则为 **45**；低于阈值标注「⚠️低置信度」
- 结果分档：`score >= 28` 进主列表，`< 28` 归为低分结果（受 `min_score` 过滤）
- 标题回退链：`metadata.title.primary` → `metadata.title`(平铺字符串) → `metadata.title.japanese_or_alias`
- 链接优先级：`page_url` → `chapter_url` → `source_url` → `metadata.source.url`
- 所有字段均以 `.get()` 取值并给默认值，字段缺失不抛 `KeyError`

### Safebooru（关键词搜图）

- `pid` 从 0 开始；多标签用 `+` 连接
- `rating` 为 `safe` 时过滤 `sensitive` / `questionable` / `explicit`
- 无结果时返回空数组或空响应体，均容错处理（不抛 `JSONDecodeError`）

---

## 运行测试

无需安装、无需启动 AstrBot 本体：

```bash
python -m unittest tests.test_core -v
# 或
python tests/test_core.py
```

测试在导入插件模块前，用 `unittest.mock` 将 `astrbot.*` 模块注入 `sys.modules`，
覆盖解析容错、阈值/分档、缓存、NSFW 关闭时不产生图片组件等关键路径。

---

## 已知限制

- 外部站点（soutubot.moe / safebooru.org）为第三方个人运营，接口可能随时变更；插件已做
  schema 容错与错误降级，但仍可能出现接口不可用。
- 缓存为**进程内内存缓存**，插件重启后失效；不做磁盘持久化。
- **本地图片来源受限**：仅允许读取 AstrBot 数据目录内的文件（PSRC 加固）；若你的部署把图片
  缓存在其它目录，需相应调整 `ImageSource(allowed_roots=...)`。QQ 图片通常走 HTTP URL 分支，不受影响。
- 回复正文默认截断到 `max_reply_chars=1200` 字符，超出追加「…（结果过长已截断）」。
- QQ 图片下载存在防盗链，已做 UA/Referer 两级重试，极端情况下仍可能失败并降级为友好提示。
