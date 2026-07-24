"""Translator service for calling DeepSeek API to translate Chinese to English."""

import re
import httpx
from datetime import datetime
from openai import OpenAI
from app.config import settings

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
_MONTHS_PAT = (
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
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
# The character class [^\s\u4e00-\u9fff\uff00-\uffef] excludes whitespace,
# CJK characters, and fullwidth punctuation so the URL match stops before
# any Chinese label prefix.
_URL_CHAR = r'[^\s\u4e00-\u9fff\uff00-\uffef]'
_URL_LINE_RE = re.compile(
    r'(?:https?://' + _URL_CHAR + r'+|www\.' + _URL_CHAR + r'+\.\w{2,}|[a-zA-Z0-9]' + _URL_CHAR + r'*\.(?:com|cn|net|org|edu|gov|io)\b' + _URL_CHAR + r'*)',
    re.IGNORECASE,
)

class TranslatorService:
    """
    Service for translating Chinese immigration document text to English
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

        combined = dict(COMMON_TERMS)
        combined.update(glossary)
        glossary_lines = "\n".join(
            f"{cn} → {en}" for cn, en in combined.items()
        )

        if system_override is not None:
            system_prompt = system_override
        else:
            system_prompt = (
                "You are a professional immigration document translator. "
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
                "Use Times New Roman style, formal tone, and double line spacing.\n"
                "Output only the translated text, no explanations."
            )

        try:
            response = client.chat.completions.create(
                model=active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": processed_text},
                ],
                temperature=temperature if temperature is not None else 0.3,
            )
            translated = response.choices[0].message.content
            return translated.strip() if translated else text
        except Exception as e:
            return f"[Translation Error]: {text}"

    # ------------------------------------------------------------------
    # English polishing — post-translation quality pass
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_mechanical_errors(text: str) -> str:
        """Fix deterministic mechanical errors: duplicated chars/words,
        missing spaces at word boundaries, punctuation, parentheses.

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
                rebuilt.append(TranslatorService._clean_segment(seg_text))
            return ''.join(rebuilt)

        return TranslatorService._clean_segment(text)

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
    def _clean_segment(text: str) -> str:
        """Apply mechanical error cleanup to a non-URL text segment."""
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

        # ── Phrase-level dedup: "14:30-17:30 14:30-17:30" → "14:30-17:30"
        # Word dedup (\b\w+\b) can't catch time ranges with colons/hyphens.
        # This regex catches any non-empty token sequence that repeats immediately.
        result = re.sub(
            r'(\S+(?:\s+\S+){0,6})\s+\1\b',
            r'\1',
            result,
        )

        # 3+ consecutive identical chars (clear typo: "missspelled"→"mispelled")
        # Exclude Roman numeral characters (I V X L C D M) and their lowercase
        # forms to avoid mangling "III"→"I", "VIII"→"VI", "XXX"→"X" etc.
        def _dedup_char(m):
            ch = m.group(1)
            if ch.lower() in 'ivxlcdm':
                return m.group(0)  # preserve Roman numerals
            return ch
        result = re.sub(r'(\w)\1{2,}', _dedup_char, result)
        # Duplicated whole word: "the the" or "has gradually has gradually"
        # Skip URL-related tokens to avoid mangling domains
        # Also skip digit-only words to avoid breaking dates (e.g. "2025 2025.10.13")
        def _dedup_word(m):
            word = m.group(1)
            if word.lower() in ('www', 'http', 'https', 'ftp'):
                return m.group(0)
            if word.isdigit():
                return m.group(0)  # don't dedup numeric tokens
            return word
        result = re.sub(r'\b(\w+)\s+\1\b', _dedup_word, result)
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

        client = self._make_client(api_key, base_url)
        active_model = model or self._model

        system_prompt = (
            "You are a professional English proofreader for academic and immigration documents. "
            "Revise the following English text so it reads like natural, fluent, academic-level English "
            "written by a native speaker.\n\n"
            "CRITICAL — fix these specific issues:\n"
            "- Typographical errors: duplicated letters (e.g. \"decdecision\"→\"decision\") "
            "or truncated words (e.g. \"Massachu\"→\"Massachusetts\").\n"
            "- Duplicate / repeated words (e.g. \"indoor indoor\"→\"indoor\").\n"
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
            "- CRITICAL: Keep ALL dates and time ranges UNCHANGED (e.g. 'October 22, 2025' "
            "or '14:30-17:30'). Do not reformat or append extra formats.\n"
            "- Do NOT change or remove any factual information — only improve how it is expressed.\n"
            "- If the text contains truncated or clearly malformed words, use context to infer "
            "and restore the correct word.\n"
            "- Output ONLY the revised text, no explanations or commentary."
        )

        try:
            response = client.chat.completions.create(
                model=active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": pre_cleaned},
                ],
                temperature=0.2,
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
