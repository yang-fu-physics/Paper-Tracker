"""
Paper-Web 全局配置
复制此文件为 config.py 并填入实际值。config.py 已被 .gitignore 排除，不会提交到仓库。
所有配置项均支持环境变量覆盖（加 PAPER_ 前缀），优先级：环境变量 > config.py 默认值。
"""
import os

# ── Web 服务 ─────────────────────────────────────────────────────────────
SERVER_PORT = int(os.environ.get("PAPER_SERVER_PORT", 3475))
SERVER_DEBUG = os.environ.get("PAPER_SERVER_DEBUG", "false").lower() == "true"

# ── LLM / OpenAI 兼容 API ────────────────────────────────────────────────
OPENAI_BASE_URL = os.environ.get("PAPER_OPENAI_BASE_URL", "http://127.0.0.1:7861")
OPENAI_API_KEY = os.environ.get("PAPER_OPENAI_API_KEY", "your-api-key-here")
OPENAI_MODEL = os.environ.get("PAPER_OPENAI_MODEL", "gemini-3-pro-preview")

# ── doc2x（PDF → LaTeX 转换服务）────────────────────────────────────────
DOC2X_API_KEY = os.environ.get("PAPER_DOC2X_API_KEY", "your-doc2x-api-key-here")
DOC2X_BASE_URL = os.environ.get("PAPER_DOC2X_BASE_URL", "https://v2.doc2x.noedgeai.com/api/v2")

# ── 翻译参数 ──────────────────────────────────────────────────────────────
TRANSLATE_CHUNK_SIZE = int(os.environ.get("PAPER_TRANSLATE_CHUNK_SIZE", 20000))

# ── 每日定时任务 ──────────────────────────────────────────────────────────
DAILY_FETCH_HOUR = int(os.environ.get("PAPER_DAILY_FETCH_HOUR", 9))  # 北京时间
RECENT_DAYS = int(os.environ.get("PAPER_RECENT_DAYS", 3))  # RSS 保留最近 N 天
