"""Controller-policy boundary for real live route execution.

The standalone route simulator intentionally keeps its historical defaults for
backward-compatible replay.  Real movement has a separately proven envelope;
this module makes that envelope explicit and fail-closed at the runtime
command/async-start boundary.
"""

from __future__ import annotations

import math
import os
from typing import Any, Mapping

from .continuous_control import CONTINUOUS_PROFILE, FAST_FOLLOW_PROFILE, SPRINT_PROFILE, SPRINT_SPEED_BOUND_M_S, MAX_SUPERVISED_LEASE_SECONDS


PROVEN_LIVE_CONTROLLER_POLICY: dict[str, float] = {
    "normal_stick_magnitude": 1.0,
    "control_period_seconds": 0.3,
    "approach_stick_magnitude": 0.5,
    "approach_control_period_seconds": 0.25,
    "final_stick_magnitude": 0.5,
    "final_control_period_seconds": 0.25,
    "minimum_effective_stick_magnitude": 0.5,
    "final_arrival_radius_m": 0.75,
    # The follower is a separately loaded, pinned simulator module.  This is
    # recorded here as part of the accepted live envelope, not passed as an
    # unused adapter argument.
    "stall_timeout_s": 5.0,
}

# These are the values that must cross the live route command boundary.  The
# follower's stall timeout is intentionally not an adapter constructor option;
# runtime evidence verifies the loaded follower's value separately.
LIVE_CONTROLLER_POLICY_INPUT_FIELDS = (
    "normal_stick_magnitude",
    "control_period_seconds",
    "approach_stick_magnitude",
    "approach_control_period_seconds",
    "final_stick_magnitude",
    "final_control_period_seconds",
    "minimum_effective_stick_magnitude",
    "final_arrival_radius_m",
)


NORMAL_06_TRIAL_PROFILE = "normal_0_6_trial"
NORMAL_08_TRIAL_PROFILE = "normal_0_8_trial"

_NORMAL_TRIAL_MAGNITUDES = {
    NORMAL_06_TRIAL_PROFILE: 0.6,
    NORMAL_08_TRIAL_PROFILE: 0.8,
    CONTINUOUS_PROFILE: 0.8,
    SPRINT_PROFILE: 0.8,
    # Same magnitude as the proven pulse default. Held full-stick travel
    # (and pursuit on top of it) is live-validated: see below.
    FAST_FOLLOW_PROFILE: 1.0,
}
# Fast follow carries streamed pursuit, the default for navigating to
# targets since 2026-10-05 (Chadley 40.7 s, the Gold Saucer and Crow's Nest
# tour), so it no longer needs the feature-validation runtime.
_ACCEPTED_CONTINUOUS_PROFILES = frozenset({FAST_FOLLOW_PROFILE})
_CONTINUOUS_PROFILES = frozenset({CONTINUOUS_PROFILE, SPRINT_PROFILE, FAST_FOLLOW_PROFILE})
_NORMAL_PROFILE_BY_MAGNITUDE = {
    0.6: NORMAL_06_TRIAL_PROFILE,
    0.8: NORMAL_08_TRIAL_PROFILE,
}


class LiveControllerPolicyError(ValueError):
    """A real live route did not provide the proven controller envelope."""

    def __init__(self, code: str, message: str, *, fields: list[str] | None = None) -> None:
        self.code = code
        self.fields = list(fields or [])
        super().__init__(message)


def require_proven_live_controller_policy(values: Mapping[str, Any]) -> dict[str, float]:
    """Require the baseline policy or an explicit feature-only intensity trial.

    Generic simulator/test construction may continue to use
    ``LiveAdapterConfig`` directly.  This function is only for a non-dry-run
    route crossing the runtime boundary, where silently inheriting the legacy
    0.35/100 ms normal envelope is unsafe.
    """

    missing = [field for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS if field not in values]
    if missing:
        raise LiveControllerPolicyError(
            "live_controller_policy_required",
            "real non-dry-run live routes require the explicit proven controller policy; "
            "missing: " + ", ".join(missing),
            fields=missing,
        )

    expected_policy = dict(PROVEN_LIVE_CONTROLLER_POLICY)
    profile = values.get("controller_policy_profile", "proven")
    mode = values.get("controller_mode", "pulse")
    expected_mode = "continuous" if profile in _CONTINUOUS_PROFILES else "pulse"
    sprint = values.get("sprint", False)
    if type(sprint) is not bool or sprint != (profile == SPRINT_PROFILE):
        raise LiveControllerPolicyError("live_sprint_profile_mismatch",
            "RT sprint requires its explicit continuous_sprint_trial profile")
    fast_follow = values.get("continuous_fast_follow", False)
    if type(fast_follow) is not bool or fast_follow != (profile == FAST_FOLLOW_PROFILE):
        raise LiveControllerPolicyError("live_fast_follow_profile_mismatch",
            "fast follow requires its explicit continuous_fast_follow_trial profile")
    if mode != expected_mode:
        raise LiveControllerPolicyError("live_controller_mode_profile_mismatch",
            "continuous movement requires its explicit continuous_0_8_trial profile")
    trial_magnitude = _NORMAL_TRIAL_MAGNITUDES.get(profile)
    if trial_magnitude is not None:
        if (profile not in _ACCEPTED_CONTINUOUS_PROFILES
                and os.getenv("FF7R_RUNTIME_PROFILE") != "FEATURE_VALIDATION"):
            raise LiveControllerPolicyError(
                "live_controller_trial_requires_feature_runtime",
                f"the {trial_magnitude:g} controller trial requires the explicit feature-validation runtime",
            )
        expected_policy["normal_stick_magnitude"] = trial_magnitude
    elif profile != "proven":
        raise LiveControllerPolicyError("live_controller_policy_profile_unknown",
            f"unknown controller policy profile: {profile!r}")

    mismatches: list[str] = []
    for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS:
        expected = expected_policy[field]
        try:
            actual = float(values[field])
        except (TypeError, ValueError):
            mismatches.append(f"{field}={values[field]!r} (expected {expected!r})")
            continue
        if not math.isfinite(actual) or actual != expected:
            mismatches.append(f"{field}={values[field]!r} (expected {expected!r})")
    if mismatches:
        raise LiveControllerPolicyError(
            "live_controller_policy_mismatch",
            "real non-dry-run live route must use the proven controller policy; "
            + "; ".join(mismatches),
            fields=mismatches,
        )
    return {
        field: expected_policy[field]
        for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS
    }


def live_controller_policy_evidence(
    *,
    adapter_config: Any,
    follower_config: Any = None,
) -> dict[str, Any]:
    """Return stable evidence for the policy actually used by a route."""

    result = {
        field: getattr(adapter_config, field)
        for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS
    }
    result.update({
        "controller_mode": getattr(adapter_config, "controller_mode", "pulse"),
        "maximum_supervised_lease_seconds": MAX_SUPERVISED_LEASE_SECONDS if getattr(adapter_config, "controller_mode", "pulse") == "continuous" else None,
        "sprint": getattr(adapter_config, "sprint", False),
        "sprint_binding": "held_rt" if getattr(adapter_config, "sprint", False) else None,
        "sprint_stick_magnitude": 1.0 if getattr(adapter_config, "sprint", False) else None,
        "speed_bound_m_s": SPRINT_SPEED_BOUND_M_S if getattr(adapter_config, "sprint", False) else 8.0,
        "continuous_fast_follow": getattr(adapter_config, "continuous_fast_follow", False),
        "controller_policy_profile": SPRINT_PROFILE if getattr(adapter_config, "sprint", False) else FAST_FOLLOW_PROFILE if getattr(adapter_config, "continuous_fast_follow", False) else CONTINUOUS_PROFILE if getattr(adapter_config, "controller_mode", "pulse") == "continuous" else _NORMAL_PROFILE_BY_MAGNITUDE.get(result["normal_stick_magnitude"], "proven"),
        "validation_status": "experimental" if result["normal_stick_magnitude"] in (0.6, 0.8) else "live_validated" if getattr(adapter_config, "continuous_fast_follow", False) else "baseline",
        "stall_timeout_s": getattr(follower_config, "stall_timeout_s", None),
        "legacy_slow_stick_magnitude": getattr(adapter_config, "slow_stick_magnitude", None),
        "effective_normal_duration_ms": int(round(float(adapter_config.control_period_seconds) * 1000.0)),
        "effective_approach_duration_ms": int(round(float(adapter_config.approach_control_period_seconds) * 1000.0)),
        "effective_final_duration_ms": int(round(float(adapter_config.final_control_period_seconds) * 1000.0)),
        "follower_stall_timeout_s": getattr(follower_config, "stall_timeout_s", None),
        "follower_final_arrival_radius_m": getattr(follower_config, "final_arrival_radius_m", None),
    })
    return result


def proven_live_controller_policy_evidence(values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return pre-start evidence for the actual accepted policy."""
    selected = require_proven_live_controller_policy(values) if values is not None else PROVEN_LIVE_CONTROLLER_POLICY
    fast_follow = bool(values.get("continuous_fast_follow", False)) if values else False
    trial = selected["normal_stick_magnitude"] in (0.6, 0.8)
    return {
        **PROVEN_LIVE_CONTROLLER_POLICY,
        **selected,
        "controller_mode": values.get("controller_mode", "pulse") if values else "pulse",
        "sprint": values.get("sprint", False) if values else False,
        "sprint_binding": "held_rt" if values and values.get("sprint") else None,
        "continuous_fast_follow": fast_follow,
        "controller_policy_profile": values.get("controller_policy_profile", "proven") if values else "proven",
        "validation_status": "experimental" if trial else "live_validated" if fast_follow else "baseline",
        "legacy_slow_stick_magnitude": 0.2,
        "effective_normal_duration_ms": 300,
        "effective_approach_duration_ms": 250,
        "effective_final_duration_ms": 250,
    }
