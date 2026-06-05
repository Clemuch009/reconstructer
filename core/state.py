# core/state.py



from typing import Dict, Any, List, Optional





class EngineState:

    """

    Runtime container for a single text reconstruction pass.



    Enforces:

    - ordered pipeline execution

    - no silent overwrites

    - explicit stage transitions

    """



    def __init__(self, raw_text: str):

        if not isinstance(raw_text, str):

            raise TypeError("raw_text must be a string")



        self.raw_text: str = raw_text



        # pipeline stages

        self.normalized_text: Optional[str] = None

        self.cleaned_text: Optional[str] = None



        self.signals: Optional[Dict[str, Any]] = None

        self.metrics: Optional[Dict[str, float]] = None

        self.features: Optional[Dict[str, float]] = None



        self.split_positions: Optional[List[int]] = None

        self.segments: Optional[List[str]] = None



    # ---------------------------------

    # STAGE SETTERS (STRICT ORDERING)

    # ---------------------------------



    def set_normalized(self, text: str) -> None:

        if self.normalized_text is not None:

            raise RuntimeError("normalized_text already set")



        self.normalized_text = text



    def set_cleaned(self, text: str) -> None:

        if self.cleaned_text is not None:

            raise RuntimeError("cleaned_text already set")



        # enforce ordering

        if self.normalized_text is None:

            raise RuntimeError("normalized_text must be set before cleaned_text")



        self.cleaned_text = text



    def set_signals(self, signals: Dict[str, Any]) -> None:

        if self.signals is not None:

            raise RuntimeError("signals already set")



        # must have text ready

        if self.get_active_text() is None:

            raise RuntimeError("no text available for signal extraction")



        self.signals = signals



    def set_metrics(self, metrics: Dict[str, float]) -> None:

        if self.metrics is not None:

            raise RuntimeError("metrics already set")



        if self.signals is None:

            raise RuntimeError("signals must be set before metrics")



        self.metrics = metrics



    def set_features(self, features: Dict[str, float]) -> None:

        if self.features is not None:

            raise RuntimeError("features already set")



        if self.metrics is None:

            raise RuntimeError("metrics must be set before features")



        self.features = features



    def set_splits(self, splits: List[int]) -> None:

        if self.split_positions is not None:

            raise RuntimeError("split_positions already set")



        if self.features is None:

            raise RuntimeError("features must be set before splits")



        self.split_positions = splits



    def set_segments(self, segments: List[str]) -> None:

        if self.segments is not None:

            raise RuntimeError("segments already set")



        if self.split_positions is None:

            raise RuntimeError("split_positions must be set before segments")



        self.segments = segments



    # ---------------------------------

    # STAGE ACCESS

    # ---------------------------------



    def get_active_text(self) -> str:

        """

        Returns the latest available text version.

        Priority:

            cleaned → normalized → raw

        """

        if self.cleaned_text is not None:

            return self.cleaned_text

        if self.normalized_text is not None:

            return self.normalized_text

        return self.raw_text



    def require_signals(self) -> Dict[str, Any]:

        if self.signals is None:

            raise RuntimeError("signals not set")

        return self.signals



    def require_metrics(self) -> Dict[str, float]:

        if self.metrics is None:

            raise RuntimeError("metrics not set")

        return self.metrics



    def require_features(self) -> Dict[str, float]:

        if self.features is None:

            raise RuntimeError("features not set")

        return self.features



    def require_splits(self) -> List[int]:

        if self.split_positions is None:

            raise RuntimeError("split_positions not set")

        return self.split_positions



    def require_segments(self) -> List[str]:

        if self.segments is None:

            raise RuntimeError("segments not set")

        return self.segments



    # ---------------------------------

    # DEBUG VIEW

    # ---------------------------------



    def summary(self) -> Dict[str, Any]:

        return {

            "has_normalized": self.normalized_text is not None,

            "has_cleaned": self.cleaned_text is not None,

            "has_signals": self.signals is not None,

            "has_metrics": self.metrics is not None,

            "has_features": self.features is not None,

            "has_splits": self.split_positions is not None,

            "has_segments": self.segments is not None,

        }





# ---------------------------------

# INTERACTIVE TEST

# ---------------------------------



if __name__ == "__main__":

    print("\n" + "=" * 60)

    print("ENGINE STATE TEST (FINAL)")

    print("=" * 60)



    state = EngineState("Raw input text")



    print("Initial:", state.summary())



    state.set_normalized("Normalized text")

    print("After normalize:", state.summary())



    state.set_cleaned("Cleaned text")

    print("After clean:", state.summary())



    state.set_signals({"mock": True})

    print("After signals:", state.summary())



    state.set_metrics({"boundary_pressure": 0.4})

    print("After metrics:", state.summary())



    state.set_features({"chaos_index": 0.2})

    print("After features:", state.summary())



    state.set_splits([10, 20])

    print("After splits:", state.summary())



    state.set_segments(["Segment 1", "Segment 2"])

    print("After segments:", state.summary())



    print("-" * 60)
