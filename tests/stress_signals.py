import pytest
from preprocess.regex_signals import extract_signals

def test_signal_overlapping_shadowing():
    # This input is physically 3 characters: "---"
    # But it potentially triggers:
    # 1. The SEPARATOR_PATTERN (---)
    # 2. If it were on a newline, it would trigger NEWLINE_PATTERN too.
    
    dirty_input = "\n---\n"
    signals = extract_signals(dirty_input)
    
    print("\nDetected Signals:")
    for s in signals:
        print(f"Type: {s['type']:<12} | Range: {s['start']}:{s['end']} | Strength: {s['strength']}")

    # ASSERTIONS TO CHECK FOR DUPLICATION
    # A robust system should ideally only have 1 signal for the "---" span.
    # Currently, your code will likely return:
    # - Newline (0:1)
    # - Separator (1:4)
    # - Newline (4:5)
    
    # The stress test is this:
    dense_input = "\n\n---\n\n"
    dense_signals = extract_signals(dense_input)
    
    # If the engine counts 'newline' and 'empty_line' for the same indices, 
    # the count will be inflated.
    newline_signals = [s for s in dense_signals if s['type'] == 'newline']
    empty_line_signals = [s for s in dense_signals if s['type'] == 'empty_line']
    
    print(f"\nTotal signals for 5 characters: {len(dense_signals)}")
    
    # If this test passes, it means you have redundant signals 
    # that the engine MUST resolve.
    assert len(dense_signals) > 3, "Redundant signals detected (Expected for current version)"

if __name__ == "__main__":
    # Manual run
    test_signal_overlapping_shadowing()
