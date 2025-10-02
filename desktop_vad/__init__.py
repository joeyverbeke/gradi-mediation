"""Desktop voice activity detection utilities."""

from .webrtc_vad_processor import VADConfig, WebRTCVADProcessor, detect_voiced_segments

__all__ = [
    "VADConfig",
    "WebRTCVADProcessor",
    "detect_voiced_segments",
]
