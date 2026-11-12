"""Version-independent normalized-source fingerprint for strategy logic.

The fingerprint is based on Python tokenizer token names and token text, not
ast.dump() output. Comments and layout-only tokens are ignored. String literals
(including docstrings) remain part of the fingerprint because they are observable
runtime values. The strategy and indicator files use syntax supported by Python
3.11-3.13; the CI matrix verifies those runtimes explicitly.
"""
from __future__ import annotations

import hashlib
import io
import tokenize
from pathlib import Path
from typing import Mapping

STRATEGY_VERSION = "ema20-ema50-rsi14-atr14-v1"
STRATEGY_SOURCE_HASH = "sha256:21a79c485fddecc7c7591b5faa9b2c004ecfdfc1f7a6e651423d3535f27e504a"

# Append a new (version, hash) pair for every strategy/indicator logic change.
# Never rewrite an existing release entry: it makes forgotten version bumps testable.
STRATEGY_VERSION_HISTORY = (
    (
        "ema20-ema50-rsi14-atr14-v1",
        "sha256:21a79c485fddecc7c7591b5faa9b2c004ecfdfc1f7a6e651423d3535f27e504a",
    ),
)

# Token types representing layout or comments do not affect executable source.
_IGNORED_TOKEN_NAMES = frozenset(
    {"ENCODING", "NL", "NEWLINE", "INDENT", "DEDENT", "COMMENT", "ENDMARKER"}
)


def strategy_source_hash(sources: Mapping[str, str]) -> str:
    """Hash normalized Python tokens, ignoring comments and formatting.

    Token names are used instead of their integer IDs because IDs are an
    implementation detail. Each token is length-prefixed to prevent ambiguous
    concatenations. Filenames and source ordering are included deterministically.
    """
    digest = hashlib.sha256()
    for filename in sorted(sources):
        digest.update(filename.encode("utf-8"))
        digest.update(b"\\0")
        tokens = tokenize.generate_tokens(io.StringIO(sources[filename]).readline)
        for token in tokens:
            token_name = tokenize.tok_name[token.type]
            if token_name in _IGNORED_TOKEN_NAMES:
                continue
            value = token.string
            digest.update(token_name.encode("ascii"))
            digest.update(b":")
            digest.update(str(len(value)).encode("ascii"))
            digest.update(b":")
            digest.update(value.encode("utf-8"))
            digest.update(b"\\0")
        digest.update(b"\\0")
    return "sha256:" + digest.hexdigest()


def current_strategy_source_hash() -> str:
    """Return the normalized-source hash of strategy.py and indicators.py."""
    package = Path(__file__).resolve().parent
    sources = {
        name: (package / name).read_text(encoding="utf-8")
        for name in ("strategy.py", "indicators.py")
    }
    return strategy_source_hash(sources)
