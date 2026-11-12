# Cognetra 3.0 — System Master Configuration Reference
# DO NOT EDIT — this is a documentation-only file

SYSTEM_NAME = "Zero-Shot Learning-Based Intelligent Multi-Domain Object Recognition and Decision Support Platform"
VERSION = "3.0.0"
CODENAME = "IRIS"

# 6-Algorithm Pipeline
PIPELINE = [
    "YOLO-World (Zero-Shot Object Detection)",
    "ByteTrack (Multi-Object Tracking + Velocity Vectors)",
    "CNN/ResNet (Appearance Feature Extraction)",
    "LSTM (Temporal Kinetic Pattern Recognition)",
    "CLIP (Semantic Scene Understanding)",
    "Rule-Based Decision Fusion (Confidence Aggregation)",
]

# 10-Second Buffer (Core IP)
CLIP_1_WINDOW = "T-5s to T+5s (impact interval)"
CLIP_2_WINDOW = "T+31s to T+41s (aftermath/status)"
CLIP_1_PREDICTIVE_WINDOW = "T-7s to T+3s (prioritize dangerous lead-up)"

# Predictive Layers
TTC_THRESHOLD = 1.5  # seconds
PANIC_BRAKE_DROP = 0.60  # 60% speed drop
PANIC_BRAKE_WINDOW = 0.5  # seconds

# Fail-Open Policy
FAIL_OPEN = True  # dispatch if Gemini API fails

# Trigger Types
TRIGGER_TYPES = {
    "MOTION_TRIGGER": "High-energy pixel diff spike (classic impact)",
    "PREDICTIVE_TRIGGER": "TTC <= 1.5s — pre-impact prediction",
    "NEAR_MISS_TRIGGER": "Panic braking >60% speed drop in <0.5s",
}
