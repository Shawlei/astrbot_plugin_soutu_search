"""astrbot_plugin_soutu_search 核心模块。

各模块职责：
- image_source:  把消息中的图片（URL / 本地路径 / data URI）统一规范化为 bytes
- soutu_client:  搜图Bot酱（soutubot.moe）以图搜图 provider
- safebooru_client: Safebooru 关键词搜图 provider
- saucenao_client: SauceNAO 以图反查 provider（可限定库，默认全部活跃库）
- ascii2d_client: ascii2d 以图反查 provider（覆盖 Pixiv/Twitter 等画师首发站，免 Key）
- cache:         TTL 结果缓存
- formatter:     统一结果模型（SearchResult/SourceOutcome）与消息块格式化
"""
