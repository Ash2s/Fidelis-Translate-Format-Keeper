import pytest
import tempfile, os
from app.services.glossary import GlossaryService

def test_load_csv_glossary():
    service = GlossaryService()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
        f.write("\u4e2d\u6587\u672f\u8bed,\u82f1\u6587\u7ffb\u8bd1\n")
        f.write("\u79fb\u6c11\u5c40,Immigration Bureau\n")
        f.write("\u7533\u8bf7\u8868,Application Form\n")
        temp_path = f.name
    glossary_id = service.load_glossary(temp_path, "test.csv")
    os.unlink(temp_path)
    assert glossary_id is not None
    assert service.get_term_count(glossary_id) == 2

def test_load_xlsx_glossary():
    from openpyxl import Workbook
    service = GlossaryService()
    wb = Workbook()
    ws = wb.active
    ws.append(["\u4e2d\u6587\u672f\u8bed", "\u82f1\u6587\u7ffb\u8bd1"])
    ws.append(["\u79fb\u6c11\u5c40", "Immigration Bureau"])
    ws.append(["\u7533\u8bf7\u8868", "Application Form"])
    temp_path = tempfile.mktemp(suffix='.xlsx')
    wb.save(temp_path)
    glossary_id = service.load_glossary(temp_path, "test.xlsx")
    os.unlink(temp_path)
    assert glossary_id is not None
    assert service.get_term_count(glossary_id) == 2

def test_normalize_quotes():
    service = GlossaryService()
    # Straight quotes should be normalized to curly quotes (idempotent)
    text_straight = '"hello"'
    normalized = service.normalize_quotes(text_straight)
    assert normalized == '\u201chello\u201d'
    # Already-curly quotes should remain unchanged (idempotent)
    text_curly = '\u201chello\u201d'
    assert service.normalize_quotes(text_curly) == text_curly

def test_longest_match_replacement():
    service = GlossaryService()
    glossary = {"\u54c1\u5b85\u88c5\u9970\u79d1\u6280": "Pinzhai Decoration Technology", "\u5185\u88c5": "Interior Decoration"}
    text = "\u54c1\u5b85\u88c5\u9970\u79d1\u6280\u7684\u5185\u88c5"
    result = service.replace_with_glossary(text, glossary)
    assert "Pinzhai" in result
    assert "Interior" in result
    assert "\u54c1\u5b85" not in result
