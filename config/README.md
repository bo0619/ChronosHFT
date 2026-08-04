# Versioned configuration contract

The tracked Paper configuration uses the strict
`chronoshft.config_manifest.v3`. The manifest declares:

- `config_version: 3`, the version of the composed runtime document;
- `unknown_keys: "reject"`, the mandatory forward-compatibility policy;
- an expected fragment identity and integer version for every include.

Each included file declares the matching
`chronoshft.config_fragment.v3` envelope using `$schema`, `fragment`, and
`version`. The loader removes those three metadata fields before merging the
fragment. Every remaining object and leaf is checked against the centralized
registry in `infrastructure/config_schema.py`, including required fields,
JSON types, finite numeric ranges, enums, array uniqueness, and dynamic-map
key formats. Operational keys that are not in the declared fragment contract
are rejected. Keys beginning with `_comment` are the only extension point;
their values must be strings and they do not enter runtime policy.

The v3 loader also checks invariants that have owners in different files. The
checks cover capital scaling against risk and symbol budgets, strategy model
registration/readiness, rate-limit reserves, queue thresholds, clock
thresholds, resource caps, Paper market-data identity, persistence queue
sizes, and model calibration bounds.

## Compatibility and upgrades

Runtime startup accepts only `chronoshft.config_manifest.v3`. Older manifests
and monolithic runtime documents are inputs to the explicit offline migration
tool only; they are never upgraded or interpreted by a running process.

There is no best-effort forward compatibility for v3 fragments. Adding or
renaming an operational field, changing its type/range/meaning, or changing a
cross-file invariant requires a fragment-version update in the schema
registry and the corresponding manifest entry. Unsupported manifest,
document, and fragment versions fail closed. Code and configuration are
therefore deployed atomically; downgrading the code while retaining newer
configuration is intentionally rejected.

Live approval uses `chronoshft.calibration_approval.v3` and binds both the
canonical normalized configuration digest and the complete deterministic
release digest. Any older approval is rejected and must be signed again after
offline migration.

Run the same gate used by startup after every change:

```powershell
.\.venv\Scripts\python.exe main.py --config config.json --check-config
```
