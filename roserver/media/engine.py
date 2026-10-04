"""Media engine boundary and a deterministic simulated engine (docs §5.4-§5.6).

项目当前**尚未进入真实媒体集成阶段**，因此 roserver 只定义抽象边界，并只提供
一个离线、确定性的模拟媒体引擎：

    MediaEngine            抽象接口（今天的唯一实现见下）
    SimulatedMediaEngine   模拟实现，测试与开发使用

未来接入真实媒体时，实现该接口的将是一个真实 WebRTC PeerConnection 通道
（SDP / ICE / audio / video track / PCM bridge），**不引入 gRPC**，也不新增
重量级依赖。今天不做真实 WebRTC。

引擎失败按 §5.5 映射到封闭的 Product 错误码：

    协商失败        -> media_negotiation_failed
    其他引擎故障    -> internal_error

模拟器是确定性的：内存中维护每个 media session 的资源（track / 缓冲占位），
answer SDP 由 offer 的 SHA-256 派生，不访问网络。
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable

from ..errors import ProductError

MEDIA_KINDS = frozenset({"call", "camera"})


# ---------------------------------------------------------------------
# media-engine errors -> Product error codes (docs §5.5)
# ---------------------------------------------------------------------
class MediaEngineError(Exception):
    """Base class for media engine failures carrying a Product error code."""

    code = "internal_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class MediaNegotiationError(MediaEngineError):
    code = "media_negotiation_failed"


def to_product_error(exc: MediaEngineError) -> ProductError:
    """Map a media-engine failure onto the Product error envelope."""
    return ProductError(exc.code, exc.message)


# ---------------------------------------------------------------------
# media engine boundary (future real implementation: WebRTC)
# ---------------------------------------------------------------------
@runtime_checkable
class MediaEngine(Protocol):
    """Media session engine boundary.

    今天的实现是 :class:`SimulatedMediaEngine`；未来接入真实媒体时由真实
    WebRTC 实现填充该接口（PeerConnection / SDP / ICE / track），不引入 gRPC。

    所有方法都是 async，由 MediaService 在单个事件循环上调用。``close()``
    用于 server shutdown，释放全部引擎侧资源。
    """

    async def create_session(
        self,
        media_session_id: str,
        *,
        kind: str,
        audio: bool,
        video: bool,
        video_source: str | None,
    ) -> None: ...

    async def handle_offer(self, media_session_id: str, sdp: str) -> str: ...

    async def close_session(self, media_session_id: str, reason: str) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------
# deterministic simulator
# ---------------------------------------------------------------------
class SimulatedMediaEngine:
    """Offline, in-memory :class:`MediaEngine` used by tests and dev.

    Deterministic: no network, no real media.  Each session holds a small
    resource record so tests can assert that every terminal path (DELETE,
    expiry, negotiation failure, shutdown) releases it.  The answer SDP is
    syntactically valid and derived from the offer's SHA-256, so it is stable
    across runs.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        # One-shot fault injection for negotiation mapping tests.
        self._fail_next: str | None = None

    # -- test/dev control surface -------------------------------------
    @property
    def active_session_ids(self) -> set[str]:
        return set(self._sessions)

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def resource(self, media_session_id: str) -> dict[str, Any] | None:
        record = self._sessions.get(media_session_id)
        return None if record is None else dict(record)

    def fail_next(self, kind: str) -> None:
        """Make the next engine call fail with ``negotiation``/``unavailable``."""
        self._fail_next = kind

    def _maybe_fail(self) -> None:
        kind = self._fail_next
        if kind is None:
            return
        self._fail_next = None
        if kind == "negotiation":
            raise MediaNegotiationError("SDP negotiation failed.")
        if kind == "unavailable":
            raise MediaEngineError("Media engine is unavailable.")
        raise MediaEngineError(f"Unknown injected failure {kind!r}.")

    # -- internals ----------------------------------------------------
    @staticmethod
    def _answer_sdp(
        media_session_id: str,
        offer_sdp: str,
        *,
        audio: bool,
        video: bool,
    ) -> str:
        digest = hashlib.sha256(offer_sdp.encode("utf-8")).hexdigest()
        lines = [
            "v=0",
            f"o=- {digest[:32]} 1 IN IP4 127.0.0.1",
            "s=roserver-simulated",
            "t=0 0",
            "a=group:BUNDLE " + " ".join(
                mid for mid, enabled in (("0", audio), ("1", video)) if enabled
            ),
            f"a=simulated-session:{media_session_id}",
            f"a=simulated-offer-sha256:{digest}",
        ]
        if audio:
            lines += [
                "m=audio 9 UDP/TLS/RTP/SAVPF 111",
                "c=IN IP4 0.0.0.0",
                "a=mid:0",
                "a=recvonly",
                "a=rtpmap:111 opus/48000/2",
            ]
        if video:
            lines += [
                "m=video 9 UDP/TLS/RTP/SAVPF 96",
                "c=IN IP4 0.0.0.0",
                "a=mid:1",
                "a=recvonly",
                "a=rtpmap:96 H264/90000",
            ]
        return "\r\n".join(lines) + "\r\n"

    # -- MediaEngine --------------------------------------------------
    async def create_session(
        self,
        media_session_id: str,
        *,
        kind: str,
        audio: bool,
        video: bool,
        video_source: str | None,
    ) -> None:
        self._maybe_fail()
        if kind not in MEDIA_KINDS:
            raise MediaEngineError(f"Unsupported media kind {kind!r}.")
        self._sessions[media_session_id] = {
            "kind": kind,
            "audio": audio,
            "video": video,
            "video_source": video_source,
            "tracks": (
                (["audio"] if audio else []) + (["video"] if video else [])
            ),
            "buffers": 0,
            "negotiated": False,
        }

    async def handle_offer(self, media_session_id: str, sdp: str) -> str:
        self._maybe_fail()
        record = self._sessions.get(media_session_id)
        if record is None:
            raise MediaNegotiationError(
                f"Media session {media_session_id!r} has no engine resources."
            )
        record["negotiated"] = True
        record["buffers"] = 0
        return self._answer_sdp(
            media_session_id,
            sdp,
            audio=bool(record["audio"]),
            video=bool(record["video"]),
        )

    async def close_session(self, media_session_id: str, reason: str) -> None:
        self._maybe_fail()
        self._sessions.pop(media_session_id, None)

    async def close(self) -> None:
        self._sessions.clear()


__all__ = [
    "MEDIA_KINDS",
    "MediaEngine",
    "MediaEngineError",
    "MediaNegotiationError",
    "SimulatedMediaEngine",
    "to_product_error",
]
