import sys
import re
from pathlib import Path
from typing import List, Dict, Any

# Available libraries
try:
    from docx import Document
    from docx.oxml import OxmlElement
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

import pdfplumber
import pypdf
from bs4 import BeautifulSoup
import pytesseract
from PIL import Image
import io

# LaTeX / Math patterns (handles nested to some degree)
LATEX_PATTERNS = [
    re.compile(r'\$\$(.+?)\$\$', re.DOTALL),           # Display math
    re.compile(r'\$(.+?)\$', re.DOTALL),               # Inline math
    re.compile(r'\\begin\{(.+?)\}(.+?)\\end\{\1\}', re.DOTALL),  # Environments
    re.compile(r'\\\[(.+?)\\\]', re.DOTALL),
    re.compile(r'\\begin\{equation\}(.+?)\\end\{equation\}', re.DOTALL),
]

def detect_formulas_in_text(text: str) -> List[str]:
    """Basic regex-based detection with support for nested-ish structures."""
    formulas = []
    for pattern in LATEX_PATTERNS:
        matches = pattern.findall(text)
        if isinstance(matches[0], tuple) if matches else False:  # for groups
            formulas.extend([m[1] if isinstance(m, tuple) else m for m in matches])
        else:
            formulas.extend(matches)
    # Clean and dedup
    return list(dict.fromkeys([f.strip() for f in formulas if len(f.strip()) > 3]))

def extract_from_docx(file_path: str) -> Dict[str, Any]:
    """Extract text and native Word equations (OMML -> LaTeX-ish)."""
    if not HAS_DOCX:
        return {"error": "python-docx not available"}
    
    doc = Document(file_path)
    text = []
    equations = []
    
    for para in doc.paragraphs:
        text.append(para.text)
        # Check for math elements
        for run in para.runs:
            if run.element.xpath('.//m:oMath'):
                equations.append(run.text or "[Math Equation]")
    
    full_text = "\n".join(text)
    return {
        "text": full_text,
        "formulas": equations + detect_formulas_in_text(full_text),
        "count": len(equations) + len(detect_formulas_in_text(full_text))
    }

def extract_from_pdf(file_path: str) -> Dict[str, Any]:
    """PDF extraction with multiple backends."""
    formulas = []
    full_text = ""
    
    # pdfplumber + pypdf for text
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                full_text += page_text + "\n"
                formulas.extend(detect_formulas_in_text(page_text))
    except Exception as e:
        full_text += f"\n[pdfplumber error: {e}]"
    
    # Fallback pypdf
    try:
        reader = pypdf.PdfReader(file_path)
        for page in reader.pages:
            page_text = page.extract_text() or ""
            full_text += page_text + "\n"
            formulas.extend(detect_formulas_in_text(page_text))
    except:
        pass
    
    # OCR fallback for image-based formulas (slow but powerful)
    # Uncomment and install poppler if needed for pdf2image
    # from pdf2image import convert_from_path
    # images = convert_from_path(file_path)
    # for img in images:
    #     ocr_text = pytesseract.image_to_string(img)
    #     formulas.extend(detect_formulas_in_text(ocr_text))
    
    return {
        "text": full_text.strip(),
        "formulas": list(dict.fromkeys(formulas)),
        "count": len(list(dict.fromkeys(formulas)))
    }

def extract_from_html(file_path: str) -> Dict[str, Any]:
    """HTML with MathML, KaTeX, MathJax support."""
    with open(file_path, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')
    
    formulas = []
    full_text = soup.get_text()
    
    # MathML
    math_tags = soup.find_all('math')
    for tag in math_tags:
        formulas.append(str(tag))
    
    # KaTeX / MathJax spans
    for cls in ['katex', 'math', 'mjx-math']:
        for tag in soup.find_all(class_=lambda x: x and cls in x):
            formulas.append(tag.get_text())
    
    formulas.extend(detect_formulas_in_text(full_text))
    
    return {
        "text": full_text,
        "formulas": list(dict.fromkeys(formulas)),
        "count": len(list(dict.fromkeys(formulas)))
    }

def main(file_path: str):
    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}")
        return
    
    suffix = path.suffix.lower()
    result = {"file": str(path), "format": suffix, "formulas": []}
    
    if suffix == '.docx':
        result.update(extract_from_docx(str(path)))
    elif suffix == '.pdf':
        result.update(extract_from_pdf(str(path)))
    elif suffix in ['.html', '.htm']:
        result.update(extract_from_html(str(path)))
    else:
        # Generic text fallback
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
            result["text"] = text
            result["formulas"] = detect_formulas_in_text(text)
            result["count"] = len(result["formulas"])
        except:
            result["error"] = "Unsupported format or binary file"
    
    print(f"\n=== Formula Detection Report ===")
    print(f"File: {result['file']}")
    print(f"Detected formulas: {result.get('count', 0)}")
    for i, eq in enumerate(result.get("formulas", [])[:20], 1):  # limit output
        print(f"{i}. {eq[:150]}{'...' if len(eq)>150 else ''}")
    if len(result.get("formulas", [])) > 20:
        print(f"... and {len(result['formulas'])-20} more")
    
    return result

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python formula_detector.py <document_path>")
