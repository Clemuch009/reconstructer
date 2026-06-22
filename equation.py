import sys
import re
from pathlib import Path
from typing import List, Dict, Any
import json

# Try to import optional libraries
try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

import pdfplumber
from bs4 import BeautifulSoup

# ==================== CORE MATH DETECTION ====================

MATH_UNICODE_RANGES = [
    (0x2200, 0x22FF),   # Mathematical Operators
    (0x2A00, 0x2AFF),   # Supplemental
    (0x1D400, 0x1D7FF), # Alphanumeric symbols
    (0x2100, 0x214F),   # Letterlike
]

MATH_FONT_KEYWORDS = ['math', 'cambria', 'stix', 'cmr', 'cmsy', 'cmex', 'symbol', 'ams']
MATH_KEYWORDS = {'sum', 'prod', 'int', 'sqrt', 'frac', 'lim', 'log', 'sin', 'cos', 'tan', 
                 'pi', 'delta', 'alpha', 'beta', 'sigma', 'gamma', 'z0', 'z1', 'z2', 
                 'ia', 'ib', 'ic', 'va', 'vb', 'vc', 'pu'}

def is_math_unicode(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return any(start <= code <= end for start, end in MATH_UNICODE_RANGES)

def has_math_symbols(text: str) -> int:
    """Enhanced scoring system from your report"""
    score = 0
    for char in text:
        if is_math_unicode(char):
            score += 3
        elif char in '=+*/^_()[]{}<>\\|':
            score += 1
    
    lower = text.lower()
    for kw in MATH_KEYWORDS:
        if kw in lower:
            score += 2
    
    # Strong boost for equations
    if '=' in text and any(c.isalnum() for c in text):
        score += 5
    
    return score

def detect_formulas_in_text(text: str, min_score: int = 5) -> List[Dict]:
    """Text-based detection with improved multi-line grouping"""
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    equations = []
    i = 0
    while i < len(lines):
        line = lines[i]
        score = has_math_symbols(line)
        
        # Multi-line grouping for complex equations
        block = [line]
        j = i + 1
        while j < len(lines) and (has_math_symbols(lines[j]) >= min_score - 3 or '=' in lines[j]):
            block.append(lines[j])
            j += 1
        
        block_text = " ".join(block) if len(block) > 1 else line
        final_score = has_math_symbols(block_text)
        
        if final_score >= min_score:
            equations.append({
                "start_line": i + 1,
                "end_line": i + len(block),
                "content": block_text,
                "confidence": min(0.98, final_score / 25.0),
                "type": "display" if len(block) > 1 or len(block_text) > 60 else "inline"
            })
        
        i = j if len(block) > 1 else i + 1
    return equations

# ==================== FORMAT HANDLERS ====================

def extract_from_docx(file_path: str) -> Dict[str, Any]:
    if not HAS_DOCX:
        return {"error": "python-docx not available", "formulas": []}
    # ... (kept original)
    doc = Document(file_path)
    full_text = []
    equations = []
    for para_idx, para in enumerate(doc.paragraphs):
        para_text = para.text.strip()
        full_text.append(para_text)
        for run in para.runs:
            if run.element.xpath('.//m:oMath'):
                content = run.text.strip() or "[Native Word Equation]"
                equations.append({
                    "start_line": para_idx + 1,
                    "content": content,
                    "confidence": 1.0,
                    "type": "display"
                })
    text_content = "\n".join(full_text)
    text_eqs = detect_formulas_in_text(text_content)
    return {
        "text": text_content,
        "formulas": equations + text_eqs,
        "count": len(equations) + len(text_eqs)
    }

def extract_from_pdf(file_path: str) -> Dict[str, Any]:
    full_text = ""
    equations = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                full_text += page_text + "\n"
                
                # Glyph level + text fallback
                words = page.extract_words(x_tolerance=3, y_tolerance=3)
                current_zone = []
                for word in words:
                    text = word.get('text', '')
                    if has_math_symbols(text) > 3 or '=' in text:
                        current_zone.append(text)
                if current_zone:
                    math_str = " ".join(current_zone)
                    if has_math_symbols(math_str) >= 5:
                        equations.append({
                            "page": page_num + 1,
                            "content": math_str,
                            "confidence": 0.85,
                            "type": "display"
                        })
                
                equations.extend(detect_formulas_in_text(page_text))
                
    except Exception as e:
        full_text += f"\n[Error: {e}]"
    
    # Deduplicate
    seen = set()
    unique = []
    for eq in equations:
        key = eq.get("content", "")[:100]
        if key not in seen:
            seen.add(key)
            unique.append(eq)
    
    return {"text": full_text.strip(), "formulas": unique, "count": len(unique)}

def extract_from_html(file_path: str) -> Dict[str, Any]:
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        soup = BeautifulSoup(f, 'html.parser')
    full_text = soup.get_text()
    equations = []
    # Math tags detection...
    equations.extend(detect_formulas_in_text(full_text))
    return {"text": full_text, "formulas": equations, "count": len(equations)}

# ==================== MAIN ENTRY POINT ====================

def extract_equations(source, source_type="file"):
    if source_type == "text":
        return detect_formulas_in_text(source)
    
    path = Path(source)
    if not path.exists():
        return [{"error": f"File not found: {source}"}]
    
    suffix = path.suffix.lower()
    if suffix == '.docx':
        data = extract_from_docx(str(path))
    elif suffix == '.pdf':
        data = extract_from_pdf(str(path))
    elif suffix in ['.html', '.htm']:
        data = extract_from_html(str(path))
    else:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            data = {"formulas": detect_formulas_in_text(text)}
        except:
            data = {"formulas": []}
    
    return data.get("formulas", [])

if __name__ == "__main__":
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        print(f"\n🔍 Analyzing: {file_path}\n")
        results = extract_equations(file_path)
        
        print(f"✅ Detected **{len(results)}** equation block(s)\n")
        
        # Show first 15 + summary
        for i, eq in enumerate(results[:15], 1):
            print(f"{i:2d}. [{eq.get('confidence', 0):.2f}] {eq.get('content', '')[:140]}...")
            if 'page' in eq:
                print(f"    Page: {eq['page']}")
        
        if len(results) > 15:
            print(f"\n... and {len(results)-15} more equations detected.")
        
        # Save full JSON report
        report = {
            "file": file_path,
            "total_equations": len(results),
            "equations": results
        }
        json_path = str(Path(file_path).with_suffix('.formulas.json'))
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n📄 Full detailed report saved to: {json_path}")
        
    else:
        # Built-in test
        sample = """
System load equation:
Score(x) = Σ (α_i * x_i) / √(1 + Δγ)
Normal text continues here.
Risk = σ_max - σ_min
E = mc^2
"""
        print("=== Running Test on Sample Text ===")
        results = extract_equations(sample, source_type="text")
        print(f"Detected {len(results)} equation blocks")
        for i, eq in enumerate(results, 1):
            print(f"{i}. {eq.get('content', '')}")
