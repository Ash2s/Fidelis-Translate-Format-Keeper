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
│   ├── api/routes.py          # API 路由 & 翻译后台任务 & 质检报告生成
│   ├── services/
│   │   ├── translator.py      # DeepSeek 翻译、润色、标点清理、日期转换
│   │   ├── document_parser.py # .docx 解析、格式读写、字体修复
│   │   └── glossary.py        # 术语表加载/查询
│   ├── models/schemas.py      # Pydantic 数据模型
│   └── config.py              # 环境变量配置
├── tests/                     # pytest 单元测试
├── static/index.html          # 前端界面
├── 脚本/                      # 临时/辅助脚本
├── 测试文件用/                # 测试文档和术语表（不修改）
└── requirements.txt
```

## 启动 & 测试
```bash
pip install -r requirements.txt          # 安装依赖
cp .env.example .env                     # 配置环境（需填入 DEEPSEEK_API_KEY）
python main.py                           # 启动 http://localhost:8002
pytest tests/ -v -q                      # 跑测试（27 用例）
```

## 翻译流程
1. 用户上传术语表（CSV/XLSX）+ .docx 文件
2. 后台逐段翻译：短文本走术语表替换，长文本调 API
3. 后处理：中文标点→英文标点、日期归一化、重复去重、AI 指令清理
4. 保存时强制 Times New Roman 12pt、清除底纹、验证中文残留
5. 有残留中文 → 自动生成质检 HTML 报告到「测试文件用/」

## API 配置
- 服务端默认读 `.env` 的 `DEEPSEEK_API_KEY`
- 用户也可在 Web UI 的「API → 配置」面板填入自己的 Key/Base URL/Model，会覆盖服务端默认值

## 约束
- `.env` 不进 git、不公开
- 生成结果统一到「测试文件用/」，不要污染项目其他目录
- 改代码后必须跑 `pytest tests/ -v -q` 验证
