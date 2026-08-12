# -*- coding: utf-8 -*-
"""badcase regression tests for the dedup & fidelity fix (方案第四章).

Run:
    python tests/test_dedup_regression.py

Groups:
    A: true duplicates (badcase type 2) — MUST be removed
    B: false positives (proper nouns / supplementary info / cross-run) — MUST be kept
    C: number / term fidelity (badcase type 1/6/8) — MUST be preserved
    D: read-only duplicate audit
"""
import os
import sys

# This file is a standalone regression script (run: python tests/test_dedup_regression.py).
# It is not a pytest test module — skip it when running `pytest` over tests/.
import pytest
pytestmark = pytest.mark.skip(reason="standalone regression script; run via `python tests/test_dedup_regression.py`")

# ── Auto-detect the source root ──
# The same test file lives in both the master repo (tests/) and the packaged
# workspace (斐迪译Fidelis-Translate/tests/). Try each candidate in order.
_here = os.path.dirname(os.path.abspath(__file__))
_base = os.path.dirname(_here)
_CANDIDATES = [
    _base,                                                          # master repo root
    os.path.join(_base, "Windows版", "source"),                    # packaged workspace (win)
    os.path.join(_base, "macOS版", "source"),                       # packaged workspace (mac)
    os.path.join(_base, "斐迪译Fidelis-Translate", "Windows版", "source"),
    os.path.join(_base, "斐迪译Fidelis-Translate", "macOS版", "source"),
]
for _p in _CANDIDATES:
    if os.path.isfile(os.path.join(_p, "app", "services", "translator.py")):
        sys.path.insert(0, _p)
        break

from app.services import dedup_guard
from app.services.translator import TranslatorService

clean = TranslatorService._clean_mechanical_errors

PASS, FAIL = 0, 0


def check(name: str, got, expected):
    global PASS, FAIL
    ok = got == expected
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n    expected: {expected!r}\n    got:      {got!r}")


def check_warn(name: str, warnings, expect_nonempty: bool):
    global PASS, FAIL
    ok = bool(warnings) == expect_nonempty
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n    expect_nonempty={expect_nonempty}, got={warnings}")


# ---------------------------------------------------------------------------
# A 组：必须删除（真重复，badcase 类型 2）
# ---------------------------------------------------------------------------
print("== A 组：真重复必须删除 ==")
A_CASES = [
    ("Copyright owner: Cai Yuan Cai Yuan",
     "Copyright owner: Cai Yuan"),
    ("Method of Right Acquisition: Original Acquisition Original Acquisition",
     "Method of Right Acquisition: Original Acquisition"),
    ("Software Copyright Registration No. 17558892 Software Copyright Registration No. 17558892",
     "Software Copyright Registration No. 17558892"),
    ("Chair of the Review Committee: Cai Yuan Cai Yuan",
     "Chair of the Review Committee: Cai Yuan"),
    ("Email: 227766088@qq. com 227766088@qq. com",
     "Email: 227766088@qq. com"),
    ("Client: Cai Yuan Cai Yuan",
     "Client: Cai Yuan"),
    ("Plastic Products V1.0 V1.0",
     "Plastic Products V1.0"),
    ("No. 17558892 No. 17558892",
     "No. 17558892"),
    # 模型级修复观察项：去重器不应改动非精确重复（由 polish 模型处理）
    ("The report is objective, complete, and complete and true",
     "The report is objective, complete, and complete and true"),
]
for inp, exp in A_CASES:
    check(f"A: {inp[:48]}", clean(inp), exp)

# ---------------------------------------------------------------------------
# B 组：必须保留（伪重复 / 误删保护）
# ---------------------------------------------------------------------------
print("== B 组：伪重复必须保留 ==")
B_CASES = [
    # 两个 No. 非紧邻，分属不同编号 → 保留
    ("No. 2026 No. L-04077", "No. 2026 No. L-04077"),
    # 补充说明 / 语义近似（无紧邻精确重复）→ 保留
    ("He is the founder of the company, and he founded the company in 2010.",
     "He is the founder of the company, and he founded the company in 2010."),
    ("The company has offices in Beijing, and it also has offices in Shanghai.",
     "The company has offices in Beijing, and it also has offices in Shanghai."),
    # 单 token 实词重复（可能为专名）→ 保留 + 告警
    ("Cai Cai", "Cai Cai"),
    # 大小写不同的词 → L1 大小写敏感，不判重
    ("Software software", "Software software"),
    # 非紧邻重复（金额双写格式）→ 保留；RMB+Yuan 冗余由类型6规范化消除
    ("RMB 400,000 Yuan (¥400,000)", "RMB 400,000 (¥400,000)"),
    # 术语 / 专名在同段不同位置重复 → 保留
    ("Cai Yuan graduated from university. Cai Yuan then founded the company.",
     "Cai Yuan graduated from university. Cai Yuan then founded the company."),
    # URL 分段保护：段尾空格不得被去重器吞掉（回归保护）
    ("The link is https://www.example.com/page",
     "The link is https://www.example.com/page"),
    # 单 token 内容词重复（think）→ 保守保留（可能专名，由告警提示）
    ("I I think think this works", "I think think this works"),
]
for inp, exp in B_CASES:
    check(f"B: {inp[:48]}", clean(inp), exp)

# 注：B2 组（run 边界去重）已随"整段翻译 + 格式并集"改造移除——
# 不再有 run 级去重，run 边界重复从源头消失；
# 格式并集写回由 tests/test_document_parser.py 的
# test_apply_per_run_formatting_whole_paragraph_union 覆盖。

# ---------------------------------------------------------------------------
# C 组：数字 / 术语保真（badcase 类型 1/6/8）
# ---------------------------------------------------------------------------
print("== C 组：数字 / 术语保真 ==")
C_OK = [
    ("注册资本 510 万元", "Registered capital of RMB 5,100,000"),
    ("总价值一千零二十五万元人民币（1025.00万元）", "Total value of RMB 10,250,000.00"),
    ("评估值 256.00万元", "Appraised value of RMB 2,560,000.00"),
    ("设备耗电量 12,000 千瓦时", "Power consumption of 12,000 kWh"),
    ("证书号 软著登字第 17558889 号", "Software Copyright Registration No. 17558889"),
    ("邮编 325000", "Postal code 325000"),
    ("电话 13806531333", "Tel: 13806531333"),
    ("注册用户月增 20,000", "Monthly increase of 20,000 registered users"),
]
for cn, en in C_OK:
    check_warn(f"C: 保真 {cn[:20]}", dedup_guard.verify_fidelity(cn, en), False)

# 负例：数字截断必须被检测出（badcase 类型 1 的拦截能力）
C_BAD = [
    ("注册资本 510 万元", "Registered capital of RMB 5,100,0"),
    ("年薪 500,000 元人民币", "Annual salary of RMB 500,0 yuan"),
    ("邮编 325000", "Postal code 3250"),
    ("证书号 软著登字第 17558889 号", "Registration No. 175589"),
]
for cn, en in C_BAD:
    check_warn(f"C: 截断检测 {cn[:20]}", dedup_guard.verify_fidelity(cn, en), True)

# 术语校验
glossary = {"中国知网": "China National Knowledge Infrastructure (CNKI)",
            "蔡远": "Cai Yuan"}
check_warn("C: 术语命中", dedup_guard.verify_terms("中国知网", "China National Knowledge Infrastructure (CNKI)", glossary), False)
check_warn("C: 术语缺失检测", dedup_guard.verify_terms("中国知网", "CNKI", glossary), True)
check_warn("C: 人名术语", dedup_guard.verify_terms("蔡远", "Cai Yuan", glossary), False)

# ---------------------------------------------------------------------------
# D 组：只读重复审计
# ---------------------------------------------------------------------------
print("== D 组：只读重复审计 ==")
check_warn("D: 紧邻重复可检出", dedup_guard.audit_duplicates("Cai Yuan Cai Yuan"), True)
check_warn("D: 单 token 实词告警(中置信度)", dedup_guard.audit_duplicates("Cai Cai"), True)
check_warn("D: 正常文本无告警", dedup_guard.audit_duplicates("This is a normal sentence."), False)

# ---------------------------------------------------------------------------
# E 组：类型 1+7 占位符保护（数字/金额/URL/邮箱不被翻译模型改动）
# ---------------------------------------------------------------------------
print("== E 组：类型 1+7 占位符保护 ==")
P = TranslatorService._protect_guardables
R = TranslatorService._restore_guardables


def check_guard(name: str, cn: str, expected_restored: str):
    prot, gmap = P(cn)
    restored = R(prot, gmap)
    check(name, restored, expected_restored)


E_CASES = [
    # 金额含中文单位 → 换算为英文金额（万=×10⁴, 亿=×10⁸）
    ("注册资本 510 万元", "注册资本 RMB 5,100,000.00"),
    ("总价值一千零二十五万元人民币（1025.00万元）",
     "总价值一千零二十五万元人民币（RMB 10,250,000.00）"),
    ("评估值 256.00万元 / 240.00万元", "评估值 RMB 2,560,000.00 / RMB 2,400,000.00"),
    ("年薪 500,000 元人民币", "年薪 RMB 500,000.00"),
    # 纯数字（电话/邮编/证书号/年份）原样保留
    ("电话 13806531333", "电话 13806531333"),
    ("邮编 325000", "邮编 325000"),
    ("证书号 软著登字第 17558889 号", "证书号 软著登字第 17558889 号"),
    # 数字+非金额单位 → 只保护数字，单位留给模型
    ("设备耗电量 12,000 千瓦时", "设备耗电量 12,000 千瓦时"),
    # URL / 邮箱原样保护
    ("网址：https://www.cstc.org.cn/，邮箱 service@cstc.org.cn",
     "网址：https://www.cstc.org.cn/，邮箱 service@cstc.org.cn"),
    # 已还原金额 + 数字不得被二次截断（保护-还原幂等）
    ("金额 RMB 10,250,000.00", "金额 RMB 10,250,000.00"),
]
for name, cn, exp in [(f"E: {c[:22]}", c, e) for c, e in E_CASES]:
    check_guard(name, cn, exp)

# 占位符丢失兜底：模型删掉占位符时不得泄漏 __G<n>__ 文本
prot, gmap = P("金额 510 万元")
dropped = R(prot.replace("__G0__", ""), gmap)
check("E: 占位符丢失兜底", dropped, "金额 ")

# ---------------------------------------------------------------------------
# F 组：类型 4 编号一致性
# ---------------------------------------------------------------------------
print("== F 组：类型 4 编号一致性 ==")
from app.services import numbering_check as nc
F_OK = [
    (["I. A", "2. B", "III. C"], ["I. A", "II. B", "III. C"]),   # 混用统一为罗马
    (["1. A", "II. B", "3. C"], ["1. A", "2. B", "3. C"]),       # 混用统一为阿拉伯
    (["I. A", "Article 2", "IV. B"], ["I. A", "Article 2", "IV. B"]),  # Article 混用不自动改
]
for inp, exp in F_OK:
    out, _ = nc.normalize_numbering(inp)
    check(f"F: 归一化 {inp}", out, exp)
check_warn("F: 序号重复告警", nc.normalize_numbering(["I. A", "I. B"])[1], True)
check_warn("F: Article 混用告警", nc.normalize_numbering(["I. A", "Article 2"])[1], True)
check_warn("F: 无混用无告警", nc.normalize_numbering(["I. A", "II. B", "III. C"])[1], False)

# ---------------------------------------------------------------------------
# H 组：类型 8 术语表头/BOM 过滤
# ---------------------------------------------------------------------------
print("== H 组：类型 8 术语表头/BOM 过滤 ==")
import tempfile as _tf
import csv as _csv
from app.services.glossary import GlossaryService as _GS
_tmp = _tf.mktemp(suffix=".csv")
with open(_tmp, "w", encoding="utf-8-sig", newline="") as _f:
    _w = _csv.writer(_f)
    _w.writerow(["zh-CN", "en-US"])
    _w.writerow(["蔡远", "Cai Yuan"])
_gs = _GS.__new__(_GS)  # 仅测解析逻辑，不初始化存储
_terms = _gs._load_csv(_tmp)
import os as _os
_os.remove(_tmp)
check("H: 表头/去BOM后词数", len(_terms), 1)
check("H: 真实词条保留", _terms.get("蔡远"), "Cai Yuan")
check("H: 无表头污染", "zh-CN" in _terms or "en-US" in _terms or "\ufeffzh-CN" in _terms, False)

# ---------------------------------------------------------------------------
# I 组：类型 8 术语表遵守度——受限前缀补全（词对齐，仅补尾部）
# ---------------------------------------------------------------------------
print("== I 组：类型 8 术语前缀补全 ==")
_I_GLOSSARY = {
    "上海通悦塑业有限公司": "Shanghai Tongyue Plastics Co., Ltd.",
    "中国软件评测中心": "China Software Testing Center (CSTC)",
    "蔡远": "Cai Yuan",
}
I_CASES = [
    # 尾部截断 → 补全
    ("Party A: Shanghai Tongyue Plastics Co. (Seal)",
     "Party A: Shanghai Tongyue Plastics Co., Ltd. (Seal)"),
    # 已完整 → 不动
    ("Shanghai Tongyue Plastics Co., Ltd. (Seal)",
     "Shanghai Tongyue Plastics Co., Ltd. (Seal)"),
    # 缩写后续出现（非前缀）→ 不动
    ("the CSTC report", "the CSTC report"),
    # 语法重组 / 同义表达 → 不动
    ("software testing agency of China", "software testing agency of China"),
    # 常见单词（China 单词语义）→ 不误伤
    ("China is a large country.", "China is a large country."),
    # 头部缺失 → 不安全，不动（可能是有意简称）
    ("the Plastic Formula Design and Performance Evaluation System V1.0",
     "the Plastic Formula Design and Performance Evaluation System V1.0"),
    # 人名完整出现多次 → 不动
    ("Cai Yuan developed the system. Cai Yuan is the CEO.",
     "Cai Yuan developed the system. Cai Yuan is the CEO."),
    # URL 不参与补全
    ("Visit https://www.example.com/page for details",
     "Visit https://www.example.com/page for details"),
]
for inp, exp in I_CASES:
    out, _ = dedup_guard.complete_glossary_terms(inp, _I_GLOSSARY)
    check(f"I: {inp[:44]}", out, exp)
# 补全审计记录
_, i_audit = dedup_guard.complete_glossary_terms(
    "Party A: Shanghai Tongyue Plastics Co.", _I_GLOSSARY)
check("I: 补全审计记录", len(i_audit), 1)
check("I: 审计内容", i_audit[0]["to"] if i_audit else "", "Shanghai Tongyue Plastics Co., Ltd.")

# ---------------------------------------------------------------------------
# J 组：类型 5/6 格式规范化（日期语序 / 序数 / 欧式日期 / RMB+Yuan 冗余）
# ---------------------------------------------------------------------------
print("== J 组：类型 5/6 格式规范化 ==")
_J_CASES = [
    ("January 1987 13th", "January 13, 1987"),
    ("October 2025 22", "October 22, 2025"),
    ("January 13th, 1987", "January 13, 1987"),
    ("13 January 1987", "January 13, 1987"),
    ("Issued on 22 October 2025", "Issued on October 22, 2025"),
    ("RMB 400,000 Yuan", "RMB 400,000"),
    ("Total amount: RMB 1,200,000 Yuan (¥1,200,000)", "Total amount: RMB 1,200,000 (¥1,200,000)"),
    # 正常文本不受影响
    ("The report is complete and true.", "The report is complete and true."),
    ("October 13, 2025 2025.10.13", "October 13, 2025"),
]
for inp, exp in _J_CASES:
    out = clean(inp)
    check(f"J: {inp[:40]}", out, exp)

# ---------------------------------------------------------------------------
# K 组：verify_fidelity 对模型垃圾标点的解析健壮性
# 模型常在日期后追加多余标点（"25,,,"、"2024,.."），旧正则 \d[\d,.]* 吞掉
# 标点后 float() 解析失败，把实际存在的数字误判为缺失。
# ---------------------------------------------------------------------------
print("== K 组：verify_fidelity 标点健壮性 ==")
_K_CASES = [
    # 垃圾标点不误报（数字实际存在）→ 0 条告警
    ("日期 2024 年 11 月 25 日", "the date November 25,,, 2024,..", 0),
    ("日期 2024 年 11 月 25 日", "a date of November 25,, 2024,.. The company", 0),
    # 金额正常匹配 → 0 条
    ("金额 500 万元人民币", "an amount of RMB 5,000,000.00", 0),
    # 真缺失仍检出（模型真丢了年份 → 金额缺失 + 数字缺失 2 条）
    ("日期 2024 年 11 月 25 日", "the date November 25,..", 2),
    # 真缺失仍检出（金额少一位 → 1 条）
    ("金额 500 万元人民币", "an amount of RMB 5,000.00", 1),
]
for cn, en, expect_n in _K_CASES:
    w = dedup_guard.verify_fidelity(cn, en)
    check(f"K: verify_fidelity({cn[:18]}) 告警数={len(w)}", len(w), expect_n)

# ---------------------------------------------------------------------------
# L 组：② 垃圾标点清理（模型在日期/金额后追加的 ,,, / ,.. 压缩）
# ---------------------------------------------------------------------------
print("== L 组：垃圾标点清理 ==")
_L_CASES = [
    ("the date November 25,,, 2024,.. The company",
     "the date November 25, 2024. The company"),
    ("November 25,..", "November 25."),
    ("involving an amount of RMB 5,000,000.00 and the date November 25,, 2024,..",
     "involving an amount of RMB 5,000,000.00 and the date November 25, 2024."),
    # 不应误伤：小数 / 千分位 / 省略号 / 编号
    ("Value is 5.5 and total 1,000.00", "Value is 5.5 and total 1,000.00"),
    ("See clause 1.2.3 for details", "See clause 1.2.3 for details"),
    ("Wait... let me think.", "Wait... let me think."),
]
for inp, exp in _L_CASES:
    out = clean(inp)
    check(f"L: {inp[:42]}", out, exp)

# ---------------------------------------------------------------------------
# M 组：URL 保护边界——中文句号不应被 URL 正则吞入（U+3002 属 CJK 标点区）
# ---------------------------------------------------------------------------
print("== M 组：URL 尾部中文句号清理 ==")
_M_CASES = [
    # 邮箱后中文句号 → 转英文句点，URL 本体不动
    ("联系方式：电话 13806531333，邮箱 service@cstc.org.cn。",
     "联系方式:电话 13806531333,邮箱 service@cstc.org.cn."),
    # URL 后中文句号 → 转英文句点
    ("链接：https://www.example.com/a/b?q=1&r=2。",
     "链接:https://www.example.com/a/b?q=1&r=2."),
    # URL 本体保护不破坏
    ("The link is https://www.example.com/page",
     "The link is https://www.example.com/page"),
    ("Website: https://www.cstc.org.cn/", "Website: https://www.cstc.org.cn/"),
    ("Email: service@cstc.org.cn", "Email: service@cstc.org.cn"),
    # 无 URL 的中文句号仍正常转换（回归）
    ("联系方式：电话 13806531333。", "联系方式:电话 13806531333."),
]
for inp, exp in _M_CASES:
    out = clean(inp)
    check(f"M: {inp[:40]}", out, exp)

# ---------------------------------------------------------------------------
# N 组：大额金额单位规范表达（badcase：126亿美元 → USD 12.6 billion，禁止
# "126 hundred million" 非规范表达；亿元人民币不得残留中文单位）
# ---------------------------------------------------------------------------
print("== N 组：大额金额单位规范化 ==")
_N_GUARD_CASES = [
    # (中文原文, 占位符保护→还原后的期望目标)
    ("智能家电市场达126亿美元", "智能家电市场达USD 12.6 billion"),
    ("贸易额达 3.5 万亿美元", "贸易额达 USD 3.5 trillion"),
    ("投资总额 500 亿元人民币", "投资总额 RMB 50 billion"),
    ("投资总额 1 亿元人民币", "投资总额 RMB 100 million"),
    ("注册资本 510 万元人民币", "注册资本 RMB 5,100,000.00"),
    ("营收 126亿美元，同比增长 8.5%", "营收 USD 12.6 billion，同比增长 8.5%"),
    ("金额 800 万", "金额 RMB 8,000,000.00"),
]
for inp, exp in _N_GUARD_CASES:
    prot, gmap = TranslatorService._protect_guardables(inp)
    restored = TranslatorService._restore_guardables(prot, gmap)
    check(f"N: guard({inp[:22]})", restored, exp)

# verify_fidelity 对 billion/trillion 缩写与 legacy "hundred million" 的数值识别
_N_FID_CASES = [
    ("智能家电市场达126亿美元", "reached USD 12.6 billion", 0),
    ("智能家电市场达126亿美元", "reached USD 12,600,000,000", 0),
    ("智能家电市场达126亿美元", "reached 126 hundred million USD", 0),  # 数值等价不误报
    ("贸易额达 3.5 万亿美元", "trade volume reached USD 3.5 trillion", 0),
    ("投资总额 500 亿元人民币", "total investment RMB 50,000,000,000.00", 0),
    # 真缺失仍检出
    ("智能家电市场达126亿美元", "The market reached USD 12.6", 1),
    ("注册资本 510 万元人民币", "registered capital RMB 5,100.00", 1),
]
for cn, en, exp in _N_FID_CASES:
    n = len(dedup_guard.verify_fidelity(cn, en))
    check(f"N: verify({cn[:18]}) 告警={n}", n, exp)

# ---------------------------------------------------------------------------
print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
