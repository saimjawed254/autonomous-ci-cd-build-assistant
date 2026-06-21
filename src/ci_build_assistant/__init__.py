"""CI build assistant package."""

from .analysis import AnalysisResult, analyze_build_log
from .parser import BuildLog, read_build_log
from .schema import FailureDiagnosis, FailureType, FileChange
from .agent import run_agent_loop, generate_diff_preview

__all__ = [
	"AnalysisResult",
	"BuildLog",
	"FailureDiagnosis",
	"FailureType",
	"FileChange",
	"analyze_build_log",
	"generate_diff_preview",
	"read_build_log",
	"run_agent_loop",
]

