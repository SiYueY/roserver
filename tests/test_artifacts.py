"""Artifact Product contract, mapping and multimodal round-trip (docs §5)."""

from __future__ import annotations

import hashlib

from conftest import FakeModel, Turn

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
TEXT = b"hello artifact"


def _upload(client, data: bytes, *, media_type: str = "image/png", filename: str = "a.png"):
    return client.post(
        "/api/v1/artifacts",
        files={"file": (filename, data, media_type)},
    )


def _expected_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_upload_is_content_addressed(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        response = _upload(client, PNG)
        assert response.status_code == 201, response.text
        artifact = response.json()
        assert artifact["artifact_id"] == _expected_id(PNG)
        assert artifact["media_type"] == "image/png"
        assert artifact["size"] == len(PNG)
        assert artifact["filename"] == "a.png"

        # GET metadata returns the same object, including the stored filename.
        fetched = client.get(f"/api/v1/artifacts/{artifact['artifact_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["artifact_id"] == artifact["artifact_id"]
        assert fetched.json()["filename"] == "a.png"

        # GET content returns the exact bytes with an ETag.
        content = client.get(f"/api/v1/artifacts/{artifact['artifact_id']}/content")
        assert content.status_code == 200
        assert content.content == PNG
        assert content.headers["etag"] == f'"{artifact["artifact_id"]}"'
        assert content.headers["content-type"].startswith("image/png")


def test_identical_upload_deduplicates(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        first = _upload(client, PNG).json()
        second = _upload(client, PNG, filename="other.png").json()
        assert first["artifact_id"] == second["artifact_id"]

        # Only one blob is stored on disk.
        root = settings_factory().resolved_artifacts_dir
        digest = first["artifact_id"].split(":", 1)[1]
        blobs = list(root.rglob(digest))
        assert len(blobs) == 1


def test_missing_artifact_is_not_found(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        missing = "sha256:" + "0" * 64
        for path in (
            f"/api/v1/artifacts/{missing}",
            f"/api/v1/artifacts/{missing}/content",
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "artifact_not_found"


def test_upload_limits_and_media_type_validation(client_factory, settings_factory):
    with client_factory(
        settings_factory(max_artifact_bytes=4), model=FakeModel([Turn(text="x")])
    ) as client:
        too_big = _upload(client, b"way too many bytes")
        assert too_big.status_code == 413
        assert too_big.json()["error"]["code"] == "payload_too_large"

    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        bad = _upload(client, TEXT, media_type="not-a-media-type")
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "unsupported_content_type"


def test_delete_is_idempotent_but_blocked_while_referenced(
    client_factory, settings_factory, api
):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        artifact = _upload(client, PNG).json()
        artifact_id = artifact["artifact_id"]

        # Unreferenced: delete succeeds, repeats are no-ops.
        assert client.delete(f"/api/v1/artifacts/{artifact_id}").status_code == 204
        assert client.delete(f"/api/v1/artifacts/{artifact_id}").status_code == 204
        assert client.get(f"/api/v1/artifacts/{artifact_id}").status_code == 404

    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        artifact = _upload(client, PNG).json()
        artifact_id = artifact["artifact_id"]
        session = api.create_session(client)
        run = api.start_run(
            client,
            session["session_id"],
            content=[{"type": "image", "artifact_id": artifact_id}],
        )
        assert run.status_code == 201, run.text
        api.wait_terminal(client, run.json()["run_id"])

        # A live Session references it -> protected.
        blocked = client.delete(f"/api/v1/artifacts/{artifact_id}")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "artifact_in_use"

        # Once the referencing Session is gone the artifact can be deleted.
        assert client.delete(f"/api/v1/sessions/{session['session_id']}").status_code == 204
        assert client.delete(f"/api/v1/artifacts/{artifact_id}").status_code == 204


def test_image_round_trip_through_a_run(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="seen")])) as client:
        artifact = _upload(client, PNG).json()
        artifact_id = artifact["artifact_id"]

        session = api.create_session(client)
        run = api.start_run(
            client,
            session["session_id"],
            content=[{"type": "image", "artifact_id": artifact_id}],
        )
        assert run.status_code == 201, run.text
        api.wait_terminal(client, run.json()["run_id"])

        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        blocks = snapshot["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        # The Product artifact_id survives canonical storage and projection.
        assert blocks[0]["artifact_id"] == artifact_id
        assert blocks[0]["media_type"] == "image/png"

        # The canonical transcript must hold an ArtifactReferenceContent, and the
        # injected reader must be able to resolve it.
        stored = client.get(f"/api/v1/artifacts/{artifact_id}/content")
        assert stored.content == PNG


def test_artifact_input_requires_a_valid_registered_id(
    client_factory, settings_factory, api
):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        session = api.create_session(client)
        malformed = api.start_run(
            client,
            session["session_id"],
            content=[{"type": "image", "artifact_id": "nope"}],
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "invalid_input"

        unknown = api.start_run(
            client,
            session["session_id"],
            content=[{"type": "file", "artifact_id": "sha256:" + "b" * 64}],
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "artifact_not_found"


def test_artifact_reference_survives_restart(    client_factory, settings_factory, api
):
    artifact_id: str
    session_id: str
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        artifact_id = _upload(client, TEXT, media_type="text/plain").json()["artifact_id"]
        session = api.create_session(client)
        session_id = session["session_id"]
        run = api.start_run(
            client,
            session_id,
            content=[{"type": "file", "artifact_id": artifact_id}],
        )
        assert run.status_code == 201, run.text
        api.wait_terminal(client, run.json()["run_id"])

    # A fresh app over the same data dir must still resolve the reference.
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
        blocks = snapshot["messages"][0]["content"]
        assert blocks[0]["artifact_id"] == artifact_id
        assert blocks[0]["type"] == "file"
        assert client.get(f"/api/v1/artifacts/{artifact_id}/content").content == TEXT


# -- tool-produced media (docs §5.2: Workspace-backed materialization) ------
MEDIA = b"\x89PNG\r\n\x1a\n" + b"tool-frame"


class FileCapableModel(FakeModel):
    """The fake model must accept FILE modality: artifact refs map to FILE."""

    @property
    def capabilities(self):  # type: ignore[override]
        from roboagent.model import ModelCapabilities
        from roboagent.runtime import Modality

        return ModelCapabilities(
            input_modalities=frozenset({Modality.TEXT, Modality.FILE}),
            output_modalities=frozenset({Modality.TEXT}),
            tool_calling=True,
            parallel_tool_calls=False,
        )


def _snapshot_tool():
    from roboagent.message import FrozenJsonObject
    from roboagent.tool import (
        BinaryToolContent,
        RawToolResult,
        Tool,
        ToolDefinition,
        ToolEffectKind,
        ToolExecutionMode,
    )

    async def handler(arguments, context):
        # Tools must return RawToolResult; BinaryToolContent alone violates the
        # Tool output contract.
        return RawToolResult((BinaryToolContent(MEDIA, "image/png"),))

    return Tool(
        ToolDefinition("snapshot", "Return a frame.", FrozenJsonObject({"type": "object"})),
        handler,
        execution_mode=ToolExecutionMode.SERIAL,
        effect_kind=ToolEffectKind.READ_ONLY,
    )


def test_tool_binary_output_materializes_into_the_artifact_store(
    client_factory, settings_factory, api
):
    from roboagent.message import FrozenJsonObject, ToolCall
    from conftest import Turn

    call = ToolCall("shot-1", "snapshot", FrozenJsonObject({}))
    model = FileCapableModel(
        [Turn(text="", tool_calls=(call,)), Turn(text="done")]
    )
    with client_factory(
        settings_factory(), model=model, tools=(_snapshot_tool(),)
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "take a photo")
        assert run.status_code == 201, run.text
        info = api.wait_terminal(client, run.json()["run_id"])
        assert info["status"] == "completed", info

        expected = _expected_id(MEDIA)
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        tool_blocks = [
            block
            for message in snapshot["messages"]
            if message["role"] == "tool"
            for block in message["content"]
        ]
        assert tool_blocks, snapshot["messages"]
        assert tool_blocks[0]["artifact_id"] == expected
        assert tool_blocks[0]["type"] == "image"

        # The workspace-backed blob is the same artifact the Product API serves.
        content = client.get(f"/api/v1/artifacts/{expected}/content")
        assert content.status_code == 200
        assert content.content == MEDIA
