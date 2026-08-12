"""Dedup guard module: graded duplicate detection with protection whitelists,
Chinese-English fidelity verification, and deletion audit.

Replaces the "blind dedup" regex logic (previously in translator._clean_segment
and document_parser.apply_per_run_formatting) with a conservative pipeline:

    judgment (multi-level) → grading (auto-delete / warn / keep)
    → protection (whitelist) → CN↔EN fidelity check → audit log

Design rules (see 翻译工具去重与保真修复方案.md):
- Scope: per-paragraph / per-cell only. Cross-paragraph dedup is FORBIDDEN.
- Auto-delete requires: adjacent + exact match + length threshold + no
  whitelist hit. Everything else is kept and flagged.
- Deletion is performed on character ranges so the surrounding whitespace
  structure of the original text is preserved (e.g. a trailing space before
  a URL segment is never consumed).
- Any auto-delete is recorded in the audit log.
"""

import re
from collections import Counter

# ---------------------------------------------------------------------------
# Stopwords — single-token duplicates of these are ALWAYS safe to delete
# ("the the" is always a typo). Content words are NOT auto-deleted at k=1.
# Note: raw-token matching means "No." (Number abbreviation) is NOT a
# stopword even though "no" is.
# ---------------------------------------------------------------------------
STOPWORDS = {
    'the', 'a', 'an', 'of', 'and', 'or', 'in', 'on', 'at', 'to', 'for',
    'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'it', 'its', 'this', 'that', 'these', 'those', 'as', 'from', 'than',
    'then', 'so', 'if', 'but', 'not', 'no', 'yes', 'we', 'our', 'you',
    'your', 'he', 'his', 'she', 'her', 'they', 'their', 'i', 'my', 'me',
}

# Punctuation stripped for token normalisation
_PUNCT = '.,;:!?"\'()[]【】《》'

# ---------------------------------------------------------------------------
# Structural protections: numbers / amounts / IDs / version numbers / URLs /
# emails / bracketed markers. Tokens matching these never get deleted at k=1
# unless they are an exact adjacent duplicate of themselves (e.g. "V1.0 V1.0",
# "2025 2025" — always a concatenation error).
# ---------------------------------------------------------------------------
_NUMBER_RE = re.compile(r'^\d[\d,.]*$')
_ID_RE = re.compile(
    r'^[vV]\d+(\.\d+)+$'          # V1.0
    r'|^[A-Z]{1,4}[-.]?\d[\d.-]*$'  # L-04077 / SR0344608
    r'|^[A-Za-z]+\d{4,}$'          # SRO344608
    r'|^\d+[A-Za-z][\w-]*$'        # 2026SR0344608
)
_EMAIL_RE = re.compile(r'^[\w.+-]+@[\w.-]+\.?$')
_URL_TOKEN_RE = re.compile(r'^(?:https?://|www\.)[\w.%-]+\.\w{2,}')
_MARKER_RE = re.compile(r'^\[(?:Image|Seal|Barcode|Logo|Original)[^\]]*\]$')


def _is_stopword(token: str) -> bool:
    """Raw-token stopword check (case-insensitive).

    Punctuation-attached tokens like "No." / "the," are NOT stopwords, so
    number labels ("No. 17558892") count as content when grading duplicates.
    """
    return token.lower() in STOPWORDS


def _norm_token(token: str) -> str:
    """Lowercase + strip punctuation (for rare-word statistics only)."""
    return token.lower().strip(_PUNCT)


def is_protected_token(token: str) -> bool:
    """Structural whitelist: number / ID / version / URL / email / marker.

    Such tokens never participate in fuzzy dedup and are only auto-deleted
    when they form an exact adjacent duplicate (k=1, token == token).
    """
    t = token.strip()
    if not t:
        return True
    return bool(
        _NUMBER_RE.match(t)
        or _ID_RE.match(t)
        or _EMAIL_RE.match(t)
        or _URL_TOKEN_RE.match(t)
        or _MARKER_RE.match(t)
    )


def _content_word_count(seg: list[str]) -> int:
    """Number of non-stopword tokens in a segment."""
    return sum(1 for t in seg if not _is_stopword(t))


# ---------------------------------------------------------------------------
# L1 exact-adjacent duplicate scanner (longest match first)
# ---------------------------------------------------------------------------

def _find_longest_dup(tokens: list[str], i: int, max_k: int = 8) -> int:
    """Return longest k (>=1) such that tokens[i:i+k] == tokens[i+k:i+2k],
    or 0 if none. Longest match avoids nested/partial false positives."""
    n = len(tokens)
    for k in range(min(max_k, (n - i) // 2), 0, -1):
        if tokens[i:i + k] == tokens[i + k:i + 2 * k]:
            return k
    return 0


# ---------------------------------------------------------------------------
# Graded dedup with audit
# ---------------------------------------------------------------------------

def dedup_text(text: str, protected: set | None = None) -> tuple[str, list[dict]]:
    """Grade & dedup adjacent exact duplicates with whitelist protections.

    Returns ``(cleaned_text, audit_entries)``.

    Auto-delete rules (in order):
      1. single stopword token ("the the")                     → delete
      2. segment with >= 2 content words, exact adjacent match  → delete
      3. single structural token (number/ID/version/email/URL
         marker repeated: "V1.0 V1.0", "2025 2025")            → delete
      4. rare/proper-noun protected tokens present             → keep + warn
      5. single non-stopword content token ("Cai Cai")         → keep + warn
    """
    if not text:
        return text, []
    protected = protected or set()
    audit: list[dict] = []

    while True:
        found = False
        # Token stream with character positions, so deletions are performed
        # on char ranges and the original whitespace structure is preserved.
        toks = [(m.group(0), m.start(), m.end())
                for m in re.finditer(r'\S+', text)]
        n = len(toks)
        i = 0
        while i < n:
            texts = [t[0] for t in toks]
            k = _find_longest_dup(texts, i)
            if not k:
                i += 1
                continue

            seg = [t[0] for t in toks[i:i + k]]
            seg_text = ' '.join(seg)
            n_content = _content_word_count(seg)
            single_token = seg[0]
            is_stopword = k == 1 and _is_stopword(single_token)
            is_structural = k == 1 and is_protected_token(single_token)
            rare_hit = any(t in protected for t in seg)

            # Delete char range: from the end of the first copy's last token
            # to the end of the second copy's last token (includes the
            # inter-copy whitespace).
            start_del = toks[i + k - 1][2]
            end_del = toks[i + 2 * k - 1][2]
            removed_text = ' '.join(t for t, _, _ in toks[i + k:i + 2 * k])

            if is_stopword:
                audit.append({
                    'dup': seg_text, 'removed': removed_text,
                    'reason': 'stopword_dup', 'confidence': 'high', 'action': 'removed',
                })
                text = text[:start_del] + text[end_del:]
                found = True
                break

            if n_content >= 2 and not rare_hit:
                audit.append({
                    'dup': seg_text, 'removed': removed_text,
                    'reason': 'exact_adjacent', 'confidence': 'high', 'action': 'removed',
                })
                text = text[:start_del] + text[end_del:]
                found = True
                break

            if is_structural:
                audit.append({
                    'dup': seg_text, 'removed': removed_text,
                    'reason': 'id_number_dup', 'confidence': 'high', 'action': 'removed',
                })
                text = text[:start_del] + text[end_del:]
                found = True
                break

            # Everything else → keep + warn (proper nouns, single content
            # words, rare terms, ambiguous cases). Skip this pair to avoid
            # re-flagging.
            audit.append({
                'dup': seg_text, 'removed': '',
                'reason': 'protected_keep' if rare_hit else 'single_content_token',
                'confidence': 'medium', 'action': 'kept_warn',
            })
            i += k

        if not found:
            break

    return text, audit


# ---------------------------------------------------------------------------
# Protected-token set: rare words (<= 2 occurrences in the whole document)
# ---------------------------------------------------------------------------

def build_protected_tokens(texts: list[str]) -> set[str]:
    """Rare-word whitelist from a document's full text set.

    Tokens appearing <= 2 times (excluding stopwords and structural tokens)
    are treated as proper nouns / rare terms and protected from deletion.
    """
    counter: Counter = Counter()
    for t in texts:
        for tok in re.findall(r'\S+', t):
            if _is_stopword(tok) or is_protected_token(tok):
                continue
            counter[_norm_token(tok)] += 1
    return {tok for tok, cnt in counter.items() if cnt <= 2}


# ---------------------------------------------------------------------------
# CN↔EN fidelity verification (number / amount integrity, term presence)
# ---------------------------------------------------------------------------

_CN_NUM_UNIT_RE = re.compile(
    r'(\d[\d,，.]*)\s*(万亿美元|亿美元|亿元人民币|万元人民币|万亿元|亿元|万元|千元|元人民币|元|亿|万|美元|美金|人民币)?'
)
_UNIT_MULT = {
    '万亿美元': 10 ** 12, '亿美元': 10 ** 8, '亿元人民币': 10 ** 8, '万元人民币': 10 ** 4,
    '万亿元': 10 ** 12, '亿元': 10 ** 8, '亿': 10 ** 8,
    '万元': 10 ** 4, '万': 10 ** 4,
    '千元': 10 ** 3, '千': 10 ** 3,
    '元人民币': 1, '元': 1, '美元': 1, '美金': 1, '人民币': 1,
}
# English amount shorthands honored in verify_fidelity ("12.6 billion" →
# 12,600,000,000), so CN "126亿美元" (rendered as USD 12.6 billion by the
# guard) is never falsely flagged as missing. "hundred million" (10^8) is
# included for numeric equivalence of legacy output; stylistic correctness
# is guaranteed by the placeholder mechanism, not by the verifier.
_EN_UNIT_MULT = {
    'hundred million': 10 ** 8, 'trillion': 10 ** 12, 'billion': 10 ** 9, 'million': 10 ** 6,
}
# Plain integer numbers (IDs / phones / postal codes) — must appear verbatim
_PLAIN_INT_RE = re.compile(r'(?<![\d.,])(\d[\d,]*)(?![\d.])')


def verify_fidelity(cn_text: str, en_text: str) -> list[str]:
    """Compare CN numbers/amounts against EN output. Returns warning strings.

    - Amounts with units: unit-converted to absolute value (万 = ×10⁴, 亿 = ×10⁸)
      then matched against EN numeric values.
    - Plain integers (>= 4 digits): must appear verbatim in EN (digit sequence).
    """
    warnings: list[str] = []
    en_norm = en_text.replace(',', '')

    # 1) amounts / numbers with optional Chinese unit
    for m in _CN_NUM_UNIT_RE.finditer(cn_text):
        raw = m.group(1).replace(',', '').replace('，', '')
        try:
            val = float(raw)
        except ValueError:
            continue
        unit = m.group(2) or ''
        mult = _UNIT_MULT.get(unit, 1)
        target = val * mult
        # Small unit-less numbers (1-2 digits) are usually ordinals/quantities
        # rendered as English words ("第 3 届" → "the third edition"); that is
        # a normal translation, not truncation. Only flag unit-bearing amounts
        # or numbers >= 100 (real amounts/IDs must be digit-preserving).
        if not unit and val < 100:
            continue
        found = False
        # Extract EN numbers robustly. The model may append stray punctuation
        # after digits (e.g. "25,,," or "2024,.." from messy date output);
        # the legacy \d[\d,.]* pattern swallowed it and float() then failed,
        # silently skipping a number that was actually present. Only digits,
        # commas and a single decimal part are consumed; anything else stops.
        # Amount shorthands are also honored: "12.6 billion" → 12,600,000,000,
        # matching CN "126亿美元" (which the guard renders as USD 12.6 billion).
        for m in re.finditer(r'\d[\d,]*(?:\.\d+)?', en_text):
            try:
                num = float(m.group(0).replace(',', ''))
            except ValueError:
                continue
            mult = 1
            tail = en_text[m.end():m.end() + 20]
            for unit, um in _EN_UNIT_MULT.items():
                if re.match(rf'\s*{unit}\b', tail, re.IGNORECASE):
                    mult = um
                    break
            if abs(num * mult - target) < 0.01:
                found = True
                break
        if not found:
            warnings.append(f"金额/数量缺失: 中文「{m.group(0)}」期望值 {target:,.2f} 未在英文中出现")

    # 2) plain integers (cert numbers, phones, postal codes, years)
    for m in _PLAIN_INT_RE.finditer(cn_text):
        num = m.group(1).replace(',', '')
        digits = re.sub(r'\D', '', num)
        if len(digits) >= 4 and digits not in en_norm:
            warnings.append(f"数字缺失: 中文「{m.group(1)}」未在英文中出现")

    return warnings


def verify_terms(cn_text: str, en_text: str, glossary: dict[str, str]) -> list[str]:
    """Check that every glossary term present in CN appears with its standard
    EN translation (or an accepted variant) in the output."""
    warnings: list[str] = []
    if not glossary:
        return warnings
    en_lower = en_text.lower()
    for cn, en in glossary.items():
        if cn and cn in cn_text:
            variants = [v.strip() for v in en.split('|')]
            if not any(v and v.lower() in en_lower for v in variants):
                warnings.append(f"术语缺失: 中文「{cn}」标准译法「{en}」未在英文中出现")
    return warnings


# ---------------------------------------------------------------------------
# Read-only duplicate audit for the QA report (never modifies text)
# ---------------------------------------------------------------------------

def audit_duplicates(text: str) -> list[str]:
    """Scan text and report exact-adjacent duplicates. Read-only.

    Returns warning strings with confidence level so the QA report can tell
    auto-deleted patterns (high confidence) from kept-with-warning ones
    (medium confidence, requires human review).
    """
    warnings: list[str] = []
    tokens = re.findall(r'\S+', text)
    n = len(tokens)
    i = 0
    while i < n:
        k = _find_longest_dup(tokens, i)
        if not k:
            i += 1
            continue
        seg = tokens[i:i + k]
        seg_text = ' '.join(seg)
        n_content = _content_word_count(seg)
        single = seg[0]
        auto_delete = (
            n_content >= 2
            or (k == 1 and (_is_stopword(single) or is_protected_token(single)))
        )
        if auto_delete:
            warnings.append(f"重复检测[可自动删除]: 「{seg_text}」置信度: 高")
        else:
            warnings.append(f"重复检测[需人工确认]: 「{seg_text}」置信度: 中")
        i += 2 * k
    return warnings


# ---------------------------------------------------------------------------
# Glossary term compliance — restricted prefix completion
#
# The glossary is a soft constraint (prompt-injected); models occasionally emit
# a truncated variant of a standard translation (e.g. "Shanghai Tongyue
# Plastics Co." instead of "Shanghai Tongyue Plastics Co., Ltd."). We complete
# such variants ONLY when the text is an exact word-aligned PREFIX of the
# standard translation. No fuzzy matching, no word-order rewriting, no
# semantic changes — anything that cannot be completed safely is left as-is
# (and surfaced by verify_terms for human review).
# ---------------------------------------------------------------------------


def _is_sentence_like(en_value: str) -> bool:
    """True when a glossary EN value looks like a full sentence / news headline
    rather than a name or phrase. Such values must NOT participate in prefix
    completion — completing a truncated prefix would inject the whole sentence
    into the text (observed: 曹闵 → "Cao Min joins forces with Zhongli
    Construction Group Co., Ltd. to write a new chapter in green building…").
    """
    en_value = en_value.strip()
    if not en_value:
        return True
    if len(en_value) > 80:
        return True
    if len(en_value.split()) > 10:
        return True
    # More than one sentence-ish terminator (outside the trailing abbreviation
    # dot of e.g. "Co., Ltd.") ⇒ sentence, not a name.
    body = en_value.rstrip('.')
    if body.count('.') >= 2:
        return True
    if any(ch in body for ch in '!?;'):
        return True
    return False


def _norm_word(word: str) -> str:
    """Normalize a token for comparison: lowercase, strip surrounding
    punctuation, so "Co.," and "Co." compare equal."""
    return word.strip('.,;:!?()[]"\'，。；：！？（）「」『』').lower()


def _match_variant_prefix(toks, pos: int, vwords: list[str]):
    """Word-aligned prefix match of *vwords* starting at token index *pos*.

    Returns ``(matched_count, end_token_index)`` where ``matched_count`` is the
    number of variant words matched, or None if the first word does not match.
    ``matched_count < len(vwords)`` ⇒ the text contains an incomplete prefix of
    the standard translation (candidate for completion).
    """
    if pos >= len(toks) or _norm_word(toks[pos][0]) != _norm_word(vwords[0]):
        return None
    count = 1
    p = pos + 1
    while count < len(vwords) and p < len(toks):
        if _norm_word(toks[p][0]) == _norm_word(vwords[count]):
            count += 1
            p += 1
        else:
            break
    return count, p


def complete_glossary_terms(
    text: str, glossary: dict[str, str]
) -> tuple[str, list[dict]]:
    """Complete truncated standard-translation prefixes in *text*.

    Returns ``(completed_text, audit_entries)``.

    Rules:
    - A truncated variant is completed ONLY when it is an exact word-aligned
      prefix of a glossary EN value (case/punctuation-insensitive on words),
      i.e. the model dropped the tail (", Ltd." etc.).
    - Already-complete translations are untouched; non-prefix matches (word
      order changed, synonyms, grammatical rephrasing) are untouched.
    - URLs, emails and guarded placeholders are never modified.
    - Every completion is recorded in the audit list (origin → standard).
    """
    if not text or not glossary:
        return text, []
    # collect (variant, words) sorted by length desc (longest first)
    variants = []
    for en in glossary.values():
        if not en:
            continue
        for v in [x.strip() for x in str(en).split('|') if x.strip()]:
            # Skip sentence-like values (news headlines etc.): completing a
            # truncated prefix of them would inject the whole sentence.
            if _is_sentence_like(v):
                continue
            variants.append((v, re.findall(r'\S+', v)))
    variants.sort(key=lambda v: -len(' '.join(v[1])))

    audit: list[dict] = []
    result = text

    while True:
        toks = [(m.group(0), m.start(), m.end())
                for m in re.finditer(r'\S+', result)]
        changed = False
        i = 0
        while i < len(toks):
            matched_full = False
            for v, vwords in variants:
                res = _match_variant_prefix(toks, i, vwords)
                if res is None:
                    continue
                m_count, p = res
                if m_count < len(vwords):
                    # Completion requires at least 2 aligned words: a single
                    # word like "China" or "Co." is a common English word, not
                    # evidence of a truncated institution name.
                    if m_count < 2:
                        i += m_count
                        matched_full = True
                        continue
                    start = toks[i][1]
                    end = toks[p - 1][2] if p > i else toks[i][2]
                    span = result[start:end]
                    result = result[:start] + v + result[end:]
                    audit.append({'from': span, 'to': v})
                    changed = True
                    break
                else:
                    # already complete → skip past the matched span
                    i += m_count
                    matched_full = True
                    break
            if changed:
                break  # restart scan
            if not matched_full:
                i += 1
        if not changed:
            break
    return result, audit
