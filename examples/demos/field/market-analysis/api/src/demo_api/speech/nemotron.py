# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVIDIA Nemotron ASR on build.nvidia.com, through the Riva Python client.

The hosted function (``SPEECH_ASR_FUNCTION_ID``) serves the English
``nvidia/nemotron-speech-streaming-en-0.6b`` model at ``grpc.nvcf.nvidia.com:443``; the key is an
nvapi- key. The audio is streamed in 100 ms chunks and only final results are kept.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

import grpc
import riva.client
from riva.client.asr import streaming_request_generator

from .service import SpeechBusyError
from .service import SpeechProviderError
from .service import SpeechProviderTimeoutError
from .service import SpeechSettings
from .service import SpeechUnavailableError

CHUNK_BYTES = 3_200  # 100 ms of 16 kHz mono PCM16


class NemotronTranscriber:
    def __init__(
        self,
        settings: SpeechSettings,
        *,
        client: Any = riva.client,
        request_factory: Callable[[Iterable[bytes], Any], Iterable[Any]] = streaming_request_generator,
    ) -> None:
        self._settings = settings
        self._client = client
        self._request_factory = request_factory

    def transcribe(self, pcm_audio: bytes) -> str:
        """Stream one validated PCM buffer and return its final transcript."""
        settings = self._settings
        try:
            auth = self._client.Auth(
                uri=settings.server,
                use_ssl=True,
                metadata_args=[["function-id", settings.function_id], ["authorization", f"Bearer {settings.api_key}"]],
            )
            config = self._client.StreamingRecognitionConfig(
                config=self._client.RecognitionConfig(
                    encoding=self._client.AudioEncoding.LINEAR_PCM,
                    sample_rate_hertz=16_000,
                    language_code=settings.language_code,
                    max_alternatives=1,
                    profanity_filter=False,
                    enable_automatic_punctuation=True,
                    audio_channel_count=1,
                ),
                interim_results=False,
            )
            service = self._client.ASRService(auth)
            responses = service.stub.StreamingRecognize(
                self._request_factory(_chunks(pcm_audio), config),
                metadata=auth.get_auth_metadata(),
                timeout=settings.timeout_seconds,
            )
            try:
                parts = [
                    result.alternatives[0].transcript
                    for response in responses
                    for result in response.results
                    if result.is_final and result.alternatives
                ]
            finally:
                if callable(cancel := getattr(responses, "cancel", None)):
                    cancel()
        except grpc.RpcError as error:
            _raise_safe(error)
        except (SpeechUnavailableError, SpeechBusyError, SpeechProviderError):
            raise
        except Exception as error:
            raise SpeechProviderError("Voice transcription is unavailable right now; please try again") from error
        return " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def _chunks(pcm_audio: bytes) -> Iterable[bytes]:
    for offset in range(0, len(pcm_audio), CHUNK_BYTES):
        yield pcm_audio[offset : offset + CHUNK_BYTES]


def _raise_safe(error: grpc.RpcError) -> None:
    """A display-safe error for a gRPC status; the provider's own message never reaches the browser."""
    code = error.code() if callable(getattr(error, "code", None)) else None
    if code in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
        raise SpeechUnavailableError("Voice transcription is not authorized; check SPEECH_API_KEY") from error
    if code in {grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION}:
        raise SpeechUnavailableError("Voice transcription is not configured correctly") from error
    if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
        raise SpeechBusyError("Voice transcription is busy; please try again") from error
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        raise SpeechProviderTimeoutError("Voice transcription timed out; please try again") from error
    raise SpeechProviderError("Voice transcription is unavailable right now; please try again") from error
