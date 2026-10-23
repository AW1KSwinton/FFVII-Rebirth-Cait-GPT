"""Offline timing model for the recorded first sprint braking trace."""
from __future__ import annotations

TRACE = (
    (8057.6610283, 0.87176847898),
    (8058.2398766, 0.55325077185),
    (8059.6380622, 0.0),
    (8059.7554641, 0.0),
)
START = 8057.6375557
MIN_BRAKE_SECONDS = 0.15
HANDOFF_MAX_SPEED_M_S = 0.60

def main() -> None:
    baseline_release = TRACE[-1][0]
    handoff_release = next(t for t, speed in TRACE if t - START >= MIN_BRAKE_SECONDS and speed <= HANDOFF_MAX_SPEED_M_S)
    baseline = baseline_release - START
    optimized = handoff_release - START
    saved = baseline - optimized
    print(f"baseline_safety_pause_s={baseline:.6f}")
    print(f"low_speed_handoff_release_s={optimized:.6f}")
    print(f"recorded_trace_pause_saved_s={saved:.6f}")
    print(f"recorded_trace_pause_reduction_pct={saved / baseline * 100.0:.2f}")
    assert 1.50 < saved < 1.53

if __name__ == "__main__":
    main()
