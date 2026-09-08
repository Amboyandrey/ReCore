"""Uploading a file into a conversation — gated by the `attachments` flag — and its extraction.

Routes provider calls through FakeProvider via monkeypatch, same as test_chat_router.py.
"""

import uuid
from io import BytesIO

import pillow_heif
import pytest
from docx import Document as DocxDocument
from httpx import AsyncClient
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import FeatureFlag, FlagScope
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.attachments import MAX_IMAGE_DIMENSION
from app.services.flags import set_override


def _minimal_png(size: tuple[int, int] = (50, 30), color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """A real, valid PNG of the given size — Pillow both writes and reads them."""
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _rotated_jpeg() -> bytes:
    """A real JPEG, landscape, tagged via EXIF as needing a 90-degree correction — the shape a
    portrait phone photo takes if a client doesn't already bake the rotation into the pixels."""
    image = Image.new("RGB", (100, 50), color=(0, 255, 0))
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: rotate 270 CW to display correctly
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _minimal_pdf(text: bytes) -> bytes:
    """Hand-build the smallest valid PDF that holds one page of real, extractable text — no
    library renders PDFs from scratch, so this writes the object/xref structure directly."""
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/MediaBox[0 0 200 200]/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = b"BT /F1 24 Tf 10 100 Td (" + text + b") Tj ET"
    objects.append(b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + obj + b"endobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010} 00000 n \n".encode()
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def _minimal_docx(paragraphs: list[str]) -> bytes:
    """A real, valid .docx with the given paragraphs — python-docx both writes and reads them."""
    document = DocxDocument()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _minimal_pptx(slide_texts: list[str]) -> bytes:
    """A real .pptx with one text-box slide per string — python-pptx both writes and reads them."""
    presentation = Presentation()
    for text in slide_texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])  # blank layout
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _minimal_xlsx(rows: list[list[object]]) -> bytes:
    """A real .xlsx with the given rows on its one sheet — openpyxl both writes and reads them."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider, *, api_key, base_url):
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every provider build through FakeProvider, in both services that call it."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)


async def _workspace_with_model(client: AsyncClient) -> tuple[str, str]:
    """Sign up as owner, create a workspace, register a credential, enable a model."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"])
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )
    credential_id = credential.json()["id"]
    model = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
        },
    )
    return workspace_id, model.json()["id"]


async def _enable_attachments(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    """Flip the `attachments` flag on for one workspace — the same lever the admin UI pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "attachments"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def test_upload_is_blocked_while_the_flag_is_off(client: AsyncClient) -> None:
    """`attachments` defaults to off — uploading 404s entirely, same as a route that doesn't exist."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 404


async def test_uploading_a_text_file_extracts_its_content(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A text/plain upload is decoded and stored as `extracted_text`, marked `done`."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"the quarterly numbers look good", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] == "the quarterly numbers look good"
    assert body["message_id"] is None  # not attached to a message yet


async def test_uploading_an_unsupported_binary_file_is_marked_unsupported(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A mime type this phase doesn't know how to read is stored but flagged, not silently empty.

    image/bmp specifically: not text-like, not one of PDF/DOCX/PPTX/XLSX, and not one of the
    image formats _normalize_image decodes — genuinely nothing this phase can do with it.
    """
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("photo.bmp", b"BM\x00\x00\x00\x00", "image/bmp")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "unsupported"
    assert body["extracted_text"] is None


async def test_listing_returns_every_attachment_uploaded_into_the_conversation(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """Two uploads into the same conversation both come back, in upload order."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]
    base = f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments"
    await client.post(base, files={"file": ("a.txt", b"first", "text/plain")})
    await client.post(base, files={"file": ("b.txt", b"second", "text/plain")})

    listed = await client.get(base)

    assert [a["original_filename"] for a in listed.json()] == ["a.txt", "b.txt"]


async def test_sending_a_message_with_an_attachment_feeds_its_text_to_the_model(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """An attached file's extracted text reaches the provider alongside the user's own message —
    proven indirectly via FakeProvider's input-token count, which counts words it was sent."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]
    upload = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"one two three four five", "text/plain")},
    )
    attachment_id = upload.json()["id"]

    async with client.stream(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi", "attachment_ids": [attachment_id]},
    ) as response:
        async for _ in response.aiter_lines():
            pass

    messages = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages")
    ).json()
    assistant = messages[1]
    # "Hi" alone is one word; with the attachment's five words (plus the bracketed filename
    # marker) folded in, the fake provider — which counts words in what it was sent — sees more.
    assert assistant["tokens_in"] > 1

    attachments = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments")
    ).json()
    assert attachments[0]["message_id"] == messages[0]["id"]  # linked to the user's turn


async def test_uploading_a_pdf_extracts_its_text(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A real PDF (not just a text file renamed) gets its page text pulled out."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.pdf", _minimal_pdf(b"Hello PDF"), "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] is not None
    assert "Hello PDF" in body["extracted_text"]


async def test_a_pdf_with_no_extractable_text_is_marked_failed(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A blank page (the shape a scanned/image-only PDF takes to a text extractor) fails clearly,
    not silently — there's a real difference between 'no attachment' and 'nothing to read'."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("blank.pdf", buffer.getvalue(), "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "failed"
    assert body["extracted_text"] is None


async def test_uploading_a_docx_extracts_its_paragraphs(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A real Word document's paragraph text comes back, in order."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    docx_bytes = _minimal_docx(["First paragraph.", "Second paragraph."])
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={
            "file": (
                "letter.docx",
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] is not None
    assert "First paragraph." in body["extracted_text"]
    assert "Second paragraph." in body["extracted_text"]


async def test_uploading_a_pptx_extracts_its_slide_text(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A real PowerPoint file's slide text comes back, one slide per block."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    pptx_bytes = _minimal_pptx(["First slide.", "Second slide."])
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={
            "file": (
                "deck.pptx",
                pptx_bytes,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] is not None
    assert "First slide." in body["extracted_text"]
    assert "Second slide." in body["extracted_text"]


async def test_uploading_an_xlsx_extracts_its_cells(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A real spreadsheet's rows come back as tab-separated text, one sheet per block."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    xlsx_bytes = _minimal_xlsx([["Name", "Score"], ["Alice", 95]])
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={
            "file": (
                "sheet.xlsx",
                xlsx_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] is not None
    assert "Name\tScore" in body["extracted_text"]
    assert "Alice\t95" in body["extracted_text"]


async def test_uploading_a_png_is_marked_passthrough_with_no_extracted_text(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """An image gets no text extraction attempt at all — it's handed to the model directly."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("photo.png", _minimal_png(), "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "passthrough"
    assert body["extracted_text"] is None
    assert body["mime"] == "image/png"


async def test_an_oversized_image_is_downscaled_to_the_max_dimension(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A phone-photo-sized image is shrunk to MAX_IMAGE_DIMENSION on its long edge, not rejected."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    huge = _minimal_png(size=(MAX_IMAGE_DIMENSION * 2, MAX_IMAGE_DIMENSION))
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("big.png", huge, "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "passthrough"
    # The stored file is what was actually downscaled — read it back and check its dimensions
    # rather than trusting a byte-count proxy, which a differently-compressible image could fake.
    stored = Image.open(f"{get_settings().storage_dir}/{workspace_id}/{body['id']}")
    assert max(stored.size) == MAX_IMAGE_DIMENSION


async def test_a_smaller_image_is_not_upscaled(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """An image already under the cap is left at its own size, not stretched up to it."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("small.png", _minimal_png(size=(50, 30)), "image/png")},
    )

    body = response.json()
    stored = Image.open(f"{get_settings().storage_dir}/{workspace_id}/{body['id']}")
    assert stored.size == (50, 30)


async def test_a_rotated_photo_is_corrected_before_storage(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """EXIF orientation is baked into the pixels, not left for the model to guess at — a
    landscape-stored, rotate-to-portrait JPEG comes out actually portrait."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("rotated.jpg", _rotated_jpeg(), "image/jpeg")},
    )

    body = response.json()
    assert body["extract_status"] == "passthrough"
    stored = Image.open(f"{get_settings().storage_dir}/{workspace_id}/{body['id']}")
    # The source was 100x50 landscape; orientation 6 means "rotate to display", so the
    # corrected, storage-ready image should be the transposed 50x100 portrait.
    assert stored.size == (50, 100)


async def test_a_heic_photo_is_transcoded_to_jpeg(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """The default iPhone photo format comes out as the one format every provider accepts."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    heic_buffer = BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (40, 40), color=(0, 0, 255))).save(heic_buffer)
    heic_bytes = heic_buffer.getvalue()
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("photo.heic", heic_bytes, "image/heic")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "passthrough"
    assert body["mime"] == "image/jpeg"


async def test_a_corrupt_image_falls_back_to_the_original_bytes_marked_failed(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A file claiming to be an image but that Pillow can't decode doesn't crash the upload — it
    fails clearly, the same "never reject the whole upload" contract every extractor follows."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("broken.png", b"not actually a png", "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "failed"
    assert body["extracted_text"] is None
