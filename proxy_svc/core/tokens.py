"""token 计数。

装了 tiktoken 用 cl100k_base 精确计数；没装则估算：
中日韩字符 1 字 1 token，其余按 4 字符 1 token。
"""
from __future__ import annotations

import math
import re

try:                                    # 可选依赖
    import tiktoken                     # type: ignore
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:                       # pragma: no cover
    _ENC = None

_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
    r"\uf900-\ufaff\uff00-\uffef]")

TOKENIZER = "tiktoken/cl100k_base" if _ENC is not None else "estimate"


def count_tokens(text: str | None) -> int:
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text, disallowed_special=()))
        except Exception:
            pass
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def usage_of(question: str, answer: str | None) -> dict:
    p = count_tokens(question)
    c = count_tokens(answer)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
