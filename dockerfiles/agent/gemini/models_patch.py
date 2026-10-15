#!/usr/bin/env python3
"""Register extra gemini flash models into a from-source gemini-cli build.

The pinned gemini-cli recognizes gemini flash models only up to the GA 3.5
generation. A newer tag (e.g. gemini-3.8-flash) is unknown to it: resolveModel()
classifies any "*-flash" string as the flash tier and silently collapses it to the
GA flash model on the wire, so the match runs on 3.5 while artifacts record the
requested tag. Our api-router upstream DOES serve these newer tags correctly, so we
teach the CLI to emit them verbatim instead of collapsing them.

Applied at image-build time against packages/core/src/config/models.ts. Idempotent;
fails loudly if an anchor is missing (a CLI version bump that moves these must be
re-reviewed). Keep GEMINI_REGISTERED_EXTRA_MODELS in agents/gemini.py in sync with
the set below.

Usage: models_patch.py <path-to-models.ts>
"""
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()
orig = src

SET_DEF = (
    "// CyberArena: flash models newer than this CLI's GA flash that we support\n"
    "// end-to-end (our api-router upstream serves them). Registered so resolveModel()\n"
    "// returns them verbatim instead of collapsing every *-flash tag onto the GA flash.\n"
    "export const CYBERARENA_EXTRA_FLASH_MODELS = new Set<string>([\n"
    "  'gemini-3.7-flash',\n"
    "  'gemini-3.8-flash',\n"
    "]);\n\n"
)

if "CYBERARENA_EXTRA_FLASH_MODELS" in src:
    print("already patched:", path)
    sys.exit(0)

# 1) define the set right before VALID_GEMINI_MODELS
a1 = "export const VALID_GEMINI_MODELS = new Set(["
assert a1 in src, "anchor VALID_GEMINI_MODELS not found"
src = src.replace(a1, SET_DEF + a1, 1)

# 2) include it in VALID_GEMINI_MODELS
src = src.replace(a1 + "\n", a1 + "\n  ...CYBERARENA_EXTRA_FLASH_MODELS,\n", 1)

# 3) exclude registered models from the flash-collapse in resolveModel()
a3 = "    normalizedModel !== PREVIEW_GEMINI_FLASH_MODEL\n  ) {"
assert a3 in src, "anchor for flash-collapse condition not found"
src = src.replace(
    a3,
    "    normalizedModel !== PREVIEW_GEMINI_FLASH_MODEL &&\n"
    "    !CYBERARENA_EXTRA_FLASH_MODELS.has(normalizedModel)\n  ) {",
    1,
)

assert src != orig, "no changes applied"
open(path, "w", encoding="utf-8").write(src)
print("patched:", path)
