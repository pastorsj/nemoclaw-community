# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Voice input: bounded, server-side transcription of one short recording.

The browser records 16 kHz mono PCM16 WAV and posts it to ``POST /v1/speech/transcriptions``
(through the UI's proxy). The API sends the audio to NVIDIA Nemotron ASR on build.nvidia.com
(``nemotron.py``) and returns only the transcript. An optional cleanup (``cleanup.py``) may then
delete fillers and false starts with a public model on build.nvidia.com; it never adds or changes
words, and any failure keeps the raw transcript.

Off unless ``SPEECH_INPUT_ENABLED`` is true and a key is set (``SPEECH_API_KEY``, an nvapi- key).
At most ``SPEECH_MAX_CONCURRENT`` transcriptions run at once; a request that waits longer than a
second for a slot is told to retry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from typing import Protocol

from demo_api.settings import Settings

MAX_AUDIO_BYTES = 3 * 1024 * 1024
MAX_TRANSCRIPT_CHARACTERS = 12_000
PROVIDER_TIMEOUT_GRACE_SECONDS = 5.0
QUEUE_TIMEOUT_SECONDS = 1.0


class SpeechError(Exception):
    """A display-safe speech failure."""


class SpeechUnavailableError(SpeechError):
    """Voice input is off, or the provider refused the configuration."""


class SpeechBusyError(SpeechError):
    """Every transcription slot is taken."""


class SpeechProviderError(SpeechError):
    """The provider failed without a transcript."""


class SpeechProviderTimeoutError(SpeechProviderError):
    """The provider exceeded its deadline."""


class SpeechNotRecognizedError(SpeechError):
    """The provider recognized no speech."""


class Transcriber(Protocol):
    def transcribe(self, pcm_audio: bytes) -> str:
        """The transcript of validated 16 kHz mono PCM16 audio. Blocking."""


class TranscriptCleaner(Protocol):
    timeout_seconds: float

    def clean(self, transcript: str) -> str:
        """A conservative cleanup, or the transcript unchanged. Blocking; never raises."""


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    server: str = "grpc.nvcf.nvidia.com:443"
    function_id: str = "bb0837de-8c7b-481f-9ec8-ef5663e9c1fa"
    language_code: str = "en-US"
    max_seconds: int = 60
    timeout_seconds: float = 60.0
    max_concurrent: int = 2
    cleanup_model: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> SpeechSettings:
        return cls(
            enabled=settings.speech_input_enabled,
            api_key=settings.speech_api_key.get_secret_value().strip(),
            server=settings.speech_asr_server,
            function_id=settings.speech_asr_function_id,
            language_code=settings.speech_asr_language,
            max_seconds=settings.speech_input_max_seconds,
            timeout_seconds=settings.speech_asr_timeout_seconds,
            max_concurrent=settings.speech_max_concurrent,
            cleanup_model=settings.speech_cleanup_model.strip(),
        )

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)


class SpeechService:
    """Run the blocking provider on a thread, with bounded concurrency and a deadline."""

    def __init__(
        self,
        settings: SpeechSettings,
        transcriber: Transcriber | None = None,
        cleaner: TranscriptCleaner | None = None,
    ) -> None:
        self.settings = settings
        self._transcriber = transcriber
        self._cleaner = cleaner
        self._slots = asyncio.Semaphore(settings.max_concurrent)
        self._workers: set[asyncio.Task[str]] = set()

    async def transcribe(self, pcm_audio: bytes) -> str:
        if not self.settings.available or self._transcriber is None:
            raise SpeechUnavailableError("Voice input is not enabled")
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise SpeechBusyError("Voice transcription is busy; please try again") from error

        # The worker owns its slot until the provider call returns, even if the request is cancelled,
        # so a disconnected browser cannot start another stream while the first is still open.
        worker = asyncio.create_task(asyncio.to_thread(self._transcribe_and_clean, pcm_audio))
        self._workers.add(worker)
        worker.add_done_callback(self._finish)
        deadline = self.settings.timeout_seconds + PROVIDER_TIMEOUT_GRACE_SECONDS
        if self._cleaner is not None:
            deadline += self._cleaner.timeout_seconds
        try:
            transcript = await asyncio.wait_for(asyncio.shield(worker), timeout=deadline)
        except TimeoutError as error:
            raise SpeechProviderTimeoutError("Voice transcription timed out; please try again") from error
        except SpeechError:
            raise
        except Exception as error:
            raise SpeechProviderError("Voice transcription is unavailable right now; please try again") from error
        return transcript

    def _transcribe_and_clean(self, pcm_audio: bytes) -> str:
        assert self._transcriber is not None
        transcript = " ".join(str(self._transcriber.transcribe(pcm_audio)).split())
        if not transcript:
            raise SpeechNotRecognizedError("No speech was recognized; please try again")
        if len(transcript) > MAX_TRANSCRIPT_CHARACTERS:
            raise SpeechProviderError("Voice transcription returned an invalid response")
        return self._cleaner.clean(transcript) if self._cleaner is not None else transcript

    def _finish(self, worker: asyncio.Task[str]) -> None:
        self._workers.discard(worker)
        self._slots.release()
        if not worker.cancelled():
            worker.exception()  # retrieved, so a detached failure is not reported as unhandled


def build_speech_service(settings: Settings) -> SpeechService:
    """The configured service; the gRPC client is imported only when voice input is on."""
    speech = SpeechSettings.from_settings(settings)
    if not speech.available:
        return SpeechService(speech)
    from .cleanup import TranscriptCleanup
    from .nemotron import NemotronTranscriber

    cleaner = TranscriptCleanup(speech.api_key, speech.cleanup_model) if speech.cleanup_model else None
    return SpeechService(speech, NemotronTranscriber(speech), cleaner)
