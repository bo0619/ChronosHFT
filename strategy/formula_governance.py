"""Release-governance metadata kept outside model implementation digests."""

# Formula fixtures prove internal consistency only. A release may add a
# formula version here after external RPI, markout, and OOS evidence has been
# independently approved and signed. This file is intentionally excluded from
# implementation_sha256_for_model: changing governance must not rewrite the
# identity of the already-reviewed execution implementation.
LIVE_APPROVED_FORMULA_VERSIONS: frozenset[str] = frozenset()
