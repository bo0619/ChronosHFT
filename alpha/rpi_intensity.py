from __future__ import annotations

import math
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RPIExposureBin:
    """Aggregated exposure for one acknowledged RPI quote depth."""

    depth_bps: float
    exposure_seconds: float
    fill_count: int
    sample_count: int = 1


@dataclass(frozen=True, slots=True)
class RPIIntensityRequirements:
    """Minimum evidence required before an intensity fit may be consumed."""

    min_sample_count: int = 30
    min_depth_level_count: int = 3
    min_total_exposure_seconds: float = 60.0
    min_fill_count: int = 10
    min_depth_span_bps: float = 0.5
    min_k_per_bps: float = 1e-6
    max_k_per_bps: float = 100.0

    def __post_init__(self) -> None:
        _require_positive_int(self.min_sample_count, "min_sample_count")
        _require_positive_int(
            self.min_depth_level_count,
            "min_depth_level_count",
        )
        _require_positive_finite(
            self.min_total_exposure_seconds,
            "min_total_exposure_seconds",
        )
        _require_positive_int(self.min_fill_count, "min_fill_count")
        _require_positive_finite(
            self.min_depth_span_bps,
            "min_depth_span_bps",
        )
        min_k = _require_nonnegative_finite(
            self.min_k_per_bps,
            "min_k_per_bps",
        )
        max_k = _require_positive_finite(
            self.max_k_per_bps,
            "max_k_per_bps",
        )
        if max_k <= min_k:
            raise ValueError("max_k_per_bps must be greater than min_k_per_bps")


@dataclass(frozen=True, slots=True)
class RPIIntensityEstimate:
    """Fail-closed result for lambda(delta) = A * exp(-k * delta)."""

    ready: bool
    state: str
    A_per_s: float | None
    k_per_bps: float | None
    sample_count: int
    depth_level_count: int
    total_exposure_seconds: float
    fill_count: int
    zero_fill_depth_level_count: int
    zero_fill_exposure_seconds: float
    invalid_sample_count: int
    log_likelihood: float | None
    reasons: tuple[str, ...]
    bins: tuple[RPIExposureBin, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "state": self.state,
            "A_per_s": self.A_per_s,
            "k_per_bps": self.k_per_bps,
            "sample_count": self.sample_count,
            "depth_level_count": self.depth_level_count,
            "total_exposure_seconds": self.total_exposure_seconds,
            "fill_count": self.fill_count,
            "zero_fill_depth_level_count": self.zero_fill_depth_level_count,
            "zero_fill_exposure_seconds": self.zero_fill_exposure_seconds,
            "invalid_sample_count": self.invalid_sample_count,
            "log_likelihood": self.log_likelihood,
            "reasons": list(self.reasons),
            "bins": [
                {
                    "depth_bps": item.depth_bps,
                    "exposure_seconds": item.exposure_seconds,
                    "fill_count": item.fill_count,
                    "sample_count": item.sample_count,
                }
                for item in self.bins
            ],
        }


@dataclass(frozen=True, slots=True)
class RPIOrderExposureResult:
    """Terminal exposure outcome for one acknowledged RPI order."""

    censored: bool
    censor_reason: str
    acknowledged_at_monotonic: float
    terminal_at_monotonic: float
    fill_count: int
    exposure_bins: tuple[RPIExposureBin, ...]


@dataclass(slots=True)
class _MutableExposureBin:
    exposure_seconds: float = 0.0
    fill_count: int = 0
    sample_count: int = 0


class RPIOrderExposure:
    """Piecewise-constant depth exposure for one venue-acknowledged order."""

    def __init__(
        self,
        *,
        acknowledged_at_monotonic: Any,
        initial_depth_bps: Any,
        depth_bin_width_bps: Any,
        censor_reason: str = "",
    ) -> None:
        acknowledged_at = _finite_float(acknowledged_at_monotonic)
        if acknowledged_at is None or acknowledged_at <= 0.0:
            raise ValueError(
                "acknowledged_at_monotonic must be positive and finite"
            )
        bin_width = _finite_float(depth_bin_width_bps)
        if bin_width is None or bin_width <= 0.0:
            raise ValueError("depth_bin_width_bps must be positive and finite")

        self.acknowledged_at_monotonic = acknowledged_at
        self.depth_bin_width_bps = bin_width
        self.last_observation_monotonic = acknowledged_at
        self._bins: dict[float, _MutableExposureBin] = {}
        self._seen_trade_ids: set[str] = set()
        self._fill_count = 0
        self._censor_reason = str(censor_reason or "").strip()
        self._current_depth_bin = self._quantized_depth(initial_depth_bps)
        if self._current_depth_bin is None and not self._censor_reason:
            self._censor_reason = "invalid_initial_depth"

    @property
    def censored(self) -> bool:
        return bool(self._censor_reason)

    @property
    def censor_reason(self) -> str:
        return self._censor_reason

    @property
    def fill_count(self) -> int:
        return self._fill_count

    def mark_censored(self, reason: str) -> None:
        if not self._censor_reason:
            self._censor_reason = str(reason or "unspecified_censor").strip()

    def observe_depth(
        self,
        *,
        observed_at_monotonic: Any,
        depth_bps: Any,
    ) -> bool:
        """Integrate the prior depth until this contemporaneous book event."""

        if self.censored:
            return False
        observed_at = _finite_float(observed_at_monotonic)
        if observed_at is None or observed_at <= 0.0:
            self.mark_censored("invalid_book_monotonic")
            return False
        if observed_at < self.last_observation_monotonic:
            self.mark_censored("non_monotonic_book_time")
            return False
        if observed_at > self.last_observation_monotonic and not self._accrue(
            observed_at
        ):
            return False

        next_depth = self._quantized_depth(depth_bps)
        if next_depth is None:
            self.mark_censored("invalid_contemporaneous_depth")
            return False
        self._current_depth_bin = next_depth
        return True

    def record_fill(self, trade_id: Any) -> bool:
        """Attribute one unique fill event to the current depth bin."""

        if self.censored:
            return False
        normalized_trade_id = str(trade_id or "").strip()
        if not normalized_trade_id:
            self.mark_censored("missing_trade_id")
            return False
        if normalized_trade_id in self._seen_trade_ids:
            return False
        if self._current_depth_bin is None:
            self.mark_censored("fill_without_depth")
            return False

        self._seen_trade_ids.add(normalized_trade_id)
        bucket = self._bins.setdefault(
            self._current_depth_bin,
            _MutableExposureBin(),
        )
        bucket.fill_count += 1
        self._fill_count += 1
        return True

    def finish(
        self,
        *,
        terminal_at_monotonic: Any,
    ) -> RPIOrderExposureResult:
        terminal_at = _finite_float(terminal_at_monotonic)
        if terminal_at is None or terminal_at <= 0.0:
            self.mark_censored("invalid_terminal_monotonic")
            terminal_at = self.last_observation_monotonic
        elif terminal_at < self.last_observation_monotonic:
            self.mark_censored("terminal_before_last_book")
        elif terminal_at > self.last_observation_monotonic:
            self._accrue(terminal_at)

        exposure_bins = tuple(
            RPIExposureBin(
                depth_bps=depth_bps,
                exposure_seconds=bucket.exposure_seconds,
                fill_count=bucket.fill_count,
                sample_count=bucket.sample_count,
            )
            for depth_bps, bucket in sorted(self._bins.items())
            if bucket.exposure_seconds > 0.0
        )
        if not self.censored and not exposure_bins:
            self.mark_censored("no_positive_exposure")
        if not self.censored and sum(
            item.fill_count for item in exposure_bins
        ) != self._fill_count:
            self.mark_censored("fill_without_positive_exposure")

        return RPIOrderExposureResult(
            censored=self.censored,
            censor_reason=self.censor_reason,
            acknowledged_at_monotonic=self.acknowledged_at_monotonic,
            terminal_at_monotonic=terminal_at,
            fill_count=self._fill_count,
            exposure_bins=() if self.censored else exposure_bins,
        )

    def _accrue(self, observed_at_monotonic: float) -> bool:
        if self.censored or self._current_depth_bin is None:
            self.mark_censored("exposure_without_depth")
            return False
        duration = observed_at_monotonic - self.last_observation_monotonic
        if not math.isfinite(duration) or duration <= 0.0:
            self.mark_censored("invalid_exposure_duration")
            return False

        bucket = self._bins.setdefault(
            self._current_depth_bin,
            _MutableExposureBin(),
        )
        exposure_seconds = bucket.exposure_seconds + duration
        if not math.isfinite(exposure_seconds):
            self.mark_censored("non_finite_exposure")
            return False
        bucket.exposure_seconds = exposure_seconds
        bucket.sample_count += 1
        self.last_observation_monotonic = observed_at_monotonic
        return True

    def _quantized_depth(self, value: Any) -> float | None:
        depth = _finite_float(value)
        if depth is None or depth < 0.0:
            return None
        scaled = depth / self.depth_bin_width_bps
        if not math.isfinite(scaled):
            return None
        quantized = math.floor(scaled + 0.5) * self.depth_bin_width_bps
        if not math.isfinite(quantized) or quantized < 0.0:
            return None
        return float(f"{quantized:.12g}")


class RPIIntensityAccumulator:
    """Collect ACK-to-terminal quote exposure for one symbol and one side."""

    def __init__(self) -> None:
        self._bins: dict[float, RPIExposureBin] = {}
        self._invalid_reasons: Counter[str] = Counter()

    @property
    def invalid_sample_count(self) -> int:
        return sum(self._invalid_reasons.values())

    def add_acked_interval(
        self,
        *,
        depth_bps: Any,
        acknowledged_at_seconds: Any,
        ended_at_seconds: Any,
        fill_count: Any = 0,
    ) -> bool:
        """Add exposure measured strictly after a successful venue ACK."""

        acknowledged_at = _finite_float(acknowledged_at_seconds)
        ended_at = _finite_float(ended_at_seconds)
        if (
            acknowledged_at is None
            or ended_at is None
            or acknowledged_at < 0.0
            or ended_at <= acknowledged_at
        ):
            self._invalid_reasons["invalid_ack_interval"] += 1
            return False
        return self.add_acked_exposure(
            depth_bps=depth_bps,
            exposure_seconds=ended_at - acknowledged_at,
            fill_count=fill_count,
        )

    def add_acked_exposure(
        self,
        *,
        depth_bps: Any,
        exposure_seconds: Any,
        fill_count: Any = 0,
    ) -> bool:
        """Add an already measured post-ACK exposure, including zero fills."""

        normalized, reason = _normalize_values(
            depth_bps=depth_bps,
            exposure_seconds=exposure_seconds,
            fill_count=fill_count,
            sample_count=1,
        )
        if normalized is None:
            self._invalid_reasons[reason] += 1
            return False
        return self._add_normalized_bin(normalized)

    def add_acked_bin(self, exposure_bin: RPIExposureBin) -> bool:
        """Merge a pre-aggregated, post-ACK depth bin."""

        normalized, reason = _normalize_bin(exposure_bin)
        if normalized is None:
            self._invalid_reasons[reason] += _invalid_bin_weight(exposure_bin)
            return False
        return self._add_normalized_bin(normalized)

    def _add_normalized_bin(self, normalized: RPIExposureBin) -> bool:
        previous = self._bins.get(normalized.depth_bps)
        if previous is None:
            self._bins[normalized.depth_bps] = normalized
            return True

        exposure = previous.exposure_seconds + normalized.exposure_seconds
        if not math.isfinite(exposure):
            self._invalid_reasons["non_finite_aggregate_exposure"] += 1
            return False
        self._bins[normalized.depth_bps] = RPIExposureBin(
            depth_bps=normalized.depth_bps,
            exposure_seconds=exposure,
            fill_count=previous.fill_count + normalized.fill_count,
            sample_count=previous.sample_count + normalized.sample_count,
        )
        return True

    def snapshot_bins(self) -> tuple[RPIExposureBin, ...]:
        return tuple(self._bins[key] for key in sorted(self._bins))

    def estimate(
        self,
        requirements: RPIIntensityRequirements | None = None,
    ) -> RPIIntensityEstimate:
        return estimate_rpi_intensity(
            self.snapshot_bins(),
            requirements=requirements,
            invalid_sample_count=self.invalid_sample_count,
            invalid_reasons=tuple(sorted(self._invalid_reasons)),
        )


def estimate_rpi_intensity(
    bins: Iterable[RPIExposureBin],
    *,
    requirements: RPIIntensityRequirements | None = None,
    invalid_sample_count: int = 0,
    invalid_reasons: Iterable[str] = (),
) -> RPIIntensityEstimate:
    """
    Fit aggregate Poisson exposure without discarding zero-count depth bins.

    For depth ``d_i``, exposure ``T_i`` and fills ``n_i``, the model is
    ``n_i ~ Poisson(T_i * A * exp(-k * d_i))``.  ``A`` is profiled out and
    ``k`` is solved from the monotone likelihood score using log-sum-exp
    weights, avoiding a fragile log(count / exposure) regression.
    """

    active_requirements = requirements or RPIIntensityRequirements()
    grouped: dict[float, list[RPIExposureBin]] = {}
    rejected_count = _nonnegative_int_or_zero(invalid_sample_count)
    rejection_labels = Counter(str(reason) for reason in invalid_reasons)

    try:
        source_bins = tuple(bins)
    except (TypeError, ValueError):
        source_bins = ()
        rejected_count += 1
        rejection_labels["invalid_bins_iterable"] += 1

    for raw_bin in source_bins:
        normalized, reason = _normalize_bin(raw_bin)
        if normalized is None:
            rejected_count += _invalid_bin_weight(raw_bin)
            rejection_labels[reason] += 1
            continue
        grouped.setdefault(normalized.depth_bps, []).append(normalized)

    aggregate_bins: list[RPIExposureBin] = []
    for depth_bps in sorted(grouped):
        depth_bins = grouped[depth_bps]
        try:
            exposure_seconds = math.fsum(
                item.exposure_seconds for item in depth_bins
            )
        except (OverflowError, ValueError):
            rejected_count += sum(item.sample_count for item in depth_bins)
            rejection_labels["non_finite_aggregate_exposure"] += 1
            continue
        if not math.isfinite(exposure_seconds) or exposure_seconds <= 0.0:
            rejected_count += sum(item.sample_count for item in depth_bins)
            rejection_labels["non_finite_aggregate_exposure"] += 1
            continue
        aggregate_bins.append(
            RPIExposureBin(
                depth_bps=depth_bps,
                exposure_seconds=exposure_seconds,
                fill_count=sum(item.fill_count for item in depth_bins),
                sample_count=sum(item.sample_count for item in depth_bins),
            )
        )

    normalized_bins = tuple(aggregate_bins)
    sample_count = sum(item.sample_count for item in normalized_bins)
    depth_level_count = len(normalized_bins)
    fill_count = sum(item.fill_count for item in normalized_bins)
    total_exposure_seconds = _finite_sum_or_infinity(
        item.exposure_seconds for item in normalized_bins
    )
    zero_fill_bins = tuple(
        item for item in normalized_bins if item.fill_count == 0
    )
    zero_fill_exposure_seconds = _finite_sum_or_infinity(
        item.exposure_seconds for item in zero_fill_bins
    )

    reasons = [
        f"invalid_samples:{rejected_count}"
        if not rejection_labels
        else "invalid_samples:"
        f"{rejected_count}[{','.join(sorted(rejection_labels))}]"
    ] if rejected_count else []

    total_exposure_is_invalid = not math.isfinite(total_exposure_seconds)
    if total_exposure_is_invalid:
        reasons.append("total_exposure_seconds:not_finite")
    if sample_count < active_requirements.min_sample_count:
        reasons.append(
            f"sample_count:{sample_count}"
            f"<{active_requirements.min_sample_count}"
        )
    if depth_level_count < active_requirements.min_depth_level_count:
        reasons.append(
            f"depth_level_count:{depth_level_count}"
            f"<{active_requirements.min_depth_level_count}"
        )
    if (
        math.isfinite(total_exposure_seconds)
        and total_exposure_seconds
        < active_requirements.min_total_exposure_seconds
    ):
        reasons.append(
            f"total_exposure_seconds:{total_exposure_seconds:g}"
            f"<{active_requirements.min_total_exposure_seconds:g}"
        )
    if fill_count < active_requirements.min_fill_count:
        reasons.append(
            f"fill_count:{fill_count}<{active_requirements.min_fill_count}"
        )

    depth_span = (
        normalized_bins[-1].depth_bps - normalized_bins[0].depth_bps
        if depth_level_count >= 2
        else 0.0
    )
    if depth_span < active_requirements.min_depth_span_bps:
        reasons.append(
            f"depth_span_bps:{depth_span:g}"
            f"<{active_requirements.min_depth_span_bps:g}"
        )

    if reasons:
        state = (
            "INVALID_DATA"
            if rejected_count or total_exposure_is_invalid
            else "WARMING_UP"
        )
        return _result(
            state=state,
            bins=normalized_bins,
            sample_count=sample_count,
            total_exposure_seconds=total_exposure_seconds,
            fill_count=fill_count,
            zero_fill_bins=zero_fill_bins,
            zero_fill_exposure_seconds=zero_fill_exposure_seconds,
            invalid_sample_count=rejected_count,
            reasons=reasons,
        )

    fitted, fit_reason = _fit_profile_poisson(
        normalized_bins,
        max_k_per_bps=active_requirements.max_k_per_bps,
    )
    if fitted is None:
        return _result(
            state="FIT_FAILED",
            bins=normalized_bins,
            sample_count=sample_count,
            total_exposure_seconds=total_exposure_seconds,
            fill_count=fill_count,
            zero_fill_bins=zero_fill_bins,
            zero_fill_exposure_seconds=zero_fill_exposure_seconds,
            invalid_sample_count=rejected_count,
            reasons=(fit_reason,),
        )

    A_per_s, k_per_bps, log_likelihood = fitted
    if k_per_bps < active_requirements.min_k_per_bps:
        return _result(
            state="FIT_FAILED",
            bins=normalized_bins,
            sample_count=sample_count,
            total_exposure_seconds=total_exposure_seconds,
            fill_count=fill_count,
            zero_fill_bins=zero_fill_bins,
            zero_fill_exposure_seconds=zero_fill_exposure_seconds,
            invalid_sample_count=rejected_count,
            reasons=(
                f"k_per_bps:{k_per_bps:g}"
                f"<{active_requirements.min_k_per_bps:g}",
            ),
        )

    return RPIIntensityEstimate(
        ready=True,
        state="READY",
        A_per_s=A_per_s,
        k_per_bps=k_per_bps,
        sample_count=sample_count,
        depth_level_count=depth_level_count,
        total_exposure_seconds=total_exposure_seconds,
        fill_count=fill_count,
        zero_fill_depth_level_count=len(zero_fill_bins),
        zero_fill_exposure_seconds=zero_fill_exposure_seconds,
        invalid_sample_count=0,
        log_likelihood=log_likelihood,
        reasons=(),
        bins=normalized_bins,
    )


def _fit_profile_poisson(
    bins: tuple[RPIExposureBin, ...],
    *,
    max_k_per_bps: float,
) -> tuple[tuple[float, float, float] | None, str]:
    total_fills = sum(item.fill_count for item in bins)
    reference_depth = bins[0].depth_bps
    event_depth_mean = reference_depth + math.fsum(
        (item.fill_count / total_fills)
        * (item.depth_bps - reference_depth)
        for item in bins
        if item.fill_count
    )
    depth_span = bins[-1].depth_bps - reference_depth
    score_tolerance = 1e-12 * max(1.0, depth_span)

    exposure_depth_at_zero = _exposure_weighted_depth(bins, 0.0)
    if exposure_depth_at_zero is None:
        return None, "exposure_weighting:not_finite"
    if exposure_depth_at_zero - event_depth_mean <= score_tolerance:
        return None, "intensity_slope:not_strictly_decaying"

    exposure_depth_at_limit = _exposure_weighted_depth(
        bins,
        max_k_per_bps,
    )
    if exposure_depth_at_limit is None:
        return None, "exposure_weighting:not_finite"
    if exposure_depth_at_limit - event_depth_mean >= -score_tolerance:
        return None, f"k_per_bps:unbounded_or_above_{max_k_per_bps:g}"

    lower = 0.0
    upper = max_k_per_bps
    for _ in range(160):
        midpoint = (lower + upper) / 2.0
        weighted_depth = _exposure_weighted_depth(bins, midpoint)
        if weighted_depth is None:
            return None, "exposure_weighting:not_finite"
        if weighted_depth > event_depth_mean:
            lower = midpoint
        else:
            upper = midpoint
        if upper - lower <= 1e-13 * max(1.0, midpoint):
            break
    k_per_bps = (lower + upper) / 2.0

    log_exposure_sum = _log_exposure_sum(bins, k_per_bps)
    if log_exposure_sum is None:
        return None, "exposure_sum:not_finite"
    log_A = math.log(total_fills) - log_exposure_sum
    if log_A > math.log(sys.float_info.max):
        return None, "A_per_s:overflow"
    try:
        A_per_s = math.exp(log_A)
    except OverflowError:
        return None, "A_per_s:overflow"
    if not math.isfinite(A_per_s) or A_per_s <= 0.0:
        return None, "A_per_s:not_positive_finite"

    likelihood_terms = []
    for item in bins:
        log_mean = (
            math.log(item.exposure_seconds)
            + log_A
            - k_per_bps * item.depth_bps
        )
        if not math.isfinite(log_mean) or log_mean > math.log(
            sys.float_info.max
        ):
            return None, "poisson_mean:not_finite"
        mean = 0.0 if log_mean < -745.0 else math.exp(log_mean)
        try:
            likelihood_terms.append(
                item.fill_count * log_mean
                - mean
                - math.lgamma(item.fill_count + 1)
            )
        except (OverflowError, ValueError):
            return None, "log_likelihood:not_finite"
    try:
        log_likelihood = math.fsum(likelihood_terms)
    except (OverflowError, ValueError):
        return None, "log_likelihood:not_finite"
    if not math.isfinite(log_likelihood):
        return None, "log_likelihood:not_finite"
    return (A_per_s, k_per_bps, log_likelihood), ""


def _exposure_weighted_depth(
    bins: tuple[RPIExposureBin, ...],
    k_per_bps: float,
) -> float | None:
    log_weights = [
        math.log(item.exposure_seconds) - k_per_bps * item.depth_bps
        for item in bins
    ]
    anchor = max(log_weights)
    if not math.isfinite(anchor):
        return None
    weights = [math.exp(value - anchor) for value in log_weights]
    denominator = math.fsum(weights)
    reference_depth = bins[0].depth_bps
    try:
        offset = math.fsum(
            weight * (item.depth_bps - reference_depth)
            for weight, item in zip(weights, bins, strict=True)
        )
    except (OverflowError, ValueError):
        return None
    value = reference_depth + offset / denominator
    return value if math.isfinite(value) else None


def _log_exposure_sum(
    bins: tuple[RPIExposureBin, ...],
    k_per_bps: float,
) -> float | None:
    terms = [
        math.log(item.exposure_seconds) - k_per_bps * item.depth_bps
        for item in bins
    ]
    anchor = max(terms)
    if not math.isfinite(anchor):
        return None
    weight_sum = math.fsum(math.exp(value - anchor) for value in terms)
    result = anchor + math.log(weight_sum)
    return result if math.isfinite(result) else None


def _normalize_bin(
    value: Any,
) -> tuple[RPIExposureBin | None, str]:
    if not isinstance(value, RPIExposureBin):
        return None, "invalid_bin_type"
    return _normalize_values(
        depth_bps=value.depth_bps,
        exposure_seconds=value.exposure_seconds,
        fill_count=value.fill_count,
        sample_count=value.sample_count,
    )


def _normalize_values(
    *,
    depth_bps: Any,
    exposure_seconds: Any,
    fill_count: Any,
    sample_count: Any,
) -> tuple[RPIExposureBin | None, str]:
    depth = _finite_float(depth_bps)
    if depth is None or depth < 0.0:
        return None, "invalid_depth_bps"
    exposure = _finite_float(exposure_seconds)
    if exposure is None or exposure <= 0.0:
        return None, "invalid_exposure_seconds"
    fills = _nonnegative_int(fill_count)
    if fills is None:
        return None, "invalid_fill_count"
    samples = _positive_int(sample_count)
    if samples is None:
        return None, "invalid_sample_count"
    return (
        RPIExposureBin(
            depth_bps=depth,
            exposure_seconds=exposure,
            fill_count=fills,
            sample_count=samples,
        ),
        "",
    )


def _invalid_bin_weight(value: Any) -> int:
    if isinstance(value, RPIExposureBin):
        samples = _positive_int(value.sample_count)
        if samples is not None:
            return samples
    return 1


def _result(
    *,
    state: str,
    bins: tuple[RPIExposureBin, ...],
    sample_count: int,
    total_exposure_seconds: float,
    fill_count: int,
    zero_fill_bins: tuple[RPIExposureBin, ...],
    zero_fill_exposure_seconds: float,
    invalid_sample_count: int,
    reasons: Iterable[str],
) -> RPIIntensityEstimate:
    return RPIIntensityEstimate(
        ready=False,
        state=state,
        A_per_s=None,
        k_per_bps=None,
        sample_count=sample_count,
        depth_level_count=len(bins),
        total_exposure_seconds=total_exposure_seconds,
        fill_count=fill_count,
        zero_fill_depth_level_count=len(zero_fill_bins),
        zero_fill_exposure_seconds=zero_fill_exposure_seconds,
        invalid_sample_count=invalid_sample_count,
        log_likelihood=None,
        reasons=tuple(reasons),
        bins=bins,
    )


def _finite_sum_or_infinity(values: Iterable[float]) -> float:
    try:
        result = math.fsum(values)
    except (OverflowError, ValueError):
        return math.inf
    return result if math.isfinite(result) else math.inf


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed != value:
        return None
    return parsed


def _positive_int(value: Any) -> int | None:
    parsed = _nonnegative_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _nonnegative_int_or_zero(value: Any) -> int:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None else 1


def _require_positive_int(value: Any, field: str) -> int:
    parsed = _positive_int(value)
    if parsed is None:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _require_positive_finite(value: Any, field: str) -> float:
    parsed = _finite_float(value)
    if parsed is None or parsed <= 0.0:
        raise ValueError(f"{field} must be positive and finite")
    return parsed


def _require_nonnegative_finite(value: Any, field: str) -> float:
    parsed = _finite_float(value)
    if parsed is None or parsed < 0.0:
        raise ValueError(f"{field} must be non-negative and finite")
    return parsed


__all__ = [
    "RPIExposureBin",
    "RPIIntensityAccumulator",
    "RPIIntensityEstimate",
    "RPIIntensityRequirements",
    "RPIOrderExposure",
    "RPIOrderExposureResult",
    "estimate_rpi_intensity",
]
