"""junitforge - standalone, coverage-driven JUnit 5 test generator (IBM watsonx).

A self-contained v2 of the agentic JUnit generator. The LLM writes tests;
deterministic tooling (javac compile gate + JaCoCo coverage) decides what
compiles and what is actually covered, and feeds exact uncovered lines/branches
back to the model. Nothing here imports from sibling packages - every needed
dependency is vendored under ``junitforge.vendor``.
"""

__version__ = "0.1.0"
