"""Real non-trickle WebRTC video sourced from the DCLPY latest-image cache."""
from __future__ import annotations

import asyncio
from fractions import Fraction
from typing import Any

from .engine import MediaEngineError, MediaNegotiationError
from ..robot.backend import RobotBackendError
from ..robot.images import image_pil


def create_camera_track(backend: Any, source: str) -> Any:
    from aiortc import VideoStreamTrack
    from aiortc.mediastreams import MediaStreamError
    from av import VideoFrame

    class CameraTrack(VideoStreamTrack):
        def __init__(self) -> None:
            super().__init__()
            self._message: Any = None
            self._image: Any = None
            self._started: float | None = None
            self._next_frame = 0.0

        async def recv(self) -> Any:
            loop = asyncio.get_running_loop()
            if self._next_frame > loop.time():
                await asyncio.sleep(self._next_frame - loop.time())
            # Wait through temporary source gaps. Do not end the RTP track on
            # one stale sample: an alive camera can recover without negotiation.
            while self.readyState == "live":
                try:
                    message = backend.camera_frame(source)
                    if message is not self._message:
                        break
                    await asyncio.sleep(.002)
                except RobotBackendError as exc:
                    if getattr(backend, "_closing", False):
                        raise MediaStreamError() from exc
                    await asyncio.sleep(.02)
            else:
                raise MediaStreamError()
            if message is not self._message:
                self._image = await asyncio.to_thread(image_pil, message)
                self._message = message
            now = loop.time()
            if self._started is None:
                self._started = now
            self._next_frame = max(self._next_frame + 1 / 30, now + .001)
            frame = VideoFrame.from_image(self._image)
            frame.pts = int((now - self._started) * 90000)
            frame.time_base = Fraction(1, 90000)
            return frame

    return CameraTrack()


class DclpyMediaEngine:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._sessions: dict[str, dict[str, Any]] = {}
        self._opening = 0
        self.on_disconnect: Any = None

    def set_disconnect_handler(self, callback: Any) -> None:
        self.on_disconnect = callback

    async def create_session(self, media_session_id: str, *, kind: str, audio: bool,
                             video: bool, video_source: str | None) -> None:
        if audio or not video:
            error = MediaEngineError("The robot exposes video; a real audio provider is required for audio calls.")
            error.code = "provider_unavailable"
            raise error
        if len(self._sessions) + self._opening >= 8:
            error = MediaEngineError("Robot camera session capacity reached.")
            error.code = "rate_limited"
            raise error
        source = video_source or "robot_head"
        acquired = False
        self._opening += 1
        try:
            from aiortc import RTCPeerConnection, RTCConfiguration
            open_camera = getattr(self.backend, "open_camera", None)
            if open_camera is not None:
                await open_camera(source)
                acquired = True
            self.backend.camera_frame(source)
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        except BaseException as exc:
            if acquired:
                self.backend.close_camera(source)
            if not isinstance(exc, (ImportError, RobotBackendError)):
                raise
            error = MediaEngineError(str(exc))
            error.code = "robot_not_ready" if isinstance(exc, RobotBackendError) else "provider_unavailable"
            raise error from exc
        finally:
            self._opening -= 1
        self._sessions[media_session_id] = {"peer": peer, "source": source, "track": None}

        @peer.on("connectionstatechange")
        async def connection_changed():
            if peer.connectionState in {"failed", "closed"} and media_session_id in self._sessions:
                await self.close_session(media_session_id, "client_disconnect")
                if self.on_disconnect is not None:
                    await self.on_disconnect(media_session_id)

    async def handle_offer(self, media_session_id: str, sdp: str) -> str:
        from aiortc import RTCSessionDescription, RTCRtpSender
        session = self._sessions[media_session_id]
        peer = session["peer"]
        if session["track"] is not None:
            raise MediaNegotiationError("A camera session accepts one offer.")
        track = create_camera_track(self.backend, session["source"])
        session["track"] = track
        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            if not any(transceiver.kind == "video" for transceiver in peer.getTransceivers()):
                raise ValueError("A video receive transceiver is required.")
            peer.addTrack(track)
            # VP8 is supported by desktop/mobile browsers; prefer it over the
            # software H264 path for this low-latency video-only stream.
            codecs = RTCRtpSender.getCapabilities("video").codecs
            for transceiver in peer.getTransceivers():
                if transceiver.kind == "video":
                    transceiver.setCodecPreferences(sorted(codecs, key=lambda codec: codec.mimeType != "video/VP8"))
            await peer.setLocalDescription(await peer.createAnswer())
            return peer.localDescription.sdp
        except Exception as exc:
            await self.close_session(media_session_id, "negotiation_failed")
            raise MediaNegotiationError(f"WebRTC camera negotiation failed: {exc}") from exc

    async def close_session(self, media_session_id: str, reason: str) -> None:
        session = self._sessions.pop(media_session_id, None)
        if session is not None:
            if session["track"] is not None:
                session["track"].stop()
            await session["peer"].close()
            if getattr(self.backend, "close_camera", None) is not None:
                self.backend.close_camera(session["source"])

    async def close(self) -> None:
        for identifier in tuple(self._sessions):
            await self.close_session(identifier, "server_shutdown")
