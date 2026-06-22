# ingestion/asset_detector.py

from typing import List, TypedDict, Optional


class Asset(TypedDict):
    type: str               # "image"
    source: str            # pdf | docx | html
    bytes: bytes
    page: Optional[int]
    context: Optional[str] # nearby text
    meta: dict


def extract_assets_from_pdf(pdf_bytes: bytes) -> List[Asset]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTImage

    assets: List[Asset] = []

    for page_num, page_layout in enumerate(extract_pages(pdf_bytes), start=1):
        for element in page_layout:
            if isinstance(element, LTImage):
                try:
                    assets.append({
                        "type": "image",
                        "source": "pdf",
                        "bytes": element.stream.get_rawdata(),
                        "page": page_num,
                        "context": None,
                        "meta": {}
                    })
                except Exception:
                    continue

    return assets


def extract_assets_from_docx(docx_path: str) -> List[Asset]:
    from docx import Document

    doc = Document(docx_path)
    assets: List[Asset] = []

    for rel in doc.part._rels:
        rel_obj = doc.part._rels[rel]
        if "image" in rel_obj.target_ref:
            assets.append({
                "type": "image",
                "source": "docx",
                "bytes": rel_obj.target_part.blob,
                "page": None,
                "context": None,
                "meta": {}
            })

    return assets


def extract_assets_from_html(html: str) -> List[Asset]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    assets: List[Asset] = []

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        assets.append({
            "type": "image",
            "source": "html",
            "bytes": src.encode("utf-8"),
            "page": None,
            "context": img.parent.get_text(strip=True) if img.parent else None,
            "meta": {
                "alt": img.get("alt")
            }
        })

    return assets

if __name__ == "__main__":
    import sys

    print("Asset Detector Test")

    file = sys.argv[1]

    if file.endswith(".pdf"):
        from pathlib import Path
        assets = extract_assets_from_pdf(Path(file).read_bytes())

    elif file.endswith(".docx"):
        assets = extract_assets_from_docx(file)

    elif file.endswith(".html"):
        assets = extract_assets_from_html(open(file).read())

    else:
        print("Unsupported format")
        exit()

    print(f"\nDetected assets: {len(assets)}\n")

    for i, a in enumerate(assets[:5]):
        print(f"[{i}] {a['source']} | {len(a['bytes'])} bytes")
