from typing import List, Optional, Literal
from typing_extensions import TypedDict


SourceFormat = Literal[
    "txt",
    "csv",
    "pdf",
    "docx",
    "xlsx",
    "json",
    "html",
    "unknown",
]


class ExtractionMetadata(TypedDict):
    source_format: SourceFormat
    page_count: Optional[int]
    sheet_count: Optional[int]
    char_count:     int
    line_count:     int
    word_count:     int
    encoding_used:  Optional[str]


class ExtractionResult(TypedDict):
    text:                str
    metadata:            ExtractionMetadata
    extraction_warnings: List[str]
    extraction_success:  bool
    visuals:             List   # List[EmbeddedVisual] — imported from ingestion.visual
                                # Optional — absent in text-only extractors
