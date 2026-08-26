# 📄 Paper-Tracker — 凝聚态物理论文自动追踪与翻译平台

自动从主流物理学期刊 RSS 源抓取最新论文，利用大语言模型（LLM）进行智能筛选、摘要翻译与分类，并提供美观的 Web 界面进行浏览、标注和 PDF 全文翻译。

## ✨ 功能特性

- **多源 RSS 聚合** — 自动抓取 arXiv、Nature 系列、Physical Review 系列、Science 系列、Advanced Materials 系列、Nano Letters、ACS Nano 等顶刊最新论文
- **AI 智能筛选** — 利用 LLM（OpenAI 兼容 API）判断论文是否与「电子输运」研究相关，仅推送相关论文
- **自动翻译** — 对筛选出的论文自动翻译标题、摘要，并生成中文总结
- **PDF 全文翻译** — arXiv 论文自动下载 LaTeX 源码翻译并重编译 PDF；非 arXiv 论文通过 doc2x 转换后翻译
- **标签分类** — 支持对论文进行「相关 / 感兴趣 / 组会报告 / 不相关」多标签标注
- **全景日历** — 按日期浏览论文，日历视图标记已读/未读状态
- **全局搜索** — 支持按标题、摘要、期刊等关键词全文搜索
- **LaTeX 公式渲染** — 标题和摘要中的数学公式自动渲染（KaTeX）
- **暗色模式** — 自动跟随系统主题切换
- **每日定时任务** — 每天北京时间 9:00 自动执行抓取与筛选流程

## 📸 界面预览

> 启动后访问 `http://localhost:3475` 即可看到论文浏览界面。

## 🏗️ 项目结构

```
paper-web/
├── server.py           # Flask 主服务（API + 定时任务）
├── scraper.py          # RSS 多源论文抓取器
├── pdf_translate.py    # LaTeX 翻译与 PDF 编译管线
├── config.py           # 全局配置（环境变量驱动，已 gitignore）
├── import.py           # 历史数据导入工具
├── index.html          # 前端主页面
├── data/               # SQLite 数据库与上传的 PDF 文件
├── downloads/          # LaTeX 源码与翻译过程的临时文件
├── static/
│   ├── css/style.css   # 样式（含暗色模式）
│   └── js/app.js       # 前端交互逻辑
├── requirements.txt    # Python 依赖
└── .gitignore
```

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/your-username/paper-tracker.git
cd paper-tracker
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> **可选依赖**：PDF 全文翻译需要 [XeLaTeX](https://www.latex-project.org/get/)（TeX Live 或 MiKTeX）环境。

### 3. 创建配置文件

`config.py` 包含 API Key 等敏感信息，已被 `.gitignore` 排除，不会提交到仓库。首次使用需要自行创建：

```bash
cp config.example.py config.py
# 编辑 config.py，填入你的 API Key 等配置
```

主要配置项说明：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `SERVER_PORT` | Web 服务端口 | `3475` |
| `FILTER_BASE_URL` | 筛选用的 LLM API 地址（OpenAI 兼容） | — |
| `FILTER_API_KEY` | 筛选用的 LLM API 密钥 | — |
| `FILTER_MODEL` | 筛选用的 模型名称 | — |
| `TRANSLATE_BASE_URL` | 翻译用的 LLM API 地址（OpenAI 兼容） | — |
| `TRANSLATE_API_KEY` | 翻译用的 LLM API 密钥 | — |
| `TRANSLATE_MODEL` | 翻译用的模型名称 | — |
| `AI_MAX_CONCURRENCY` | AI 筛选并发批次数（仅允许 2–4） | `2` |
| `AI_MAX_ATTEMPTS` | 每个筛选批次的最大尝试次数 | `5` |
| `AI_RETRY_BASE_DELAY` | 筛选重试指数退避基数（秒） | `10.0` |
| `DOC2X_API_KEY` | doc2x API 密钥（PDF→LaTeX） | — |
| `DAILY_FETCH_HOUR` | 每日抓取时间（北京时间） | `9` |
| `RECENT_DAYS` | RSS 保留天数 | `3` |

所有配置项同时支持环境变量覆盖（加 `PAPER_` 前缀），详见 `config.example.py`。

### 4. 启动服务

```bash
python server.py
```

访问 `http://localhost:3475` 开始使用。

## 📦 数据导入

如果有历史数据库需要导入：

```bash
python import.py /path/to/old/pushed_papers.db
```

## 🔧 工作原理

```
RSS 源 ──抓取──→ 全量论文列表
                    │
              过滤已评估 URL
                    │
            ┌───────┴───────┐
            ↓               ↓
      LLM 批量评估     跳过（已入库）
     (输运相关性判定)
            │
     ┌──────┴──────┐
     ↓             ↓
  相关论文      不相关论文
  (推送到前端)   (仅记录评估结果)
```

### 论文翻译流程

```
arXiv 论文:  LaTeX 源码 → 翻译 → XeLaTeX 编译 → 中文 PDF
非 arXiv:    PDF → doc2x → LaTeX → 翻译 → XeLaTeX 编译 → 中文 PDF
```

## ⚠️ 注意事项

- **API 报错重试**：如果 LLM API 在筛选过程中报错，失败的论文不会写入数据库，下次抓取时会自动重试（前提是论文仍在 RSS 源中，通常保留 3 天）
- **LaTeX 环境**：PDF 全文翻译功能需要安装 XeLaTeX（TeX Live / MiKTeX），未安装时翻译功能不可用

## 📝 License

MIT
