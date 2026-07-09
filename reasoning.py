"""Compatibility wrapper for the current score-based heat-risk pipeline.

Earlier versions of the project used OWL/HermiT reasoning to infer
``uhi:VulnerableBuilding`` from a cardinality restriction over risk factors.
The current project uses computed ``HeatRiskAssessment`` instances instead.

Run this file when older documentation or scripts refer to ``reasoning.py``;
it delegates to ``risk_assessment.py``.
"""

from risk_assessment import main


if __name__ == "__main__":
    main()
