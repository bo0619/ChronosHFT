"""Version identifiers for durable governance and release contracts.

This module deliberately imports only the Python standard library. Runtime
code and offline tools may depend on these names without creating a dependency
on either implementation layer.
"""

CONFIG_MANIFEST_SCHEMA = "chronoshft.config_manifest.v3"
CONFIG_DOCUMENT_VERSION = 3
CONFIG_FRAGMENT_SCHEMA = "chronoshft.config_fragment.v3"
CONFIG_UNKNOWN_KEY_POLICY = "reject"

RELEASE_MANIFEST_SCHEMA = "chronoshft.release_manifest.v3"
RELEASE_DIGEST_ALGORITHM = "SHA-256"
RELEASE_DIGEST_DOMAIN = b"chronoshft.release_manifest.v3\0"

LIVE_APPROVAL_SCHEMA = "chronoshft.calibration_approval.v3"
LIVE_APPROVAL_SIGNATURE_DOMAIN = b"chronoshft.calibration_approval.v3\0"
CANONICAL_CONFIG_SCHEMA = "chronoshft.canonical_config.v3"
CANONICAL_CONFIG_DIGEST_DOMAIN = b"chronoshft.canonical_config.v3\0"

# Target durable versions are centralized here even while their offline
# migrations are implemented by their owning subsystems.
OMS_JOURNAL_RECORD_VERSION = 3
SIDECAR_IPC_VERSION = 2
RPI_CALIBRATION_ARTIFACT_SCHEMA = "chronoshft.glft_rpi_calibration.v3"
RPI_EXPOSURE_SAMPLE_SCHEMA = "chronoshft.rpi_exposure_sample.v2"


__all__ = [
    "CANONICAL_CONFIG_DIGEST_DOMAIN",
    "CANONICAL_CONFIG_SCHEMA",
    "CONFIG_DOCUMENT_VERSION",
    "CONFIG_FRAGMENT_SCHEMA",
    "CONFIG_MANIFEST_SCHEMA",
    "CONFIG_UNKNOWN_KEY_POLICY",
    "LIVE_APPROVAL_SCHEMA",
    "LIVE_APPROVAL_SIGNATURE_DOMAIN",
    "OMS_JOURNAL_RECORD_VERSION",
    "RELEASE_DIGEST_ALGORITHM",
    "RELEASE_DIGEST_DOMAIN",
    "RELEASE_MANIFEST_SCHEMA",
    "RPI_CALIBRATION_ARTIFACT_SCHEMA",
    "RPI_EXPOSURE_SAMPLE_SCHEMA",
    "SIDECAR_IPC_VERSION",
]
