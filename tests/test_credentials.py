#!/usr/bin/env python3
"""Tests del detector de credenciales (regex + placeholders)."""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Aislar los patrones sin importar todo el handler
_CREDENTIAL_PATTERNS = [
    (r"postgresql://[\w._\-]+:[^@\s]+@[\w.\-]+:\d+/\w+(?:\?[^\s]*)?",  "SUPABASE_DB_URL"),
    (r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{60,}",                            "ANTHROPIC_KEY"),
    (r"sk-proj-[A-Za-z0-9_\-]{80,}",                                    "OPENAI_API_KEY"),
    (r"sk-[A-Za-z0-9]{40,}",                                            "OPENAI_API_KEY"),
    (r"github_pat_[A-Za-z0-9_]{50,}",                                   "GITHUB_TOKEN"),
    (r"ghp_[A-Za-z0-9]{30,}",                                           "GITHUB_TOKEN"),
]

_PLACEHOLDERS = ["[YOUR-PASSWORD]", "<password>", "YOUR_PASSWORD", "xxxxxxxx", "CHANGEME"]


def _detect(text):
    seen = set()
    out = []
    for pat, key in _CREDENTIAL_PATTERNS:
        for m in re.finditer(pat, text):
            v = m.group(0)
            if v in seen or any(p in v for p in _PLACEHOLDERS):
                continue
            seen.add(v)
            out.append(key)
    return out


def test(desc, text, expected):
    got = _detect(text)
    ok = "OK  " if got == expected else "FAIL"
    print(f"{ok} {desc}: got={got} expected={expected}")
    return got == expected


def main():
    fails = 0
    if not test("hola sin creds", "hola cuki, qué onda?", []):
        fails += 1
    if not test("github pat", "pasá este ghp_abcdefghijklmnopqrstuvwxyz1234", ["GITHUB_TOKEN"]):
        fails += 1
    if not test("postgres URL real", "postgresql://postgres:abc123secret@db.xyz.supabase.co:5432/postgres", ["SUPABASE_DB_URL"]):
        fails += 1
    if not test("postgres URL con placeholder — debe ignorar", "postgresql://postgres:[YOUR-PASSWORD]@db.xyz.supabase.co:5432/postgres", []):
        fails += 1
    if not test("ant key", "sk-ant-api03-" + "a"*80, ["ANTHROPIC_KEY"]):
        fails += 1
    if not test("openai proj", "sk-proj-" + "X"*100, ["OPENAI_API_KEY"]):
        fails += 1
    print(f"\n{'OK' if fails == 0 else 'FAIL'} · {fails} fallos")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
