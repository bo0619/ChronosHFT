# Governance contracts

`governance/contracts.py` is the dependency-neutral source of durable schema
versions. Runtime modules and offline tools import the same identifiers; tools
must not define private copies.

Build the deterministic release inventory after the deploy tree is final:

```powershell
.\.venv\Scripts\python.exe scripts\build_release_manifest.py build
.\.venv\Scripts\python.exe scripts\build_release_manifest.py verify
```

The manifest covers every production/offline Python module, configuration
schema document, Web asset, deployment asset, and dependency lock file. It has
no timestamp or absolute path, so identical release bytes produce identical
JSON and the same `release_digest`.

Live approvals must use `chronoshft.calibration_approval.v3`. The signed
payload contains both `canonical_config_sha256` and `release_digest`, plus the
path to the verified `release-manifest.json`. v1/v2 approvals are rejected and
must be reissued; startup never upgrades them.
