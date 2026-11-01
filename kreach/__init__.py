"""kreach -- Kconfig Reachability Checker.

Finds Kconfig configurations that Kconfig accepts but that fail to link,
because built-in code calls a function provided by code that is a module or
disabled.  See docs/design.md for the stage breakdown (S1..S7).
"""

__version__ = "0.1.0"

STAGES = ("s1", "s2", "s3", "s4", "s5", "s6", "s7")
