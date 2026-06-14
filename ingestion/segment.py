from typing import Literal, List, Dict
from typing_extensions import TypedDict

SegmentType = Literal["html", "csv", "json", "text", "mixed"]


class Segment(TypedDict):
    content: str

    segment_type: SegmentType

    start_line: int
    end_line: int

    confidence: float

    confidence_breakdown: Dict[str, float]

    source_signals: List[str]

    warnings: List[str]

    hash: str
    pass_assigned:        Literal["pass1", "pass2"]
