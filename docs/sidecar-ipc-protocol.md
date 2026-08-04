# Risk sidecar IPC protocol

The current wire contract is version 2. Version 2 changes safety semantics:
account snapshots expose separate daily and deployment cash-flow totals,
durable status can carry writer/owner/safety epochs, and terminal flatness is
represented by an account-wide `FlatProof`. These are not v1-compatible
diagnostic additions, so mixed v1/v2 peers fail the launch handshake.

The risk sidecar is started with Python multiprocessing's `spawn` context from
the same checkout as its parent. The IPC contract therefore supports exactly
one protocol version at a time. Mixed-release or rolling parent/sidecar pairs
are not supported.

## Wire contract

`SidecarProtocol.VERSION` is the authoritative integer protocol version. Every
heartbeat, control request, and child status contains `protocol_version`.
Missing, non-integer, Boolean, or non-matching versions are rejected.

The spawn settings form the first half of the handshake:

- `parent_capabilities` lists the features offered by the parent.
- `required_child_capabilities` lists the child features required by the
  parent.
- The child validates both sets before console setup, credentials, network
  clients, snapshot workers, or risk-state processing starts.

Version 2 advertises `state.version.v2` from the parent and
`state.cas.v2`, `cash-flow.split.v1`, and `account.flat-proof.v1` from the
child. A same-release deployment may continue using the legacy JSON state
adapter during migration, but it must not claim a healthy v2 CAS lineage in
status until a recover-only v2 store has been opened and its writer fence is
held.

Each child status forms the second half of the handshake. It advertises
`capabilities` and sets `protocol_handshake_complete=true` only after the
launch contract was accepted. The parent validates the version, capability
set, and handshake flag before committing the status. An enabled supervisor
cannot report healthy until that handshake is complete.

Initialization failures still publish the child's current version and
capabilities. If launch validation failed, the failure status sets
`protocol_handshake_complete=false`, allowing the parent to expose the reason
without treating the peer as compatible.

## Compatibility policy

- Version equality is mandatory. There is no legacy-message fallback.
- Additive diagnostic fields and optional capabilities may be introduced
  without a version bump when existing required semantics do not change.
- Adding a required capability, changing a field's type or safety meaning,
  removing a field/capability, or changing command/ACK behavior requires a
  protocol version bump and coordinated parent/child deployment.
- Unknown optional capabilities are tolerated; every required capability must
  be present.
- A missing or incompatible launch contract prevents child initialization. A
  malformed or incompatible command is ignored, so the existing parent
  heartbeat timeout drives the child into its fail-closed path. A malformed or
  incompatible status is discarded, so the parent keeps OMS health closed.

Tests should construct launch settings with
`SidecarProtocol.with_launch_contract`, parent messages with
`SidecarProtocol.parent_message`, and wire statuses with
`SidecarProtocol.child_status(..., handshake_complete=...)`. The handshake
argument is mandatory and strictly Boolean so a caller cannot accidentally
publish an unverified peer as compatible. These helpers keep fixtures on the
same contract as production code.
