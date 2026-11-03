"""Smoke tests for the pypdf-based test-PDF constructor ``pdf_fixtures``.

Each test builds a document from a description and reads it back with pypdf to
check that page count, /Rotate, CropBox, image objects and the encryption flag
match what was described.
"""

import io

from pypdf import PdfReader

from tests.pdf_fixtures import (
    OWNER_ONLY,
    USER_REQUIRED,
    DocSpec,
    ImageSpec,
    PageSpec,
    TextRun,
    build_pdf,
    simple_image_pdf,
    simple_multi_page_pdf,
)


def _reader(data: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(data))


def _images(page):
    xobjects = page["/Resources"].get("/XObject", {})
    return [x.get_object() for x in xobjects.values() if x.get_object().get("/Subtype") == "/Image"]


def test_page_count_matches_description():
    data = simple_multi_page_pdf([["a"], ["b"], ["c"]])
    reader = _reader(data)
    assert len(reader.pages) == 3
    assert not reader.is_encrypted


def test_rotate_and_boxes_match_description():
    doc = DocSpec(
        pages=(
            PageSpec(
                media_box=(10, 20, 310, 420),
                crop_box=(20, 30, 300, 400),
                rotate=90,
                runs=(TextRun("hello"),),
            ),
            PageSpec(),
        )
    )
    reader = _reader(build_pdf(doc))
    first, second = reader.pages
    assert [float(v) for v in first.mediabox] == [10, 20, 310, 420]
    assert [float(v) for v in first.cropbox] == [20, 30, 300, 400]
    assert int(first["/Rotate"]) == 90
    assert "/Rotate" not in second
    assert "/CropBox" not in second


def test_image_object_matches_description():
    doc = DocSpec(
        pages=(PageSpec(images=(ImageSpec(kind="rgb", width=5, height=3, smask=True),)),)
    )
    page = _reader(build_pdf(doc)).pages[0]
    images = _images(page)
    assert len(images) == 1
    assert int(images[0]["/Width"]) == 5
    assert int(images[0]["/Height"]) == 3
    assert images[0]["/ColorSpace"] == "/DeviceRGB"
    assert "/SMask" in images[0]


def test_simple_image_pdf_has_one_gray_image():
    page = _reader(simple_image_pdf(4, 6, kind="gray")).pages[0]
    images = _images(page)
    assert len(images) == 1
    assert (int(images[0]["/Width"]), int(images[0]["/Height"])) == (4, 6)
    assert images[0]["/ColorSpace"] == "/DeviceGray"


def test_owner_only_encryption_opens_without_password():
    reader = _reader(build_pdf(DocSpec(encryption=OWNER_ONLY)))
    assert reader.is_encrypted
    assert len(reader.pages) == 1


def test_user_password_encryption_requires_password():
    reader = _reader(build_pdf(DocSpec(encryption=USER_REQUIRED)))
    assert reader.is_encrypted
    assert reader.decrypt("") == 0
    assert reader.decrypt("user-secret") != 0
    assert len(reader.pages) == 1
