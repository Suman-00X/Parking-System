"""Stage 13: the checks run against a finished draft. Deterministic rules only; nothing here asks a model."""

from radreport.pipeline.stages.verify.rules import DETERMINISTIC_CHECKS, VerifyStage, run_checks

__all__ = ["DETERMINISTIC_CHECKS", "VerifyStage", "run_checks"]
