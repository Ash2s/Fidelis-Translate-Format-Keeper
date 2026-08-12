"""Numbering consistency check & normalization for translated documents.

Fixes badcase type 4 (编号体系混乱): within one document, list markers such
as "I. II. III." / "1. 2. 3." / "IV." must not be mixed; repeated sequence
numbers are flagged.

Strategy (conservative):
- Only PURE enumeration markers at line start are normalized (roman ↔ arabic),
  unified to the more frequent style. Sequence values are preserved; they are
  never renumbered (reordering could mask real model errors).
- "Article N" numbered clauses are kept as-is. Mixing Article numbering with
  plain list markers is flagged as a warning (semantics are ambiguous and
  cannot be auto-decided safely).
- Duplicate sequence numbers (same marker appears twice) → warning.
"""

import re

# Roman numerals 1..10 and their values (typical for legal/contract clauses)
_ROMAN = {1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V',
          6: 'VI', 7: 'VII', 8: 'VIII', 9: 'IX', 10: 'X'}
_ROMAN_VALUES = {v: k for k, v in _ROMAN.items()}

# Pure list markers at line start: "I." / "1、" / "3." / "(一)" style.
# Group 1 = numeral token, group 2 = separator, group 3 = trailing spaces
# (trailing spaces are preserved when normalizing).
_LIST_MARK_RE = re.compile(
    r'^(?:\(\s*)?(I{1,3}|IV|V|VI{0,3}|IX|X|[1-9]\d{0,2})([.、．)）])(\s*)'
)
# Article-style clause markers: "Article 3" / "Article III"
_ARTICLE_MARK_RE = re.compile(
    r'^(?:Article|Clause|Section)\s+(I{1,3}|IV|V|VI{0,3}|IX|X|[1-9]\d{0,2})\b',
    re.IGNORECASE,
)


def _roman_to_int(s: str) -> int | None:
    return _ROMAN_VALUES.get(s.upper())


def _int_to_roman(n: int) -> str | None:
    return _ROMAN.get(n)


def _classify(text: str) -> tuple[str | None, int | None]:
    """Return ``(kind, seq)`` for a line-start marker.

    kind in {'roman', 'arabic', 'article', None}.
    """
    m = _ARTICLE_MARK_RE.match(text)
    if m:
        tok = m.group(1)
        if tok.isdigit():
            return 'article', int(tok)
        v = _roman_to_int(tok)
        return ('article', v) if v is not None else (None, None)
    m = _LIST_MARK_RE.match(text)
    if m:
        tok = m.group(1)
        if tok.isdigit():
            return 'arabic', int(tok)
        v = _roman_to_int(tok)
        return ('roman', v) if v is not None else (None, None)
    return None, None


def normalize_numbering(texts: list[str]) -> tuple[list[str], list[str]]:
    """Normalize line-start list numbering across a document.

    Returns ``(new_texts, warnings)``.
    """
    new_texts = list(texts)
    warnings: list[str] = []

    kinds = [_classify(t) for t in texts]
    roman_n = sum(1 for k, _ in kinds if k == 'roman')
    arabic_n = sum(1 for k, _ in kinds if k == 'arabic')
    article_n = sum(1 for k, _ in kinds if k == 'article')

    # ── Mixed list numbering: unify to the more frequent style ──
    target = None
    if roman_n and arabic_n:
        target = 'roman' if roman_n >= arabic_n else 'arabic'
        style = '罗马数字' if target == 'roman' else '阿拉伯数字'
        warnings.append(
            f'编号体系混用: 列表编号含罗马数字 {roman_n} 处与阿拉伯数字 {arabic_n} 处，'
            f'已统一为 {style}'
        )
        for i, (k, seq) in enumerate(kinds):
            if k not in ('roman', 'arabic') or seq is None:
                continue
            if target == 'roman' and k == 'arabic':
                roman = _int_to_roman(seq)
                if roman is None:
                    warnings.append(f'第 {i + 1} 段序号 {seq} 超出罗马数字可表示范围(1-10)，未转换')
                    continue
                new_texts[i] = _LIST_MARK_RE.sub(
                    lambda m: roman + '.' + m.group(3), new_texts[i], count=1)
            elif target == 'arabic' and k == 'roman':
                new_texts[i] = _LIST_MARK_RE.sub(
                    lambda m: str(seq) + '.' + m.group(3), new_texts[i], count=1)

    # ── Article numbering mixed with list numbering → warn (no auto-change) ──
    if article_n and (roman_n or arabic_n):
        warnings.append(
            f'条款编号(Article/Clause/Section) {article_n} 处与列表编号混用，'
            '语义不确定，未自动修改，请人工确认'
        )

    # ── Duplicate sequence numbers ──
    seen: dict[tuple[str, int], int] = {}
    for i, (k, seq) in enumerate(kinds):
        if k is None or seq is None:
            continue
        key = (k, seq)
        if key in seen:
            label = {
                'roman': f'罗马序号 {seq}',
                'arabic': f'阿拉伯序号 {seq}',
                'article': f'条款序号 {seq}',
            }[k]
            warnings.append(f'编号重复: 第 {seen[key] + 1} 段与第 {i + 1} 段均为{label}')
        else:
            seen[key] = i

    return new_texts, warnings
