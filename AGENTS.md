# Fidelis Translate — Format Keeper

中译英文档翻译 Web 工具。上传 .docx + 术语表，通过 DeepSeek API（或 OpenAI 兼容接口）自动翻译并保留原文格式。

## 技术栈
- **后端**: FastAPI + python-docx + OpenAI SDK（兼容 DeepSeek）
- **前端**: 原生 HTML/CSS/JS（单页应用，无框架）
- **Python**: 3.9+ | **端口**: 8002

## 目录结构
```
├── main.py                    # FastAPI 入口
├── app/
│   ├── api/routes.py          # API 路由、翻译后台任务、质检报告生成、批量修复
│   ├── services/
│   │   ├── translator.py      # 翻译/润色、占位符保护、标点清理、格式规范化
│   │   ├── document_parser.py # .docx 解析、格式读写（整段+格式并集写回）
│   │   ├── glossary.py        # 术语表加载/查询（BOM/表头/句子型词条过滤）
│   │   ├── dedup_guard.py     # 去重守卫（分级判定）、保真校验、术语前缀补全
│   │   └── numbering_check.py # 编号归一化（罗马/阿拉伯统一、重复告警）
│   ├── models/schemas.py      # Pydantic 数据模型
│   └── config.py              # 环境变量配置
├── tests/                     # pytest 单元测试 + test_dedup_regression.py（106 断言）
├── static/index.html          # 前端界面（含翻译报告弹窗）
├── 测试文件用/                # per-file 质检报告输出（git 忽略）
└── requirements.txt
```

## 启动 & 测试
```bash
pip install -r requirements.txt          # 安装依赖
cp .env.example .env                     # 配置环境（需填入 DEEPSEEK_API_KEY）
python main.py                           # 启动 http://localhost:8002
pytest tests/test_translator.py tests/test_document_parser.py -q   # 单元测试（23 用例）
python tests/test_dedup_regression.py    # 回归测试（106 断言，独立脚本）
```

## 翻译流程（现状）
1. 用户上传术语表（CSV/XLSX，可选）+ .docx 文件（可批量）
2. 整段翻译（段落级 3 线程 + 文件级 2 路并发，全局 API 并发 6 路上限）；>400 字符按句切分
3. 保护机制：数字/金额/URL/邮箱占位符保护、术语表注入、日期预转换
4. 后处理：机械清理（标点/重复/垃圾符号）→ 格式规范化（日期语序/RMB 冗余）→ polish 润色（>120 字符）
5. 质检：中文残留扫描、CN↔EN 保真校验（数字/术语）、重复审计、编号归一化；确定性缺陷自动修复
6. 遗留问题 → 可视化翻译报告（job 级弹窗 + per-file HTML），可勾选批量修复、导出 PDF/HTML

## 关键约定
- **主目录 `app/` 是代码本体**（唯一开发源）；`斐迪译Fidelis-Translate/`（Windows/macOS 打包结构）是产物，不同步进 git
- 改动流程：先改主本体 → 验证 → 再同步打包工作区，md5 三处核验
- 大额金额规范：中文单位含"亿/万亿" → billion/trillion 缩写（126亿美元→USD 12.6 billion），万/元级保留完整数字
- 术语表标准译法超过 80 字符或句子型 → 自动忽略（防媒体报道标题当译名）
- 改代码后必须跑 pytest + 回归脚本验证

## 约束
- `.env` 不进 git、不公开
- 打包 zip 重打不是固定流程，按需处理
