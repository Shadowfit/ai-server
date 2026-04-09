"""Build a reference squat sequence from a multi-rep guide video."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.core.video_processor import analyze_video
from app.models.video import FrameResult, VideoAnalysisResult


@dataclass
class RepSegment:
    """One detected squat repetition segment from a guide video."""

    rep_index: int
    start_frame_index: int
    end_frame_index: int
    angles: list[list[float]]
    knee_angles: list[float]
    torso_tilts: list[float]
    min_knee_angle: float
    mean_torso_tilt: float
    frame_count: int
    score: float


def _resample_series(values: list[float], target_length: int) -> list[float]:
    if not values:
        return [0.0] * target_length
    if len(values) == 1:
        return [round(values[0], 4)] * target_length

    original_axis = np.linspace(0.0, 1.0, num=len(values))
    target_axis = np.linspace(0.0, 1.0, num=target_length)
    return [round(float(value), 4) for value in np.interp(target_axis, original_axis, values)]


def _resample_angle_sequence(
    sequence: list[list[float]], target_length: int
) -> list[list[float]]:
    if not sequence:
        return []

    num_angles = len(sequence[0])
    columns = []
    for angle_index in range(num_angles):
        column = [frame_angles[angle_index] for frame_angles in sequence]
        columns.append(_resample_series(column, target_length))

    normalized: list[list[float]] = []
    for frame_index in range(target_length):
        normalized.append([columns[col][frame_index] for col in range(num_angles)])
    return normalized


def _average_sequences(sequences: list[list[list[float]]]) -> list[list[float]]:
    if not sequences:
        return []

    array = np.array(sequences, dtype=np.float64)
    mean_array = np.mean(array, axis=0)
    return np.round(mean_array, 4).tolist()


def _segment_reps(frames: list[FrameResult]) -> list[RepSegment]:
    rep_segments: list[RepSegment] = []
    current_frames: list[FrameResult] = []
    active = False
    seen_bottom = False
    rep_index = 0

    for frame in frames:
        metrics = frame.squat_metrics
        if metrics is None:
            continue

        knee_angle = metrics.knee_angle
        if not active and knee_angle < 160:
            active = True
            current_frames = [frame]
            seen_bottom = knee_angle <= 100
            continue

        if active:
            current_frames.append(frame)
            if knee_angle <= 100:
                seen_bottom = True

            if seen_bottom and knee_angle >= 155:
                rep_index += 1
                angles = [item.angles for item in current_frames]
                knee_angles = [item.squat_metrics.knee_angle for item in current_frames if item.squat_metrics]
                torso_tilts = [item.squat_metrics.torso_tilt for item in current_frames if item.squat_metrics]
                min_knee = min(knee_angles) if knee_angles else 180.0
                mean_torso = sum(torso_tilts) / len(torso_tilts) if torso_tilts else 0.0
                depth_score = max(0.0, 120.0 - min_knee)
                torso_score = max(0.0, 35.0 - mean_torso)
                stability_score = min(len(current_frames), 40)
                score = round(depth_score * 2.0 + torso_score + stability_score, 2)

                rep_segments.append(
                    RepSegment(
                        rep_index=rep_index,
                        start_frame_index=current_frames[0].frame_index,
                        end_frame_index=current_frames[-1].frame_index,
                        angles=angles,
                        knee_angles=knee_angles,
                        torso_tilts=torso_tilts,
                        min_knee_angle=round(min_knee, 2),
                        mean_torso_tilt=round(mean_torso, 2),
                        frame_count=len(current_frames),
                        score=score,
                    )
                )
                active = False
                seen_bottom = False
                current_frames = []

    return rep_segments


def build_reference_sequence(
    video_path: str,
    *,
    target_length: int = 30,
    max_reps: int = 5,
) -> dict:
    """Create a representative squat reference pattern from a guide video."""
    result: VideoAnalysisResult = analyze_video(video_path, "squat")
    rep_segments = _segment_reps(result.frames)

    if not rep_segments:
        raise ValueError("기준 영상에서 유효한 스쿼트 반복을 찾지 못했습니다.")

    sorted_segments = sorted(rep_segments, key=lambda rep: rep.score, reverse=True)
    selected_segments = sorted_segments[:max_reps]

    normalized_sequences = [
        _resample_angle_sequence(rep.angles, target_length) for rep in selected_segments
    ]
    representative_sequence = _average_sequences(normalized_sequences)

    return {
        "exercise_type": "squat",
        "source_video": video_path,
        "analysis_fps": result.fps,
        "target_length": target_length,
        "total_reps_detected": len(rep_segments),
        "selected_rep_count": len(selected_segments),
        "selected_reps": [
            {
                "rep_index": rep.rep_index,
                "score": rep.score,
                "frame_count": rep.frame_count,
                "start_frame_index": rep.start_frame_index,
                "end_frame_index": rep.end_frame_index,
                "min_knee_angle": rep.min_knee_angle,
                "mean_torso_tilt": rep.mean_torso_tilt,
            }
            for rep in selected_segments
        ],
        "representative_sequence": representative_sequence,
    }
