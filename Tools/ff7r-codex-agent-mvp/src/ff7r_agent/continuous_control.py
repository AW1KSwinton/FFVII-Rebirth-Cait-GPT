"""Short movement leases renewed only by advancing, validated control samples."""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_SUPERVISED_LEASE_SECONDS = 0.5
CONTINUOUS_PROFILE = "continuous_0_8_trial"
SPRINT_PROFILE = "continuous_sprint_trial"
# Opt-in full-stick (1.0) continuous travel for cached-topology routes. It keeps
# every continuous-mode lease/corridor/freshness bound, additionally proves the
# measured-momentum ray, and may hold normal travel through an intermediate
# waypoint's follower slow band only when that exact proof passes.
FAST_FOLLOW_PROFILE = "continuous_fast_follow_trial"
FAST_FOLLOW_MAX_HEADING_ERROR_DEG = 45.0
# Camera-follow is presentation-only; every material camera command must
# release held movement and wait for yaw settling (live 0.6-2.7 s for a 5-9
# degree correction). Fast follow therefore only re-aims a larger error.
FAST_FOLLOW_CAMERA_DEADBAND_DEG = 30.0
SPRINT_SPEED_BOUND_M_S = 12.0
SPRINT_BRAKING_MARGIN_M = 1.5
SPRINT_SETTLE_TIMEOUT_SECONDS = 5.0


@dataclass
class SupervisedStickLease:
    stream_id: str | None = None
    sequence: int = -1
    deadline: float | None = None

    def accept(self, *, stream_id, sequence, deadline, now):
        if not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 128:
            raise ValueError("supervised_stream_identity_invalid")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("supervised_sample_sequence_invalid")
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or not 0 < deadline - now <= MAX_SUPERVISED_LEASE_SECONDS):
            raise ValueError("supervised_deadline_invalid_or_expired")
        if stream_id == self.stream_id and sequence <= self.sequence:
            raise ValueError("supervised_sample_not_advancing")
        if self.deadline is not None and now < self.deadline and stream_id != self.stream_id:
            raise ValueError("supervised_stream_changed_while_active")
        self.stream_id, self.sequence, self.deadline = stream_id, sequence, deadline

    def expired(self, now):
        return self.deadline is not None and now >= self.deadline

    def cancel(self):
        # Keep the high-water mark: a neutral report cannot make an old sample
        # valid again. A new controller instance supplies a new stream ID.
        self.deadline = None


def movement_lease_seconds(*, effective_age, freshness_limit, distance_to_turn_m,
                           speed_bound_m_s, braking_margin_m=0.75):
    values = (effective_age, freshness_limit, distance_to_turn_m, speed_bound_m_s, braking_margin_m)
    if not all(math.isfinite(v) for v in values) or effective_age < 0 or speed_bound_m_s <= 0:
        return 0.0
    return max(0.0, min(0.4, freshness_limit - effective_age - 0.025,
        (distance_to_turn_m - braking_margin_m) / speed_bound_m_s - effective_age))
