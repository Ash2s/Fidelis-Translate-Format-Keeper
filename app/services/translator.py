"""Translator service for calling DeepSeek API to translate Chinese to English."""

import re
import time
import threading
import httpx
from datetime import datetime
from openai import OpenAI
from app.config import settings
from app.services import dedup_guard

# Global API concurrency cap + retry. A single semaphore guards EVERY
# chat.completions.create call (translate / polish / interpret), across all
# jobs and threads, so running several batch jobs in parallel cannot exceed
# the provider rate limit.
_API_SEMAPHORE = threading.BoundedSemaphore(6)
_API_MAX_RETRIES = 2  # extra attempts after the first failure


def _api_completion(client, model: str, messages: list, temperature: float) -> object:
    """chat.completions.create with a global concurrency cap and retries."""
    last_err: Exception | None = None
    for attempt in range(_API_MAX_RETRIES + 1):
        try:
            with _API_SEMAPHORE:
                return client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature,
                )
        except Exception as e:
            last_err = e
            if attempt < _API_MAX_RETRIES:
                time.sleep(0.6 * (2 ** attempt))  # 0.6s, 1.2s backoff
    raise last_err

# ---------------------------------------------------------------------------
# Common proper nouns used as fallback when user glossary doesn't cover them
# ---------------------------------------------------------------------------
COMMON_TERMS = {
    # General-purpose fallback terms.  Extend or replace as needed for your domain.
    '首席执行官': 'Chief Executive Officer',
    '联合创始人': 'Co-Founder',
    '数字化转型': 'digital transformation',
    '碳中和': 'carbon neutrality',
    '智慧城市': 'smart city',
    '工业化': 'industrialization',
    '数字化': 'digitalization',
    '云平台': 'Cloud Platform',
    '注册建筑师': 'Registered Architect',
}

# ---------------------------------------------------------------------------
# Chinese date patterns — convert to English before translation so the model
# never sees 年/月/日 as standalone words.
# ---------------------------------------------------------------------------
MONTH_NAMES = [
    '', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]

# Full date    2025年10月22日  →  October 22, 2025
# Year+month   2025年10月     →  October 2025
# Month+day    10月22日       →  October 22
_CHINESE_FULL_DATE_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_CHINESE_YEAR_MONTH_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)')
_CHINESE_MONTH_DAY_RE = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日')

# Numeric date patterns — YYYY.MM.DD / YYYY-MM-DD → Month DD, YYYY
# (?<!\d) / (?!\d) prevent matching inside longer digit sequences (e.g.
# ISSN "1009-4067.2025.16.06" where 2025 is preceded by a digit).
_NUMERIC_DOT_DATE_RE = re.compile(r'(?<!\d)(\d{4})\.(\d{1,2})\.(\d{1,2})(?!\d)')
_NUMERIC_DASH_DATE_RE = re.compile(r'(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)')

# Double-date cleanup patterns
# NOTE: non-capturing group so that any pattern embedding _MONTHS_PAT keeps
# its own group numbering (e.g. _DATE_MONTH_YEAR_DAY_RE expects 1=month).
_MONTHS_PAT = (
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
)
# "October 13, 2025 2025.10.13" → "October 13, 2025"
_DOUBLE_DATE_RE = re.compile(
    rf'\b({_MONTHS_PAT}\s+\d{{1,2}},\s+\d{{4}})\s+\d{{4}}[-.]\d{{1,2}}[-.]\d{{1,2}}\b'
)
# "October 13, 2025 October 13, 2025" → "October 13, 2025" (per-run duplicate)
_DUP_EN_DATE_RE = re.compile(
    rf'\b({_MONTHS_PAT}\s+\d{{1,2}},\s+\d{{4}})\s+\1\b'
)

# URL detection pattern — matches URLs (http/https/www or domain-like patterns
# with .tld).  Used to protect URLs from being mangled by the translation model.
# The character class [^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef] excludes
# whitespace, CJK characters, CJK punctuation (U+3000-303F — this covers the
# Chinese full stop 。U+3002 which was previously swallowed into the URL match,
# leaving it un-cleaned) and fullwidth punctuation, so the URL match stops
# before any Chinese label prefix or trailing Chinese punctuation.
_URL_CHAR = r'[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]'
_URL_LINE_RE = re.compile(
    r'(?:https?://' + _URL_CHAR + r'+|www\.' + _URL_CHAR + r'+\.\w{2,}|[a-zA-Z0-9]' + _URL_CHAR + r'*\.(?:com|cn|net|org|edu|gov|io)\b' + _URL_CHAR + r'*)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Guardable placeholders — amounts / plain numbers / URLs / emails
#
# These are replaced with __G<n>__ tokens BEFORE the text is sent to the
# translation / polish models, and restored verbatim AFTER, so the model can
# never truncate, reformat or invent them (fixes badcase type 1 数字截断 and
# type 7 URL/邮箱损坏 at the root).
# ---------------------------------------------------------------------------
# Amounts with Chinese units → converted to an English amount (万=×10⁴, 亿=×10⁸)
_AMOUNT_CN_RE = re.compile(
    r'(\d[\d,，.]*)\s*(万亿美元|亿美元|亿人民币|亿元人民币|万元人民币|亿元|万元|千元|元人民币|美元|美金|人民币|亿|万|元)'
)
_AMOUNT_CN_MULT = {
    '万亿美元': 10 ** 12, '亿美元': 10 ** 8, '亿人民币': 10 ** 8, '亿元人民币': 10 ** 8,
    '万元人民币': 10 ** 4, '亿元': 10 ** 8, '万元': 10 ** 4, '千元': 10 ** 3,
    '元人民币': 1, '美元': 1, '美金': 1, '人民币': 1,
    '亿': 10 ** 8, '万': 10 ** 4, '元': 1,
}
# Plain numbers (with thousand separators / decimals). (?<![\d_]) / (?![\d_])
# exclude digits inside our own __G<n>__ placeholders.
_PLAIN_NUM_RE = re.compile(r'(?<![\d_])\d[\d,，.]*(?:\.\d+)?(?![\d_])')
# Inline URLs / emails embedded in longer text (protected so the model keeps
# them byte-for-byte; the dedicated URL-label shortcut handles URL-only lines).
_URL_INLINE_RE = re.compile(
    r'(?:https?://' + _URL_CHAR + r'+|www\.' + _URL_CHAR + r'+\.\w{2,})',
    re.IGNORECASE,
)
_EMAIL_INLINE_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w{2,}')
# Residual guard token — any __G<n>__ the model failed to keep verbatim is
# dropped rather than leaking into the output.
_GUARD_LEFTOVER_RE = re.compile(r'__G\d+__')

# ---------------------------------------------------------------------------
# Format normalization (badcase type 5/6 rule-based fixes)
# NOTE: _MONTHS_PAT contains its own capturing group, so it is wrapped in a
# non-capturing group here to keep the group numbers aligned (1=month, ...).
# ---------------------------------------------------------------------------
# "January 1987 13th" → "January 13, 1987" (wrong date word order)
_DATE_MONTH_YEAR_DAY_RE = re.compile(
    rf'\b((?:{_MONTHS_PAT}))\s+(\d{{4}})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b'
)
# "January 13th, 1987" → "January 13, 1987" (strip ordinal suffixes)
_DATE_ORDINAL_RE = re.compile(
    rf'\b((?:{_MONTHS_PAT}))\s+(\d{{1,2}})(?:st|nd|rd|th),\s+(\d{{4}})\b'
)
# "13 January 1987" → "January 13, 1987" (European order → US order)
_DATE_EURO_RE = re.compile(
    rf'\b(\d{{1,2}})\s+((?:{_MONTHS_PAT}))\s+(\d{{4}})\b'
)
# "RMB 400,000 yuan" / "RMB 400,000 Yuan" → "RMB 400,000" (RMB+Yuan redundancy)
_RMB_YUAN_RE = re.compile(r'\bRMB\s+([\d,]+\.?\d*)\s*(?:yuan|Yuan)\b')

# Stray punctuation the model appends after numbers/dates:
#   "November 25,,, 2024,.." → "November 25, 2024."
# Compress runs of commas/periods and comma-period pairs. Decimal separators
# (5.5), thousands (1,000), ellipses (...) and dotted IDs (1.2.3) stay intact.
_PUNCT_RUN_COMMA_RE = re.compile(r',{2,}')
_PUNCT_RUN_DOT_RE = re.compile(r'\.{2,}')
_PUNCT_COMMA_DOT_RE = re.compile(r',\.')

class TranslatorService:
    """
    Service for translating Chinese document text to English
    via the DeepSeek API (OpenAI-compatible endpoint).

    Provides glossary-aware replacement, Chinese residue detection, and
    Chinese label cleanup utilities.
    """

    def __init__(self):
        """Initialise the TranslatorService with the default model name."""
        self._model = settings.DEEPSEEK_MODEL

    # ------------------------------------------------------------------
    # URL-aware translation
    # ------------------------------------------------------------------

    @staticmethod
    def contains_url(text: str) -> bool:
        """Check whether *text* contains a URL pattern."""
        return bool(_URL_LINE_RE.search(text))

    @staticmethod
    def translate_url_label_line(text: str, glossary: dict[str, str]) -> str:
        """Translate a line that contains a URL.

        Only translates the Chinese prefix/label before the URL and leaves
        the URL itself untouched.  If the entire text is just a URL with no
        Chinese characters, returns it unchanged.

        Examples
        --------
        >>> translate_url_label_line("链接：https://www.example.com/news/202603/449040.html")
        'link: https://www.example.com/news/202603/449040.html'
        >>> translate_url_label_line("https://www.example.com")
        'https://www.example.com'
        """
        if not text:
            return text

        # Find all URL matches
        url_matches = list(_URL_LINE_RE.finditer(text))
        if not url_matches:
            return text

        # If there's no Chinese character in the text, return as-is
        if not re.search(r'[一-鿿]', text):
            return text

        # Map of common Chinese URL labels → English
        label_map = {
            '链接': 'link',
            '网址链接': 'URL',
            '网址': 'URL',
            '网站': 'Website',
            '参考链接': 'Reference link',
            '来源链接': 'Source link',
            '来源': 'Source',
            '参考': 'Reference',
        }

        # Replace the Chinese label before the URL
        result = text
        # Sort by length descending so "网址链接" is matched before "网址"
        for cn_label in sorted(label_map.keys(), key=len, reverse=True):
            # Match the label followed by optional colon/space then the URL
            pattern = re.escape(cn_label) + r'([：:]\s*)'
            result = re.sub(pattern, label_map[cn_label] + r'\1', result, count=1)

        # Re-find URL matches on the label-replaced text (positions may have
        # shifted after replacing 链接→link etc.)
        url_matches = list(_URL_LINE_RE.finditer(result))

        # Apply glossary replacements to any remaining Chinese text that is
        # NOT part of the URL — but only to segments outside URLs.
        # Split text into URL and non-URL segments.
        segments: list[tuple[str, bool]] = []  # (text, is_url)
        last_end = 0
        for m in url_matches:
            if m.start() > last_end:
                segments.append((result[last_end:m.start()], False))
            segments.append((m.group(0), True))
            last_end = m.end()
        if last_end < len(result):
            segments.append((result[last_end:], False))

        # Merge common-terms replacement onto non-URL segments only
        merged = dict(COMMON_TERMS)
        merged.update(glossary)
        sorted_terms = sorted(merged.keys(), key=len, reverse=True)

        rebuilt = []
        for seg_text, is_url in segments:
            if is_url or not seg_text:
                rebuilt.append(seg_text)
                continue
            # Apply glossary replacement to this non-URL segment
            seg_result = seg_text
            for term in sorted_terms:
                pattern = re.escape(term)
                seg_result = re.sub(pattern, merged[term], seg_result)
            rebuilt.append(seg_result)

        return ''.join(rebuilt)

    # ------------------------------------------------------------------
    # Guardable protection — amounts / numbers / URLs / emails
    # ------------------------------------------------------------------

    @staticmethod
    def _protect_guardables(text: str) -> tuple[str, dict[str, str]]:
        """Replace amounts, plain numbers, URLs and emails with __G<n>__
        placeholders so the translation/polish models cannot alter them.

        Order matters:
          1. amounts with Chinese units (converted to English amounts first,
             e.g. 1025.00万元 → RMB 10,250,000.00),
          2. inline URLs,
          3. inline emails,
          4. any remaining plain numbers.

        Returns ``(protected_text, guard_map)``; restore with
        ``_restore_guardables``.
        """
        if not text:
            return text, {}
        guard_map: dict[str, str] = {}
        counter = [0]

        def _make(original: str) -> str:
            token = f'__G{counter[0]}__'
            counter[0] += 1
            guard_map[token] = original
            return token

        result = text

        def _guard_amount(m: re.Match) -> str:
            raw = m.group(1).replace(',', '').replace('，', '')
            try:
                val = float(raw)
            except ValueError:
                return m.group(0)
            unit = m.group(2)
            mult = _AMOUNT_CN_MULT.get(unit, 1)
            total = val * mult
            currency = 'USD' if ('美元' in unit or '美金' in unit) else 'RMB'
            # Shorthand follows the CN unit granularity: 亿/万亿 units are
            # themselves coarse (integer 亿), so converting e.g. 500亿元 →
            # "RMB 50 billion" is exactly equivalent, and far more natural
            # than 50,000,000,000.00. Finer units (万/千/元) keep the full
            # number for precision (510万元 → RMB 5,100,000.00).
            if '亿' in unit:
                if total >= 10 ** 12:
                    rendered = f'{currency} {total / 10 ** 12:g} trillion'
                elif total >= 10 ** 9:
                    rendered = f'{currency} {total / 10 ** 9:g} billion'
                else:
                    rendered = f'{currency} {total / 10 ** 6:g} million'
            else:
                rendered = f'{currency} {total:,.2f}'
            return _make(rendered)

        result = _AMOUNT_CN_RE.sub(_guard_amount, result)
        result = _URL_INLINE_RE.sub(lambda m: _make(m.group(0)), result)
        result = _EMAIL_INLINE_RE.sub(lambda m: _make(m.group(0)), result)
        result = _PLAIN_NUM_RE.sub(lambda m: _make(m.group(0)), result)
        return result, guard_map

    @staticmethod
    def _restore_guardables(text: str, guard_map: dict[str, str]) -> str:
        """Restore guard placeholders in reverse-insertion order (stable).
        Any placeholder the model failed to keep is dropped."""
        if not text or not guard_map:
            return text if not guard_map else _GUARD_LEFTOVER_RE.sub('', text)
        result = text
        for token, original in guard_map.items():
            result = result.replace(token, original)
        return _GUARD_LEFTOVER_RE.sub('', result)

    # ------------------------------------------------------------------
    # Glossary-aware replacement
    # ------------------------------------------------------------------

    def replace_with_glossary(self, text: str, glossary: dict[str, str]) -> str:
        """
        Replace Chinese terms in *text* using longest-match-first strategy.

        Merges COMMON_TERMS as fallback (user glossary takes priority).
        Also performs space-normalized fuzzy matching so that glossary keys
        with spaces match text where spaces were omitted (e.g. "系统 V1.0"
        matches "系统V1.0").

        Parameters
        ----------
        text : str
            The text to process.
        glossary : dict[str, str]
            Mapping of Chinese term -> English translation.

        Returns
        -------
        str
            Text with glossary terms replaced.
        """
        if not text:
            return text

        # ── Quote normalization (same as GlossaryService) ──
        text = text.replace(chr(0x201C), chr(34)).replace(chr(0x201D), chr(34))
        parts = text.split(chr(34))
        text = ''
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                text += part
            elif i % 2 == 0:
                text += part + chr(0x201C)
            else:
                text += part + chr(0x201D)

        # Convert Chinese dates to English format before glossary replacement,
        # so year/month/day are never treated as standalone translatable words.
        text = self.convert_chinese_dates(text)

        # Merge with COMMON_TERMS fallback (user glossary takes priority)
        merged = dict(COMMON_TERMS)
        merged.update(glossary)
        if not merged:
            return text

        # Add space-normalized versions for fuzzy matching
        extended = dict(merged)
        for cn, en in merged.items():
            norm = cn.replace(' ', '').replace('　', '')
            if norm != cn and norm not in extended:
                extended[norm] = en

        # Sort by key length descending (longest match first)
        sorted_terms = sorted(extended.keys(), key=len, reverse=True)

        result = text
        for term in sorted_terms:
            pattern = re.escape(term)
            result = re.sub(pattern, extended[term], result)

        return result

    # ------------------------------------------------------------------
    # DeepSeek API translation
    # ------------------------------------------------------------------

    def _make_client(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> OpenAI:
        """Create an OpenAI client (always fresh for thread safety)."""
        return OpenAI(
            api_key=api_key or settings.DEEPSEEK_API_KEY,
            base_url=base_url or "https://api.deepseek.com",
            http_client=httpx.Client(
                transport=httpx.HTTPTransport(),
                timeout=httpx.Timeout(60.0, connect=10.0),
            ),
        )

    @staticmethod
    def _normalize_formats(text: str) -> str:
        """Rule-based format normalization for common type-5/6 issues.

        - Date word order: "January 1987 13th" → "January 13, 1987"
        - Ordinal suffixes: "January 13th, 1987" → "January 13, 1987"
        - European order: "13 January 1987" → "January 13, 1987"
        - RMB+Yuan redundancy: "RMB 400,000 yuan" → "RMB 400,000"

        Applied after translation/polish; only touches unambiguous patterns.
        """
        if not text:
            return text
        result = _DATE_MONTH_YEAR_DAY_RE.sub(
            lambda m: f"{m.group(1)} {int(m.group(3))}, {m.group(2)}", text)
        result = _DATE_ORDINAL_RE.sub(
            lambda m: f"{m.group(1)} {int(m.group(2))}, {m.group(3)}", result)
        result = _DATE_EURO_RE.sub(
            lambda m: f"{m.group(2)} {int(m.group(1))}, {m.group(3)}", result)
        result = _RMB_YUAN_RE.sub(r'RMB \1', result)

        # ── Stray punctuation cleanup ──
        # The model often appends junk commas/periods after dates or amounts
        # ("November 25,,, 2024,.."). Compress runs but preserve ellipses.
        # Order matters: dots first (so ",.." → ",."), then comma runs, then
        # the comma-period pair (",." → ".").
        result = _PUNCT_RUN_DOT_RE.sub(
            lambda m: '...' if len(m.group(0)) >= 3 else '.', result)
        result = _PUNCT_RUN_COMMA_RE.sub(',', result)
        result = _PUNCT_COMMA_DOT_RE.sub('.', result)
        return result

    @staticmethod
    def _split_long(text: str, max_len: int = 400) -> list[str]:
        """Split text into sentence-aligned chunks of at most *max_len* chars.

        Splits on Chinese/English sentence-ending punctuation so each chunk is
        a complete semantic unit (whole-paragraph translation quality without
        oversized single requests that risk tail truncation). Guarded
        placeholders (__G<n>__) contain no punctuation, so they are never
        split across chunk boundaries. Text without punctuation beyond max_len
        is hard-split.
        """
        if not text:
            return [text]
        if len(text) <= max_len:
            return [text]
        parts = re.split(r'(?<=[。；！？!?])', text)
        chunks: list[str] = []
        cur = ""
        for p in parts:
            if not p:
                continue
            if len(cur) + len(p) <= max_len:
                cur += p
            else:
                if cur:
                    chunks.append(cur)
                while len(p) > max_len:
                    chunks.append(p[:max_len])
                    p = p[max_len:]
                cur = p
        if cur:
            chunks.append(cur)
        return chunks

    def translate_text(
        self,
        text: str,
        glossary: dict[str, str],
        system_override: str | None = None,
        temperature: float | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Translate Chinese *text* to English via the API.

        The *glossary* is injected into the system prompt to ensure technical
        terms are translated consistently.

        Parameters
        ----------
        text : str
            Chinese text to translate.
        glossary : dict[str, str]
            Mapping of Chinese term -> English translation.
        system_override : str | None
            If provided, use this instead of the default translation system prompt.
        temperature : float | None
            Model temperature override.  Defaults to 0.3 if not specified.
        api_key, base_url, model : str | None
            Custom API credentials. If omitted, uses the server defaults.

        Returns
        -------
        str
            Translated English text. If the API call fails, returns the
            original text prefixed with ``[Translation Error]: ``.
        """
        # ── URL-aware shortcut ──
        # Only for text that is PRIMARILY a URL with a short Chinese label.
        # If there's substantial Chinese content (>= 20 chars), do a full
        # translation so the Chinese text actually gets translated.
        if self.contains_url(text) and len(re.findall(r'[一-鿿]', text)) < 20:
            return self.translate_url_label_line(text, glossary)
        client = self._make_client(api_key, base_url)
        active_model = model or self._model

        # Convert Chinese dates to English format BEFORE sending to API,
        # so the model never sees 年/月/日 as standalone words.
        processed_text = self.convert_chinese_dates(text)
        # Guard amounts / numbers / URLs / emails so the model can never
        # truncate, reformat or invent them (badcase type 1 & 7).
        processed_text, guard_map = self._protect_guardables(processed_text)
        # Split very long text into sentence chunks AFTER protection, so
        # guarded tokens (URLs/numbers/emails) can never be split across
        # chunk boundaries, and each chunk is a complete semantic unit.
        chunks = self._split_long(processed_text)

        combined = dict(COMMON_TERMS)
        combined.update(glossary)
        glossary_lines = "\n".join(
            f"{cn} → {en}" for cn, en in combined.items()
        )

        if system_override is not None:
            system_prompt = system_override
        else:
            system_prompt = (
                "You are a professional Chinese-to-English document translator. "
                "Translate the following Chinese text to English accurately and formally.\n"
                "Use the provided glossary for technical terms:\n"
                f"{glossary_lines}\n\n"
                "Preserve formatting markers such as (Seal), (Signature), [Image], [Barcode].\n"
            "When translating picture/image markers, always use [Image] (NOT [Photo] or [Picture]).\n"
                "Convert Chinese numbered lists (一、二、三… / 第一、第二、第三… / 1、2、3… / (一)(二)…) "
                "to English format (I. II. III. / 1. 2. 3. / (1) (2)…).\n"
                "IMPORTANT — Dates:\n"
                "- All Chinese date expressions (年/月/日) and numeric dates (YYYY.MM.DD / YYYY-MM-DD) "
                "have already been pre-converted to English format (e.g. 'October 22, 2025').\n"
                "- DO NOT modify, reinterpret, or add any date format. Preserve dates exactly as they appear.\n"
                "- DO NOT append original numeric date formats (like '2025.10.13' or '2025-10-13') "
                "after converted dates.\n"
                "IMPORTANT — Numbers, amounts, URLs and emails:\n"
                "- Amounts, numbers, URLs and emails are protected as __G<n>__ placeholders.\n"
                "- Preserve every __G<n>__ placeholder EXACTLY as-is; never translate, reformat,\n"
                "  truncate, merge or drop them. Do not invent values.\n"
                "Use Times New Roman style, formal tone, and double line spacing.\n"
                "Output only the translated text, no explanations."
            )

        try:
            if len(chunks) == 1:
                response = _api_completion(
                    client, active_model,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": chunks[0]},
                    ],
                    temperature if temperature is not None else 0.3,
                )
                translated = response.choices[0].message.content or ""
            else:
                parts = []
                for c in chunks:
                    response = _api_completion(
                        client, active_model,
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": c},
                        ],
                        temperature if temperature is not None else 0.3,
                    )
                    parts.append(response.choices[0].message.content or "")
                translated = ''.join(parts)
            if translated:
                translated = translated.strip()
            # Restore guarded numbers / amounts / URLs / emails verbatim.
            translated = self._restore_guardables(translated, guard_map)
            return translated if translated else text
        except Exception as e:
            return f"[Translation Error]: {text}"

    # ------------------------------------------------------------------
    # English polishing — post-translation quality pass
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_mechanical_errors(text: str, protected: set | None = None) -> str:
        """Fix deterministic mechanical errors: duplicated chars/words,
        missing spaces at word boundaries, punctuation, parentheses.

        *protected* is an optional whitelist of rare/proper-noun tokens that
        must never be deduplicated (see dedup_guard.build_protected_tokens).

        Applied to ALL translated text (both full-paragraph and per-run)
        regardless of whether the polish step runs."""
        if not text:
            return text

        # ── Protect URL segments from being mangled ──
        # Split text into URL and non-URL segments; only apply cleanup to
        # non-URL segments.
        url_matches = list(_URL_LINE_RE.finditer(text))
        if url_matches:
            segments: list[tuple[str, bool]] = []
            last_end = 0
            for m in url_matches:
                if m.start() > last_end:
                    segments.append((text[last_end:m.start()], False))
                segments.append((m.group(0), True))
                last_end = m.end()
            if last_end < len(text):
                segments.append((text[last_end:], False))

            rebuilt = []
            for seg_text, is_url in segments:
                if is_url or not seg_text:
                    rebuilt.append(seg_text)
                    continue
                rebuilt.append(TranslatorService._clean_segment(seg_text, protected))
            return ''.join(rebuilt)

        return TranslatorService._clean_segment(text, protected)

    # ------------------------------------------------------------------
    # Chinese punctuation → English mapping
    # ------------------------------------------------------------------
    _CN_PUNC_MAP = {
        '：': ':',    # ：
        '，': ',',    # ，
        '。': '.',    # 。
        '；': ';',    # ；
        '！': '!',    # ！
        '？': '?',    # ？
        '（': '(',    # （
        '）': ')',    # ）
        '【': '[',    # 【
        '】': ']',    # 】
        '“': '"',    # "
        '”': '"',    # "
        '《': '<',    # 《
        '》': '>',    # 》
        '、': ',',    # 、
        '．': '.',    # ．
    }

    @staticmethod
    def _clean_segment(text: str, protected: set | None = None) -> str:
        """Apply mechanical error cleanup to a non-URL text segment.

        *protected* is an optional whitelist of rare/proper-noun tokens that
        must never be deduplicated (see dedup_guard.build_protected_tokens).
        """
        if not text:
            return text

        # ── Chinese punctuation → English（must run FIRST）──
        result = text
        for cn_punc, en_punc in TranslatorService._CN_PUNC_MAP.items():
            result = result.replace(cn_punc, en_punc)

        # ── Double-date cleanup: two variants ──
        # "October 13, 2025 2025.10.13" → "October 13, 2025"
        # MUST run before word dedup (which would mistakenly collapse "2025 2025").
        result = _DOUBLE_DATE_RE.sub(r'\1', result)
        # "October 13, 2025 October 13, 2025" → "October 13, 2025" (per-run dup)
        result = _DUP_EN_DATE_RE.sub(r'\1', result)

        # ── Format normalization (type 5/6): date word order, ordinal suffixes,
        # European date order, RMB+Yuan redundancy ──
        result = TranslatorService._normalize_formats(result)

        # ── Graded phrase/word dedup with protections (dedup_guard) ──
        # Replaces the legacy blind regexes which could not distinguish stopwords,
        # content words and proper nouns, and had no audit trail. Auto-delete now
        # requires: adjacent + exact match + (>= 2 content words | stopword | ID
        # token) + no whitelist hit. Everything else is kept and audited.
        result, _audit = dedup_guard.dedup_text(result, protected)

        # 3+ consecutive identical chars (clear typo: "missspelled"→"mispelled")
        # Preserve Roman numerals (IVXLCDM) and digits to avoid mangling
        # "III"→"I", "10001"→"101", etc.
        def _dedup_char(m):
            ch = m.group(1)
            if ch.isdigit() or ch.lower() in 'ivxlcdm':
                return m.group(0)  # preserve numbers & Roman numerals
            return ch
        result = re.sub(r'(\w)\1{2,}', _dedup_char, result)
        # lowercase→UPPERCASE word boundary: "regionRelatively" → "region Relatively"
        result = re.sub(r'([a-z])([A-Z])', r'\1 \2', result)
        # Punctuation without trailing space: "climate.trend" → "climate. trend"
        result = re.sub(r'([.,;:!?])([A-Za-z])', r'\1 \2', result)
        # Word before opening paren: "PMV(Predicted" → "PMV (Predicted"
        result = re.sub(r'(\w)\(', r'\1 (', result)
        # Closing paren before word: "system)such" → "system) such"
        result = re.sub(r'\)(\w)', r') \1', result)
        # Apostrophe-s merging: "Germany'sFraunhofer" → "Germany's Fraunhofer"
        result = re.sub(r"([a-zA-Z])'s([A-Z])", r"\1's \2", result)
        # Double spaces
        result = re.sub(r' {2,}', ' ', result)
        # Double-date cleanup: "October 13, 2025 2025.10.13" → "October 13, 2025"
        result = _DOUBLE_DATE_RE.sub(r'\1', result)
        return result

    def polish_text(
        self,
        text: str,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Polish English *text* to read like natural, fluent academic English.

        Applies a deterministic pre-clean pass for mechanical errors (duplicated
        letters, repeated words, missing word-boundary spaces), then sends the
        text through the API with a proofreading system prompt that focuses on
        grammar, flow, word choice, and removal of translationese ("Chinglish").
        A final post-clean pass catches any residual mechanical issues.

        Parameters
        ----------
        text : str
            English text to polish / proofread.
        api_key, base_url, model : str | None
            Custom API credentials.  If omitted, uses the server defaults.

        Returns
        -------
        str
            Polished English text.  On API failure, returns the pre-cleaned
            *text*.
        """
        if not text or len(text) < 10:
            return text

        # ── Skip polishing for URL-containing lines ──
        # The model may misinterpret URLs as requests to access them and
        # return "I cannot access external URLs" messages.  URL lines have
        # already been handled by translate_url_label_line.
        if self.contains_url(text):
            return self._clean_mechanical_errors(text)

        # ── Pre-clean mechanical errors ──
        pre_cleaned = self._clean_mechanical_errors(text)

        # ── Date protection: replace dates with placeholders ──
        # so the polish model doesn't rewrite them
        _DATE_GUARD_RE = re.compile(
            rf'\b({_MONTHS_PAT}\s+\d{{1,2}},\s+\d{{4}})\b'
        )
        date_map: dict[str, str] = {}
        date_counter = 0

        def _guard_date(m: re.Match) -> str:
            nonlocal date_counter
            token = f'__DATE_{date_counter}__'
            date_map[token] = m.group(1)
            date_counter += 1
            return token

        pre_cleaned = _DATE_GUARD_RE.sub(_guard_date, pre_cleaned)
        # Also protect time ranges: "14:30-17:30" style
        _TIME_RANGE_RE = re.compile(r'\b(\d{1,2}:\d{2}[-–]\d{1,2}:\d{2})\b')
        time_map: dict[str, str] = {}
        time_counter = 0

        def _guard_time(m: re.Match) -> str:
            nonlocal time_counter
            token = f'__TIME_{time_counter}__'
            time_map[token] = m.group(1)
            time_counter += 1
            return token

        pre_cleaned = _TIME_RANGE_RE.sub(_guard_time, pre_cleaned)

        # ── Also guard numbers / amounts / URLs / emails so the polish model
        # cannot reformat or truncate them (badcase type 1 & 7). ──
        pre_cleaned, guard_map = self._protect_guardables(pre_cleaned)

        client = self._make_client(api_key, base_url)
        active_model = model or self._model

        system_prompt = (
            "You are a professional English proofreader for translated documents. "
            "Revise the following English text so it reads like natural, fluent, academic-level English "
            "written by a native speaker.\n\n"
            "CRITICAL — fix these specific issues:\n"
            "- Typographical errors: duplicated letters (e.g. \"decdecision\"→\"decision\") "
            "or truncated words (e.g. \"Massachu\"→\"Massachusetts\").\n"
            "- Duplicate words: ONLY remove exact, immediately-adjacent duplicated words\n"
            "  or short phrases (e.g. \"indoor indoor\"→\"indoor\"). Do NOT remove any word\n"
            "  that is not an immediate adjacent duplicate.\n"
            "- Run-together words with missing spaces (e.g. \"regionRelatively\"→\"region relatively\").\n"
            "- Grammar errors: subject-verb agreement, missing articles, incorrect prepositions, "
            "tense consistency.\n"
            "- Chinglish / translationese: unnatural word order, calques from Chinese, "
            "overly literal phrasing. Rewrite to sound like natural academic English.\n"
            "- Improve sentence flow: split run-on sentences, merge short choppy ones, add "
            "appropriate transitions.\n\n"
            "RULES:\n"
            "- Maintain a formal academic tone throughout.\n"
            "- Preserve ALL [bracketed markers] like [Seal], [Image], [Barcode] exactly.\n"
            "- Preserve all numbers, dates, proper nouns, and technical terms exactly.\n"
            "- Preserve every __G<n>__ placeholder EXACTLY as-is; never translate, reformat,\n"
            "  truncate, merge or drop them (they stand for amounts / numbers / URLs / emails).\n"
            "- CRITICAL: Keep ALL dates and time ranges UNCHANGED (e.g. 'October 22, 2025' "
            "or '14:30-17:30'). Do not reformat or append extra formats.\n"
            "- NEVER remove or merge any proper noun, person name, institution name, number,\n"
            "  technical term, or bracketed marker, even if it appears more than once in\n"
            "  different locations. Repeated terms at different positions are intentional\n"
            "  and MUST be preserved.\n"
            "- NEVER delete supplementary explanations, parallel structures, or semantically\n"
            "  similar phrasing — only exact, immediately-adjacent duplicates qualify for\n"
            "  removal.\n"
            "- Do NOT change or remove any factual information — only improve how it is expressed.\n"
            "- If the text contains truncated or clearly malformed words, use context to infer "
            "and restore the correct word.\n"
            "- Output ONLY the revised text, no explanations or commentary."
        )

        try:
            response = _api_completion(
                client, active_model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": pre_cleaned},
                ],
                0.2,
            )
            polished = response.choices[0].message.content
            result = polished.strip() if polished else pre_cleaned
        except Exception as e:
            result = pre_cleaned

        # ── Restore date / time placeholders ──
        for token, original in date_map.items():
            result = result.replace(token, original)
        for token, original in time_map.items():
            result = result.replace(token, original)
        # Also restore any placeholders still left in case of partial matches
        result = re.sub(r'__DATE_\d+__|__TIME_\d+__', '', result)

        # ── Restore guarded numbers / amounts / URLs / emails ──
        result = self._restore_guardables(result, guard_map)

        # ── Post-clean: catch anything the model missed ──
        result = self._clean_mechanical_errors(result)
        return result

    # ------------------------------------------------------------------
    # Quality-assurance helpers
    # ------------------------------------------------------------------

    @staticmethod
    def detect_chinese_residue(text: str) -> list[str]:
        """
        Scan *text* and return a list of any remaining Chinese characters
        found (using the Unicode CJK Unified Ideographs block).

        Parameters
        ----------
        text : str
            Text to scan.

        Returns
        -------
        list[str]
            List of individual Chinese characters found (empty if none).
        """
        return re.findall(r"[一-鿿]", text)

    # ------------------------------------------------------------------
    # Formatting feedback interpretation
    # ------------------------------------------------------------------

    def interpret_formatting_feedback(
        self,
        feedback: str,
        paragraphs_text: list[str],
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> dict:
        """Use the model to interpret formatting feedback and identify target paragraphs.

        Sends the full list of paragraph texts + the user's formatting request to
        the model, and returns a structured dict describing which paragraphs to
        modify and how.

        Parameters
        ----------
        feedback : str
            The user's formatting request (e.g. "将结尾的签名改为红色").
        paragraphs_text : list[str]
            All paragraph texts in the document (for context).

        Returns
        -------
        dict
            A dict with:
            - ``needs_retranslation`` (bool): whether content re-translation is also needed
            - ``actions`` (list[dict]): each dict has ``indices`` (list[int]), ``action`` (str),
              and ``value`` (str).  ``indices`` may be empty meaning "all paragraphs".
        """
        # Build a compact paragraph listing
        para_lines = []
        for i, t in enumerate(paragraphs_text):
            preview = t[:60].replace('\n', ' ')
            para_lines.append(f"  [{i}] {preview}")
        para_block = "\n".join(para_lines)

        system_prompt = (
            "You are a document formatting assistant. Given a list of paragraph texts "
            "and a user's formatting request, identify which paragraphs should be "
            "modified and what formatting to apply.\n\n"
            "Return ONLY valid JSON in this exact format:\n"
            "{\n"
            '  "needs_retranslation": false,\n'
            '  "actions": [\n'
            '    {\n'
            '      "indices": [0, 1, 2],\n'
            '      "action": "font_color",\n'
            '      "value": "FF0000"\n'
            '    }\n'
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- If the user asks about content changes, set needs_retranslation to true.\n"
            "- If formatting-only (e.g. \"make red\", \"change font\"), set needs_retranslation to false.\n"
            '- Possible actions: "font_color" (hex value), "line_spacing" ("single"/"double"/"1.5"), '
            '"bold" (true/false), "italic" (true/false), "font_name" (string).\n'
            "- If the request targets specific paragraphs (e.g. \"signature\", \"title\", \"结尾\"), "
            "list their indices.  If it targets the whole document, omit indices (empty array).\n"
            "- Use only the paragraph indices from the listing above.\n"
            "- If the request is ambiguous, target all paragraphs."
        )

        user_prompt = (
            f"Paragraphs:\n{para_block}\n\n"
            f"Formatting request: {feedback}"
        )

        try:
            raw = self.translate_text(
                user_prompt,
                glossary={},
                system_override=system_prompt,
                temperature=0.1,
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
            # Extract JSON from the response
            import json
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                if "actions" not in result:
                    result["actions"] = []
                if "needs_retranslation" not in result:
                    result["needs_retranslation"] = False
                return result
        except Exception:
            pass

        # Fallback: empty result (no formatting)
        return {"needs_retranslation": False, "actions": []}

    @staticmethod
    def _cn_numeral(n: int) -> str:
        """Return Chinese numeral *n* (1-10) as the character."""
        return ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'][n - 1]

    @staticmethod
    def _roman_numeral(n: int) -> str:
        """Return Roman numeral for *n* (1-10)."""
        return ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X'][n - 1]

    @staticmethod
    def _build_cn_enum_pattern() -> list[tuple[re.Pattern, str]]:
        """Build regex patterns for Chinese enumeration markers.

        Returns a list of ``(compiled_regex, replacement_template)`` tuples.
        """
        patterns = []
        for i in range(1, 11):
            cn = TranslatorService._cn_numeral(i)
            roman = TranslatorService._roman_numeral(i)
            # Line-start: "一、" → "I."
            patterns.append((
                re.compile(rf'(?<![^\s]){re.escape(cn)}、'),
                f'{roman}.',
            ))
            # Ordinal prefix: "第一、" → "I."
            patterns.append((
                re.compile(rf'(?<![^\s])第{re.escape(cn)}、'),
                f'{roman}.',
            ))
            # Parenthesized halfwidth: "(一)" → "(I)"
            patterns.append((
                re.compile(rf'\(({re.escape(cn)})\)'),
                rf'({roman})',
            ))
            # Parenthesized fullwidth: "（一）" → "(I)"
            patterns.append((
                re.compile(rf'（{re.escape(cn)}）'),
                rf'({roman})',
            ))

        # Arabic numeral with Chinese enumeration comma: "1、" → "1."
        for i in range(0, 10):
            patterns.append((
                re.compile(rf'(?<![^\s]){i}、'),
                f'{i}.',
            ))
        return patterns

    @staticmethod
    def fix_cn_labels(text: str) -> str:
        """
        Replace common Chinese formatting labels with their English equivalents.

        ============= ===========
        Chinese label English
        ============= ===========
        ``【图片】``   ``[Image]``
        ``【条形码】`` ``[Barcode]``
        ``【盖章】``   ``[Seal]``
        ============= ===========

        Also converts Chinese enumeration markers (一、二、三… → I. II. III.)
        as a post-processing fallback for any the model missed.

        Parameters
        ----------
        text : str
            Text containing Chinese labels.

        Returns
        -------
        str
            Text with labels replaced.
        """
        result = text
        replacements = {
            "【图片】": "[Image]",
            # Normalize photo/picture → Image
            "【照片】": "[Image]",
            "【图像】": "[Image]",
            "【条形码】": "[Barcode]",
            "【图标】": "[Logo]",
            "【盖章】": "[Seal]",
            "【英文材料无需翻译】": "[No Translation Required — Source Material in English]",
            "【英文材料，无需翻译】": "[No Translation Required — Source Material in English]",
            "【含越南语】": "",
            "【含越南文】": "",
            "【原文】": "[Original]",
            # Common field labels — replace before they surface in output
            "电子邮箱": "Email",
            "电子邮件": "Email",
            "邮箱": "Email",
            "联系电话": "Contact Number",
            "电话": "Tel",
            "网址": "URL",
            "链接": "Link",
            "来源": "Source",
        }
        for cn, en in replacements.items():
            result = result.replace(cn, en)
        # ── AI meta-instruction patterns ──
        # Remove instruction fragments the AI may leave in output.
        _AI_META_PATTERNS = [
            re.compile(r'此段内容为[^，。\n]*，请提供需翻译的[^。\n]*[。]?'),
            re.compile(r'您提供的文本是[^，。\n]*，[^。\n]*[。]?'),
            re.compile(r'原文[：:]\s*.+?\s*翻译[：:]\s*', re.DOTALL),
        ]
        for pat in _AI_META_PATTERNS:
            result = pat.sub('', result)

        # ── Image marker normalisation ──
        # AI sometimes outputs [Photo] or [Picture] instead of [Image].
        result = result.replace('[Photo]', '[Image]').replace('[Picture]', '[Image]')

        # Chinese enumeration fallback
        for pattern, replacement in TranslatorService._build_cn_enum_pattern():
            result = pattern.sub(replacement, result)

        return result

    @staticmethod
    def convert_chinese_dates(text: str) -> str:
        """Convert Chinese date expressions to English format.

        Handles three patterns:
          - 2025年10月22日  →  October 22, 2025
          - 2025年10月     →  October 2025  (standalone year+month)
          - 10月22日       →  October 22    (standalone month+day)

        Also handles a comma before 日 for edge cases like 2025年10月22日,
        Parameters
        ----------
        text : str
            Text possibly containing Chinese date expressions.

        Returns
        -------
        str
            Text with dates converted to English format.
        """
        def _full_date(m: re.Match) -> str:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1 <= mo <= 12):
                return m.group(0)  # not a real month, skip
            return f"{MONTH_NAMES[mo]} {d}, {y}"

        def _year_month(m: re.Match) -> str:
            y, mo = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12):
                return m.group(0)
            return f"{MONTH_NAMES[mo]} {y}"

        def _month_day(m: re.Match) -> str:
            mo, d = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12):
                return m.group(0)
            return f"{MONTH_NAMES[mo]} {d}"

        result = _CHINESE_FULL_DATE_RE.sub(_full_date, text)
        result = _CHINESE_YEAR_MONTH_RE.sub(_year_month, result)
        result = _CHINESE_MONTH_DAY_RE.sub(_month_day, result)
        # Also convert numeric dates: YYYY.MM.DD / YYYY-MM-DD
        result = _NUMERIC_DOT_DATE_RE.sub(_full_date, result)
        result = _NUMERIC_DASH_DATE_RE.sub(_full_date, result)

        # Also handle the translated string 年/月/日 as standalone words
        # (catch what the model might produce even after conversion)
        result = result.replace('年 ', ' ').replace('月 ', ' ').replace('日 ', ' ')
        return result
