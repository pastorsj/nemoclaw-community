# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Voice input: NVIDIA Nemotron ASR on build.nvidia.com, with an optional deletion-only cleanup."""

from .service import SpeechService
from .service import SpeechSettings
from .service import build_speech_service

__all__ = ["SpeechService", "SpeechSettings", "build_speech_service"]
