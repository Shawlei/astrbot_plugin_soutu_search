"""astrbot_plugin_soutu_search 核心模块。

各模块职责：
- image_source:  把消息中的图片（URL / 本地路径 / data URI）统一规范化为 bytes
- soutu_client:  搜图Bot酱（soutubot.moe）以图搜图 provider
- safebooru_client: Safebooru 关键词搜图 provider
- saucenao_client: SauceNAO 以图反查 provider（可限定 Pixiv 库，即「搜 P 站」）
- cache:         TTL 结果缓存
- formatter:     统一结果模型（SearchResult/SourceOutcome）与消息块格式化
"""
