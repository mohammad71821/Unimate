from pathlib import Path

from pypdf import PdfReader


def extract_text_from_pdf(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    text_parts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(text_parts).strip()
