# utils/context.py

def get_local_window(text: str, pos: int, radius: int = 120) -> str: 
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    return text[start:end]


def compute_local_density(window: str) -> float:
    if not window:
        return 0.0
    return window.count("\n") / len(window)
