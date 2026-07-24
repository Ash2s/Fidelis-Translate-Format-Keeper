# Fidelis Translate — Format Keeper

中译英文档翻译工具。上传 .docx 文件 + 术语表，通过 DeepSeek API（或其他 OpenAI 兼容接口）自动翻译并保留原文格式。

## 功能

- **文档翻译** — 上传 .docx 文件，自动识别正文/表格/文本框并翻译
- **术语表** — 上传 CSV/XLSX 术语表，保证专业词汇翻译一致性
- **润色审核** — 翻译后自动进行英文校对润色，修正语法错误、句式杂糅、用词搭配和翻译腔
- **日期处理** — 自动识别中文日期（年月日）并转换为标准英文格式
- **机械错误清理** — 检测并修复拼写重复、单词粘连、标点缺空格等机械性错误
- **跨 Run 去重** — 处理 docx 格式标记导致的词语拆分和边界重复
- **自定义 API** — 支持使用自己的 API Key / Base URL / 模型，不限 DeepSeek
- **格式保留** — 翻译后保留字体、颜色、行距、加粗/斜体等格式
- **内容修正** — 翻译完成后可提交反馈重新翻译
- **实时进度** — 可视化进度环 + 逐文件状态跟踪
- **批量下载** — 多文件翻译结果一键打包 ZIP 下载
- **预览** — 在线预览翻译结果，下载 .docx 文件

## 技术栈

- **后端**: FastAPI + python-docx + DeepSeek API (OpenAI 兼容接口)
- **前端**: 原生 HTML/CSS/JS（无框架依赖）
- **依赖**: 见 requirements.txt

## 快速开始

### 1. 环境要求

- Python 3.9+
- pip

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

项目使用 `.env` 文件管理配置。复制模板文件并根据自己的情况修改：

```bash
cp .env.example .env
```

然后编辑 `.env` 文件，填入必要的信息：

```env
# ===== 必填 =====
# DeepSeek API Key（在 https://platform.deepseek.com 申请）
DEEPSEEK_API_KEY=sk-your-api-key-here

# ===== 可选 =====
# 使用的模型名称（默认 deepseek-v4-flash）
DEEPSEEK_MODEL=deepseek-v4-flash

# 数据存储目录（默认为项目根目录下的 data/ 文件夹）
# GLOSSARY_DIR=./data/glossaries
# UPLOAD_DIR=./data/uploads
# JOBS_DIR=./data/jobs
```

#### 如何获取 DeepSeek API Key？

1. 访问 [DeepSeek 开放平台](https://platform.deepseek.com)
2. 注册/登录账号
3. 进入「API Keys」页面，点击「创建 API Key」
4. 复制生成的 Key，粘贴到 `.env` 文件中的 `DEEPSEEK_API_KEY=`
5. DeepSeek API 按量计费，新用户通常有免费额度

### 4. 启动服务

```bash
python main.py
```

### 5. 访问工具

打开浏览器访问 **http://localhost:8002**

---

### 使用其他 OpenAI 兼容的 API

本工具不仅支持 DeepSeek，也支持任何 OpenAI 兼容接口（如 OpenAI 官方、Azure OpenAI、本地 Ollama 等）。

**无需修改 `.env` 文件**，直接在网页界面上操作：

1. 展开页面上的「**自定义 API**」面板
2. 填入你的 API Key、Base URL 和模型名称
3. 开始翻译即可

> 界面配置仅在当前浏览器会话生效，不会影响服务器上的 `.env` 配置。

## 质量保障机制

翻译流程内置多层质量检查：

| 阶段 | 机制 | 说明 |
|---|---|---|
| 预处理 | 日期转换 | 中文日期 → 英文格式，避免逐字直译 |
| 翻译 | 术语表 + 系统提示词 | 专业术语一致性 + 日期/编号/格式规则 |
| 后处理 | 润色审核 | API 驱动的英文校对：语法、用词、句式、翻译腔 |
| 清理 | 机械错误检测 | 正则清理：重复字母、重复词、单词粘连、标点间距 |
| 格式 | 跨 Run 归一化 | 间距补全 + 边界词语去重，消除格式拆分痕迹 |

## 测试

```bash
pytest tests/ -v
```

## 项目结构

```
├── app/
│   ├── api/routes.py          # API 路由 & 后台翻译任务
│   ├── services/
│   │   ├── document_parser.py # .docx 解析与格式操作
│   │   ├── glossary.py        # 术语表管理
│   │   └── translator.py      # DeepSeek 翻译 & 润色服务
│   ├── models/schemas.py      # Pydantic 数据模型
│   └── config.py              # 配置
├── tests/                     # 单元测试
├── static/index.html          # 前端界面（单页应用）
├── main.py                    # 入口
└── requirements.txt
```
