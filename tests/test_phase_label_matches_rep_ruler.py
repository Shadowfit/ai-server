"""The offline phase label and repetition counter must describe the same cycle."""

from app.core.squat_analyzer import analyze_squat_frames
from tests.test_squat_analyzer import _frame


def test_phase_labels_match_counting_thresholds():
    angles = [175] * 3 + [145, 120, 90, 85, 90, 120, 145] + [175] * 4
    metrics, summary = analyze_squat_frames([_frame(angle) for angle in angles], fps=3)

    assert summary.reps_detected == 1
    assert metrics[-1].rep_count == 1
    assert any(metric.phase == "bottom" for metric in metrics)
    for metric in metrics:
        if metric.knee_angle <= 100 and metric.hip_angle <= 150:
            assert metric.phase == "bottom"
        if metric.knee_angle >= 160 and metric.hip_angle >= 160:
            assert metric.phase == "standing"
