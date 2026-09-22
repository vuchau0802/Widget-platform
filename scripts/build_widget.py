#!/usr/bin/env python3
"""Build script for widget.js — minifies and generates a content-hashed bundle.

Usage:
    python scripts/build_widget.py

Outputs:
    static/widget.min.js          — minified bundle
    static/widget.version.txt     — content hash (short SHA-256)

The version hash is used by the serving endpoint for cache-busting URLs.
"""
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "static", "widget.js")
OUT_MIN = os.path.join(ROOT, "static", "widget.min.js")
OUT_VER = os.path.join(ROOT, "static", "widget.version.txt")


def minify_js(code: str) -> str:
    """Lightweight JS minifier — no external deps needed."""
    # Remove single-line comments (but not URLs with //)
    code = re.sub(r'(?<![:/])//(?!\s*$).$', '', code, flags=re.MULTILINE)
    # Remove multi-line comments
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
    # Collapse whitespace (newlines, tabs, multiple spaces)
    code = re.sub(r'\s+', ' ', code)
    # Remove spaces around operators and punctuation
    code = re.sub(r'\s*([{};:,=+\-*/<>!&|?])\s*', r'\1', code)
    # Remove leading/trailing whitespace
    code = code.strip()
    return code


def content_hash(data: bytes) -> str:
    """Return first 12 chars of SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()[:12]


def main():
    if not os.path.exists(SRC):
        print(f"ERROR: Source file not found: {SRC}", file=sys.stderr)
        sys.exit(1)

    with open(SRC, "r", encoding="utf-8") as f:
        source = f.read()

    minified = minify_js(source)

    with open(OUT_MIN, "w", encoding="utf-8") as f:
        f.write(minified)

    h = content_hash(minified.encode("utf-8"))

    with open(OUT_VER, "w", encoding="utf-8") as f:
        f.write(h)

    src_size = len(source)
    min_size = len(minified)
    ratio = (1 - min_size / src_size) * 100 if src_size else 0

    print(f"Source:     {src_size:>6} bytes  ({SRC})")
    print(f"Minified:   {min_size:>6} bytes  ({OUT_MIN})")
    print(f"Reduction:  {ratio:.1f}%")
    print(f"Version:    {h}  ({OUT_VER})")


if __name__ == "__main__":
    main()
