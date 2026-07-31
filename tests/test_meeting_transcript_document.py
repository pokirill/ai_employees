from types import SimpleNamespace

import pytest

from team_bot.main import _extract_transcript_text, _meeting_transcript_document_file


def _message(document=None):
    return SimpleNamespace(document=document)


def _document(file_name="", mime_type="", file_id="file-1", file_size=100):
    return SimpleNamespace(file_name=file_name, mime_type=mime_type, file_id=file_id, file_size=file_size)


def test_docx_with_correct_mime_type_is_detected():
    doc = _document(
        file_name="встреча.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    result = _meeting_transcript_document_file(_message(doc))
    assert result == ("file-1", 100, ".docx")


def test_docx_with_generic_mime_type_falls_back_to_extension():
    # Некоторые клиенты Telegram шлют .docx с mime_type=application/octet-stream.
    doc = _document(file_name="встреча.docx", mime_type="application/octet-stream")
    result = _meeting_transcript_document_file(_message(doc))
    assert result == ("file-1", 100, ".docx")


def test_txt_document_is_detected():
    doc = _document(file_name="транскрипт.txt", mime_type="text/plain")
    result = _meeting_transcript_document_file(_message(doc))
    assert result == ("file-1", 100, ".txt")


def test_pdf_document_is_not_detected():
    doc = _document(file_name="отчёт.pdf", mime_type="application/pdf")
    assert _meeting_transcript_document_file(_message(doc)) is None


def test_image_document_is_not_detected():
    doc = _document(file_name="photo.jpg", mime_type="image/jpeg")
    assert _meeting_transcript_document_file(_message(doc)) is None


def test_message_without_document_returns_none():
    assert _meeting_transcript_document_file(_message(None)) is None


def test_extract_text_from_txt_utf8(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Обсудили план на спринт", encoding="utf-8")
    assert _extract_transcript_text(str(path), ".txt") == "Обсудили план на спринт"


def test_extract_text_from_txt_cp1251_fallback(tmp_path):
    path = tmp_path / "t.txt"
    path.write_bytes("Обсудили план на спринт".encode("cp1251"))
    assert _extract_transcript_text(str(path), ".txt") == "Обсудили план на спринт"


def test_extract_text_from_docx(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Первая реплика встречи.")
    doc.add_paragraph("Вторая реплика встречи.")
    path = tmp_path / "t.docx"
    doc.save(str(path))

    text = _extract_transcript_text(str(path), ".docx")
    assert "Первая реплика встречи." in text
    assert "Вторая реплика встречи." in text
