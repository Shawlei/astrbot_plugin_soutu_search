# astrbot_plugin_soutu_search · AstrBot 搜图插件

为 [AstrBot](https://github.com/Soulter/AstrBot) 提供搜图能力：

- **以图搜图**：发送图片并用「搜图」指令 → 调用 [soutubot.moe](https://soutubot.moe)（搜图Bot酱）做相似图检索
- **关键词搜图**：`搜图 <关键词>` → 调用 [Safebooru](https://safebooru.org) DAPI 按标签检索
- **搜 P 站（反查出处）**：发送图片并用「搜P站」指令 → 调用 [SauceNAO](https://saucenao.com) 反查图片出处/画师，**默认仅检索 Pixiv 库**

> 默认采用「**只回文字与来源链接、不发送缩略图**」的安全策略，避免在群聊中触发平台风控。
> 搜图**只能通过指令触发**：插件**不监听消息、不会自动搜图**，群内有人发图不会被自动识别。
> 指令前缀**跟随 AstrBot 全局配置的命令前缀**（`wake_prefix`，默认 `/`）；下文示例以默认的 `/` 书写，
> 若你把前缀配成 `#`，则实际命令是 `#搜图`、`#搜图帮助`、`#搜P站`。

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

> `命令前缀` 跟随 AstrBot 全局配置（顶层 `wake_prefix`）。下表以默认前缀 `/` 书写；
> 若你的前缀是 `#`，把 `/搜图` 读作 `#搜图`。

| 指令 | 说明 |
|---|---|
| `命令前缀 + 搜图` + 附图 | 以图搜图（也支持引用一张图片后发送，或 `搜图 <图片链接>`） |
| `命令前缀 + 搜图 <关键词>` | 关键词搜图，例如 `/搜图 cat_ears` |
| `命令前缀 + 搜P站` + 附图 | **搜 P 站**：SauceNAO 反查出处/画师（默认仅 Pixiv 库；需配置 API Key） |
| `命令前缀 + 搜图帮助` | 输出用法说明 |
| `命令前缀 + 搜P站帮助` | 输出「搜 P 站」用法说明 |

- 搜图**只能通过指令触发**：单纯发图片（不带指令）**不会**搜索（插件不监听消息）。
- `/搜图` 不带图片也不带关键词时，返回帮助文本。
- 别名：`soutu`、`找图`；帮助别名：`搜图help`、`soutuhelp`。
- **搜 P 站**别名：`pixiv`、`saucenao`（如 `/pixiv`、`/saucenao`）；帮助别名：`搜P站help`、`saucenaohelp`。

### 🔤 命令前缀跟随（`wake_prefix`）

指令前缀**不写死**，而是读取 AstrBot 全局配置的**顶层** `wake_prefix` 字段（`list` 类型，默认 `["/"]`）：

- **帮助文案**用实际前缀渲染（如 `#搜图`、`#搜图 <关键词>`、`#搜图帮助`）；配置多个前缀时取**第一个**。
- **指令识别**优先按配置前缀逐个剥离（`str.startswith`，支持多个，且**不受前缀含正则元字符影响**）；
  配置前缀未命中时回退通用前缀正则（兼容 `/`、`!`、`。` 等）。
- 值为 `str`（单前缀）/ `list`（多前缀）均可；`None`、`[]`、`""`、非法类型或 `get_config()` 异常时
  **回退通用正则**，不会报错；列表中混入的非字符串项会被跳过。

---

## 配置项（`_conf_schema.json`）

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `nsfw_send_image` | bool | `false` | **是否在回复里发送缩略图。默认关闭：只回文字+链接** |
| `search_factor` | string(`1.2`/`1.4`) | `1.2` | soutubot 搜索模式（1.4=严格） |
| `result_count` | int | `3` | 返回结果条数 |
| `min_score` | int | `28` | 低于此分的结果不展示 |
| `cache_ttl` | int | `3600` | 结果缓存时长（秒），设为 0 关闭缓存 |
| `request_timeout` | int | `30` | 请求超时（秒） |
| `max_reply_chars` | int | `1200` | 回复正文长度上限（字符），超出按字符边界截断并追加提示；`0` 表示不限制 |
| `max_image_bytes` | int | `10485760` | 单张图片大小上限（字节，默认 10MB），超出拒绝 |
| `safebooru_rating` | string(`safe`/`all`) | `safe` | 关键词搜图评级过滤 |
| `soutu_base_url` | string | `https://soutubot.moe` | 以图搜图服务地址 |
| `safebooru_base_url` | string | `https://safebooru.org` | 关键词搜图服务地址 |
| `saucenao_api_key` | string（`secret`） | `""` | **搜 P 站** 的 SauceNAO API Key（申请：<https://saucenao.com/user.php?page=search-api>）。未填写时「搜P站」给出引导、不发起请求 |
| `saucenao_base_url` | string | `https://saucenao.com` | SauceNAO 接口地址，可改镜像/反代以应对网络问题 |
| `saucenao_db_mask` | int | `96` | 数据库位掩码。**`96` = 仅 Pixiv**（`0x20|0x40`）；`0` = 不限库（搜全部） |
| `saucenao_min_similarity` | int | `50` | SauceNAO 最低相似度（0-100）；与 soutubot 的 `min_score` 体系不同，故独立配置 |
| `saucenao_hide` | int | `0` | SauceNAO 内容过滤：`0`=全显示、`1`=隐藏预期 R18、`2`=隐藏预期可疑、`3`=只留安全 |
| `access_mode` | string(`all`/`whitelist`/`blacklist`) | `all` | 访问控制模式（详见下方「访问控制」） |
| `whitelist` | list | `[]` | 白名单：允许使用本插件的会话/群（**空列表 = 全部拒绝**） |
| `blacklist` | list | `[]` | 黑名单：禁止使用本插件的会话/群（**空列表 = 不限制**） |
| `extra_allowed_roots` | list | `[]` | 额外允许读取的本地图片根目录（一般无需填写，仅应对 AstrBot 版本差异导致的临时目录变化） |

> 共 **20** 项配置。命令前缀不在本插件配置中，而是跟随 AstrBot 全局 `wake_prefix`。
> `saucenao_db_mask` 非法值（负数/非整数）回退 `96`；`saucenao_min_similarity` 越界回退 `50`；
> `saucenao_hide` 不在 0-3 回退 `0`。

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

#### 受限时怎么表现？

访问控制**只作用于指令通道**（本插件没有自动搜图）。被限制的会话发送指令时，回一句简短提示
「🚫 本会话未启用搜图功能，如需使用请联系管理员。」，让用户知道是**权限限制**而非插件故障。

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
  "whitelist": ["123456789", "987654321"]
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
     **另拒绝一切非全局单播地址（`not is_global`）**，兜住那些"既非内网、也非保留"的边角段——
     典型即 **CGNAT / 共享地址 `100.64.0.0/10`（RFC 6598）**，其 `is_private`/`is_reserved`/`is_global`
     **同时为 False**，旧判定会漏放；该段现已连同其 IPv4-mapped 形态（`::ffff:100.64.0.1`）一并拒绝。
     （新增的"图片直链"入口使该段首次可由用户输入触达，故纳入纵深防御。）
   - **第二层（域名 DNS 解析）**：对域名执行 `socket.getaddrinfo`（在异步路径中放入线程池执行，
     **不阻塞事件循环**），**逐一**校验解析出的所有 IP，任一落在上述地址段即拒绝；可拦截
     `127.0.0.1.nip.io` 这类通配 DNS。
   - **fail-closed**：**DNS 解析失败（无结果或异常）一律视为拒绝**，不放行。这同时兜住
     `2130706433` 等「在 Windows 上解析失败但在 glibc 上可解析」的跨平台形态。
3. **重定向逐跳校验**：**禁用自动重定向**，手动处理 3xx；对每个 `Location` 重新做
   协议白名单 + 解析后 IP 校验（最多 5 跳），杜绝「公网 → 内网」的重定向绕过。
4. **本地文件目录白名单**：本地路径 / `file://` 解析为绝对路径后，必须位于**允许的根目录之内**，
   否则拒绝。允许的根目录由三部分组成（**最小授权**）：
   1. **插件自身数据目录**；
   2. **AstrBot 的 `data/temp` 目录**（AstrBot 收到图片后会先落盘到 `data/temp/media_image_*.jpg`，
      再以本地路径传给插件；具体 `data` 根目录通过官方 API 或从插件数据目录向上回溯得到，
      **不硬编码任何绝对路径**）；
   3. 用户配置的 `extra_allowed_roots`。
   出于安全考虑，**不会**放行整个 AstrBot `data/` 目录（其中含各插件配置，可能含密钥）。
   ⚠️ 若插件未配置任何 `allowed_roots`，则**默认拒绝一切本地文件**（安全默认）。
   被拒时的日志会同时打印**被拒路径**与**当前允许根目录列表**，便于区分「配置漏目录」还是「真越权」。

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

## 🎯 SauceNAO / 搜 P 站

「搜P站」用 [SauceNAO](https://saucenao.com) 做**以图反查**，专治「这张图出自哪个 Pixiv 作品 / 谁画的」。

### 与 soutubot 的区别

| | soutubot.moe（`搜图`） | SauceNAO（`搜P站`） |
|---|---|---|
| 定位 | 偏**本子 / 里番**截图溯源（nhentai、ehentai、禁漫等） | 偏**原创插画站**（Pixiv 等）出处 / 画师反查 |
| 触发 | `搜图`（附图） | `搜P站`（附图），别名 `pixiv` / `saucenao` |
| 是否要 Key | 不需要 | **需要 API Key** |
| 可限定库 | 否 | **是**（`saucenao_db_mask` 位掩码） |
| 额度 | 无明确限制 | **150 次/天、4 次/30 秒**（免费账户） |

> 两条指令**刻意分开、互不并跑**：SauceNAO 限额很紧（4 次/30 秒），若挂在每次 `搜图` 上并跑会迅速耗尽配额。

### 配置 API Key

1. 到 <https://saucenao.com/user.php?page=search-api> 注册并获取 API Key；
2. 在插件配置中填入 `saucenao_api_key`（该字段为密文 `secret`）。
3. **未配置**时，「搜P站」会直接回一条引导提示，**不会**发起请求（避免无谓消耗）。

### `dbmask` 用法与常用掩码

`saucenao_db_mask` 是**位掩码**：把想启用的库掩码用**按位或**相加（官方口径是十六进制相加后转十进制）。

| 库 | 掩码（十六进制） | 十进制 |
|---|---|---|
| pixiv | `0x20` | 32 |
| pixivhistorical | `0x40` | 64 |
| danbooru | `0x200` | 512 |
| yande.re | `0x1000` | 4096 |
| Twitter | `0x10000000000` | 1099511627776 |

- **默认 `96` = `0x20 | 0x40` = 仅 Pixiv（pixiv + pixivhistorical）** —— 即「专门搜 P 站」。
- `0` 表示**不限库**（搜全部库），可能混入 booru / 书籍等其它来源。
  > ⚠️ 位掩码惯例下 `0` 也可能被服务端理解为「不启用任何库」。为避免误伤，插件在 `db_mask=0` 时
  > **不发送 `dbmask` 参数**（不传即由服务端按默认全库处理）。该行为为离线推断，**待真机确认**（见「已知限制」）。
- 想「Pixiv + Twitter」：`0x20 | 0x40 | 0x10000000000`。
- 非法值（负数 / 非整数）会回退为 `96`。

### 配额限制（重要）

免费账户：**150 次/天**、**4 次/30 秒**。响应头会回传剩余额度，插件会解析并在耗尽时明确提示：

- `long_remaining == 0` → 「今日配额已用完，请明天再试」；
- `short_remaining == 0` → 「触发了限流（4 次/30 秒），请等待约 30 秒后再试」。

### 网络与连通性

**中国大陆访问 `saucenao.com` 通常需要代理。** 插件遵循 AstrBot 的全局 `http_proxy` 配置；
也可用 `saucenao_base_url` 指向自建镜像 / 反代。

> ⚠️ 本插件的**开发环境连不通 saucenao.com**（TLS 连接重置），因此 SauceNAO 相关逻辑以
> 规范 + 构造样例做了充分单测，但**未在本机做真实联网端到端验证**。请在你自己（可联网）的机器上运行
> `python tests/live_saucenao_check.py` 复验（见下文「联网校验脚本」）。

---

## 架构

采用 **provider 适配器模式**，以图搜图（soutubot）、关键词搜图（Safebooru）与以图反查（SauceNAO）
三个图源完全解耦：

```
astrbot_plugin_soutu_search/
├── main.py                      # 插件入口：指令注册、前缀跟随、访问控制、结果回复
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
│   ├── saucenao_client.py       # SauceNAO 以图反查 provider（可限定 Pixiv 库，即「搜 P 站」）
│   ├── cache.py                 # TTL 缓存（key: 图片 sha256 / 关键词 / SauceNAO 独立命名空间）
│   └── formatter.py             # 统一结果模型与消息链格式化
├── tests/
│   ├── test_core.py             # 核心单元测试（无需 AstrBot 本体）
│   ├── test_hardening.py        # 安全/截断/指令判定等加固回归测试
│   ├── test_access_control.py   # 群/会话黑白名单访问控制测试
│   ├── test_adversarial.py      # 对抗性/边界用例（指令判定、SSRF、输入畸形等）
│   ├── test_http_errors.py      # HTTP 异常与错误降级测试
│   ├── test_local_image_roots.py# 本地图片来源白名单（allowed_roots / temp 放行）测试
│   ├── test_wake_prefix.py      # 命令前缀跟随（wake_prefix）测试
│   ├── test_saucenao.py         # SauceNAO provider（掩码/解析/配额/指令/访问控制）测试
│   └── live_saucenao_check.py   # SauceNAO 真人联网校验脚本（需可联网环境，见下）
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

三个 provider 均返回 `SourceOutcome`：

- `SoutuClient.search(image, *, filename, mime, factor, top_k) -> SourceOutcome`
- `SafebooruClient.search_by_tags(tags, *, limit, page, rating) -> SourceOutcome`
- `SaucenaoClient.search(image, *, filename, mime) -> SourceOutcome`（用 `db_mask` 限定库）

`soutu_client.parse_soutu_response`、`safebooru_client.parse_safebooru_response` 与
`saucenao_client.parse_saucenao_response` 均为**纯函数**，便于离线单测。
`saucenao_client` 另有 `resolve_db_mask` / `resolve_min_similarity` / `resolve_hide` 等配置归一化函数
（非法值一律回退安全默认）。

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

### SauceNAO（搜 P 站）

- 请求：`POST /search.php`（multipart 字段名 `file`）+ 查询参数 `output_type=2 / dbmask / numres / minsim / hide / api_key`
- 解析容错（真实会踩的坑）：
  - `results[].header.similarity` 是**字符串**，安全转 `float`，非法/缺失不崩；
  - `header.status != 0` → 抛**可读 `RuntimeError`**（不静默返回空）；
  - `results` 为 `[]` / 缺失 / 非 list → 优雅返回无结果；
  - `data` 结构**因库而异**（Pixiv 有 `pixiv_id`/`member`/`creator`；booru 类有 `source`/`material`；书籍类有 `part`/`year`），按可用字段回退；
  - **链接回退链**：`ext_urls[0]` → 由 `pixiv_id` 拼 `https://www.pixiv.net/artworks/{id}` → `data.source` → 无则标记无链接；
  - **画师**：取 `creator` / `author_name`，退而取 `member`（uid，拼 `https://www.pixiv.net/users/{id}`）；
  - 命中库名：`index_name`（缺失时按 `index_id` 回退）展示为「来自 <库> 库」；
  - **缩略图**带 `auth`/`exp` 签名会过期，**只走「先下载再发」的图片组件，绝不外传文本**（NSFW 关闭时不发图）。
- 配额：解析 `short_remaining` / `long_remaining`，耗尽时给出明确提示（不解析到也不报错）。

---

## 运行测试

无需安装、无需启动 AstrBot 本体：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
# 或单文件
python -m unittest tests.test_core -v
```

测试在导入插件模块前，用 `unittest.mock` 将 `astrbot.*` 模块注入 `sys.modules`，
覆盖解析容错、阈值/分档、缓存、NSFW 关闭时不产生图片组件、访问控制、本地图片来源白名单、
命令前缀跟随、SauceNAO（掩码/解析/配额/指令路由/配置一致性）等关键路径。

### 联网校验脚本（SauceNAO）

> 本仓库的**开发环境连不通 `saucenao.com`**，SauceNAO 逻辑未在本机做真实联网端到端验证。
> 请在你**可联网**（大陆通常需代理）的机器上运行下面的脚本复验：

```bash
# 方式一：环境变量提供 API Key（可选 --image 指定图片，默认用 recon/test.jpg）
export SAUCENAO_API_KEY=你的key            # bash；Windows 用 set SAUCENAO_API_KEY=你的key
python tests/live_saucenao_check.py

# 方式二：命令行参数 + 自建反代
python tests/live_saucenao_check.py --api-key 你的key --image path/to/pic.jpg --base-url https://你的反代/

# 方式三：离线自检请求构造（不发网络请求，专门核对 multipart 字段名 / 参数脱敏）
python tests/live_saucenao_check.py --self-test --api-key 你的key
```

脚本会依次打印：**连通性 / HTTP 状态码**、**实际发出的 multipart 字段名与查询参数**（**`api_key` 已脱敏为前 3 位 + `***`**）、
**解析出的结果条数**、**首条相似度 / 标题 / 链接 / 画师**，以及 **`header` 里的配额字段**
（`short_remaining` / `long_remaining` / `short_limit` / `long_limit`）；连不通时给出可读提示并优雅退出。
即使网络反查失败，脚本**仍会打印请求自检块**（请求已构造/发出），便于核对未实测的字段名 `file`。

---

## 已知限制

- 外部站点（soutubot.moe / safebooru.org）为第三方个人运营，接口可能随时变更；插件已做
  schema 容错与错误降级，但仍可能出现接口不可用。
- 缓存为**进程内内存缓存**，插件重启后失效；不做磁盘持久化。
- **本地图片来源受限**：默认仅允许读取**插件自身数据目录**与 **AstrBot 的 `data/temp`** 内的文件
  （PSRC 加固）。QQ 图片通常走 HTTP URL 分支，不受影响。若你的部署因 AstrBot 版本差异把图片
  缓存在其它目录、并出现「本地图片路径不在允许目录内」，可用配置项 `extra_allowed_roots` 追加
  （被拒日志会打印当前允许根目录，便于定位）。
- 回复正文默认截断到 `max_reply_chars=1200` 字符，超出追加「…（结果过长已截断）」。
- QQ 图片下载存在防盗链，已做 UA/Referer 两级重试，极端情况下仍可能失败并降级为友好提示。
- **命令前缀**跟随 AstrBot 全局 `wake_prefix`；若插件读取不到该配置（旧版本/异常），
  会回退到通用前缀正则（兼容 `/`、`!`、`。` 等），仅帮助文案的前缀可能与实际不同。
- **SauceNAO（搜P站）**：
  - **需要 API Key**（`saucenao_api_key`）；未配置时指令直接给出引导、不发起请求。
  - **配额紧**：免费账户 150 次/天、4 次/30 秒；耗尽时插件会明确提示，而非「搜不到图」。
  - **连通性**：中国大陆访问 `saucenao.com` 通常需要代理（遵循 AstrBot 全局 `http_proxy`），
    或用 `saucenao_base_url` 指向镜像 / 反代。
  - **本机（开发环境）未做真实联网验证**：因 `saucenao.com` 在本机不可达（TLS 连接重置），
    SauceNAO 逻辑仅以规范 + 构造样例做离线单测；请用 `tests/live_saucenao_check.py` 在你自己的
    可联网机器上复验（含 multipart 字段名 `file`、`dbmask=96` 是否确只返回 Pixiv、配额字段等）。
  - **`dbmask=0`（不限库）行为待真机确认**：插件在掩码为 `0` 时**不发送** `dbmask` 参数
    （按"省略即全库"推断），避免误发 `dbmask=0`（位掩码惯例下可能表示"不启用任何库"）导致搜不到。
  - `搜图` / `搜P站` 均支持 **`<指令> <图片链接>`** 直链（走 `image_source` 的协议白名单 + SSRF 校验）；
    链接不可达 / 非图片 / 内网地址时给出明确提示，不会静默失败。
