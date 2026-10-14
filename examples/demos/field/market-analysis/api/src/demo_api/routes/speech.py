# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""``POST /v1/speech/transcriptions``: one WAV recording in, its transcript out (``demo_api/speech``)."""

from __future__ import annotations

import io
import wave
from typing import NoReturn

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from demo_api.services import ServicesDep
from demo_api.speech.service import MAX_AUDIO_BYTES
from demo_api.speech.service import MAX_TRANSCRIPT_CHARACTERS
from demo_api.speech.service import SpeechBusyError
from demo_api.speech.service import SpeechNotRecognizedError
from demo_api.speech.service import SpeechProviderError
from demo_api.speech.service import SpeechProviderTimeoutError
from demo_api.speech.service import SpeechUnavailableError

router = APIRouter(prefix="/v1/speech", tags=["speech"])


class Transcription(BaseModel):
    """The only provider-derived value the browser receives."""

    text: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARACTERS)


@router.post("/transcriptions")
async def transcribe(request: Request, services: ServicesDep) -> Transcription:
    """Transcribe 16 kHz mono PCM16 WAV (``Content-Type: audio/wav``), at most ``SPEECH_INPUT_MAX_SECONDS`` long."""
    speech = services.speech
    if not speech.settings.available:
        _fail(503, "Voice input is not enabled.")
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "audio/wav":
        _fail(415, "Voice input requires a WAV recording.")
    pcm = _pcm(await _body(request), max_seconds=speech.settings.max_seconds)
    try:
        return Transcription(text=await speech.transcribe(pcm))
    except SpeechUnavailableError as error:
        _fail(503, str(error))
    except SpeechBusyError as error:
        _fail(429, str(error))
    except SpeechProviderTimeoutError as error:
        _fail(504, str(error))
    except SpeechNotRecognizedError as error:
        _fail(422, str(error))
    except SpeechProviderError as error:
        _fail(502, str(error))


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_AUDIO_BYTES):
        _fail(413, "The recording is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_AUDIO_BYTES:
            _fail(413, "The recording is too large.")
    if not body:
        _fail(422, "The recording is empty.")
    return bytes(body)


def _pcm(audio: bytes, *, max_seconds: int) -> bytes:
    """The PCM frames of a 16 kHz, mono, 16-bit WAV no longer than ``max_seconds``."""
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        _fail(422, "The recording is not a WAV file.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as recording:
            shape = (recording.getnchannels(), recording.getsampwidth(), recording.getframerate())
            if shape != (1, 2, 16_000) or recording.getcomptype() != "NONE":
                _fail(422, "The recording must be 16-bit mono audio at 16 kHz.")
            frames = recording.getnframes()
            if frames < 1:
                _fail(422, "The recording is empty.")
            if frames > max_seconds * 16_000:
                _fail(413, f"Voice input is limited to {max_seconds} seconds.")
            pcm = recording.readframes(frames)
    except (EOFError, wave.Error):
        _fail(422, "The recording is not a valid WAV file.")
    if len(pcm) != frames * 2:
        _fail(422, "The recording is incomplete.")
    return pcm


def _fail(status_code: int, message: str) -> NoReturn:
    raise HTTPException(status_code, message)
