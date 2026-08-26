"""
Paper-Web 全局配置
复制此文件为 config.py 并填入实际值。config.py 已被 .gitignore 排除，不会提交到仓库。
所有配置项均支持环境变量覆盖（加 PAPER_ 前缀），优先级：环境变量 > config.py 默认值。
"""
import os

# ── Web 服务 ─────────────────────────────────────────────────────────────
SERVER_PORT = int(os.environ.get("PAPER_SERVER_PORT", 3475))
SERVER_DEBUG = os.environ.get("PAPER_SERVER_DEBUG", "false").lower() == "true"

# ── 访问密码 ──────────────────────────────────────────────────────────────
# 浏览器访问时会弹出登录框。部署前务必修改默认密码。
ACCESS_USERNAME = os.environ.get("PAPER_ACCESS_USERNAME", "paper")
ACCESS_PASSWORD = os.environ.get("PAPER_ACCESS_PASSWORD", "")

# ── LLM / OpenAI 兼容 API ────────────────────────────────────────────────
FILTER_BASE_URL = os.environ.get("PAPER_FILTER_BASE_URL", "")
FILTER_API_KEY = os.environ.get("PAPER_FILTER_API_KEY", "your-api-key-here")
FILTER_MODEL = os.environ.get("PAPER_FILTER_MODEL", "")

TRANSLATE_BASE_URL = os.environ.get("PAPER_TRANSLATE_BASE_URL", "")
TRANSLATE_API_KEY = os.environ.get("PAPER_TRANSLATE_API_KEY", "your-api-key-here")
TRANSLATE_MODEL = os.environ.get("PAPER_TRANSLATE_MODEL", "")
FILTER_PROMPT = os.environ.get("PAPER_FILTER_PROMPT", """你是一个凝聚态物理专家。任务：
1. 判定列表中的论文是否与"输运(transport)"或"电子输运"研究相关。相关返回 is_transport=true，否则 false。
2. 若相关，必须把英文标题和英文摘要精准且完整地翻译为流畅的学术中文。
3. 提供一个 150 字以内的中文总结 summary_zh。

要求仅返回严格格式的 JSON 数据，格式如下：
{
  "results": [
    {
      "index": <整数，原列表序号>,
      "is_transport": <布尔值>,
      "title_zh": "<如果相关则提供中文标题>",
      "summary_zh": "<中文核心总结，150字以内>",
      "abstract_zh": "<完整客观的全文翻译>"
    }
  ]
}
不要返回 json 之外的任何 Markdown。""")

# ── AI 筛选可靠性 ─────────────────────────────────────────────────────────
# 并发限制为 2–4，避免过高并发触发上游限流或耗尽本机资源。
AI_MAX_CONCURRENCY = int(os.environ.get("PAPER_AI_MAX_CONCURRENCY", 2))
AI_MAX_ATTEMPTS = int(os.environ.get("PAPER_AI_MAX_ATTEMPTS", 5))
AI_RETRY_BASE_DELAY = float(os.environ.get("PAPER_AI_RETRY_BASE_DELAY", 2.0))

# ── doc2x（PDF → LaTeX 转换服务）────────────────────────────────────────
DOC2X_API_KEY = os.environ.get("PAPER_DOC2X_API_KEY", "your-doc2x-api-key-here")
DOC2X_BASE_URL = os.environ.get("PAPER_DOC2X_BASE_URL", "https://v2.doc2x.noedgeai.com/api/v2")

# ── 翻译参数 ──────────────────────────────────────────────────────────────
TRANSLATE_CHUNK_SIZE = int(os.environ.get("PAPER_TRANSLATE_CHUNK_SIZE", 20000))

# ── 每日定时任务 ──────────────────────────────────────────────────────────
DAILY_FETCH_HOUR = int(os.environ.get("PAPER_DAILY_FETCH_HOUR", 9))  # 北京时间
RECENT_DAYS = int(os.environ.get("PAPER_RECENT_DAYS", 3))  # RSS 保留最近 N 天
