# ingestion/ocr.py

import io
from typing import List, Optional, Literal
from PIL import Image

from ingestion.result import ExtractionResult, ExtractionMetadata


# ---------------------------------
# Vision classification
# ---------------------------------

ImageType = Literal["screenshot", "document_scan", "chart", "diagram", "photo", "unknown"]


def _classify_image(img: Image.Image) -> ImageType:
    """
    Lightweight heuristic classifier.
    No ML dependency yet.
    """

    w, h = img.size
    aspect = w / h

    # Heuristic signals
    if aspect > 2 or aspect < 0.5:
        return "screenshot"

    # High text density hint (simple proxy)
    if img.mode in ("RGB", "L") and max(img.size) > 2000:
        return "document_scan"

    return "unknown"


# ---------------------------------
# OCR core (pluggable)
# ---------------------------------

def _run_ocr(img: Image.Image) -> str:
    """
    Placeholder OCR engine hook.
    Replace with Tesseract / EasyOCR / cloud OCR later.
    """
    try:
        import pytesseract
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


# ---------------------------------
# Main OCR pipeline
# ---------------------------------

def extract_image(raw_bytes: bytes) -> ExtractionResult:
    """
    OCR + image understanding entry point.

    Flow:
    1. Decode image
    2. Classify image type
    3. Run OCR (always baseline)
    4. Attach structural hints (no interpretation)
    """

    warnings: List[str] = []

    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as e:
        return ExtractionResult(
            text="",
            metadata=ExtractionMetadata(
                source_format="image",
                page_count=None,
                sheet_count=None,
                char_count=0,
                line_count=0,
                word_count=0,
                encoding_used=None,
            ),
            extraction_warnings=[f"Image decode failed: {str(e)[:200]}"],
            extraction_success=False,
        )

    img_type = _classify_image(img)

    ocr_text = _run_ocr(img)

    if not ocr_text.strip():
        warnings.append("OCR returned empty output")

    # Inject lightweight structural hint (IMPORTANT)
    structured_text = f"[IMAGE_TYPE: {img_type}]\n{ocr_text}".strip()

    return ExtractionResult(
        text=structured_text,
        metadata=ExtractionMetadata(
            source_format="image",
            page_count=None,
            sheet_count=None,
            char_count=len(structured_text),
            line_count=len(structured_text.splitlines()),
            word_count=len(structured_text.split()),
            encoding_used=None,
        ),
        extraction_warnings=warnings,
        extraction_success=bool(ocr_text.strip()),
    )

# ---------------------------------
# Interactive test harness
# ---------------------------------

if __name__ == "__main__":
    import os

    print("\n🧪 OCR Interactive Test Mode")
    print("Drop an image file path (or type 'exit')\n")

    while True:
        path = input("image> ").strip()

        if path.lower() in ("exit", "quit"):
            break

        if not os.path.exists(path):
            print("❌ File not found\n")
            continue

        try:
            with open(path, "rb") as f:
                raw = f.read()

            result = extract_image(raw)

            print("\n──────── OCR OUTPUT ────────")
            print(result["text"])

            print("\n──────── METADATA ──────────")
            meta = result["metadata"]
            for k, v in meta.items():
                print(f"{k}: {v}")

            print("\n──────── WARNINGS ──────────")
            for w in result["extraction_warnings"]:
                print("⚠", w)

            print("\n──────── DONE ──────────────\n")

        except Exception as e:
            print("❌ Error:", str(e))
