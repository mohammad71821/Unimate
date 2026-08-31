from pathlib import Path

import pytesseract
from PIL import Image

IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/tiff",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def is_image(content_type: str, filename: str) -> bool:
    if content_type in IMAGE_CONTENT_TYPES:
        return True
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def extract_text_from_image(file_path: Path) -> str:
    image = Image.open(file_path)
    text = pytesseract.image_to_string(image, lang="fas+eng")
    return text.strip()
