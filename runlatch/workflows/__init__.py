"""Built-in developer workflows for runlatch."""

from runlatch.workflows.ci_triage import (
    build_ci_triage_workflow,
    ci_triage_demo_responses,
)
from runlatch.workflows.dep_audit import (
    build_dep_audit_workflow,
    dep_audit_demo_responses,
)
from runlatch.workflows.doc_sync import (
    build_doc_sync_workflow,
    doc_sync_demo_responses,
)
from runlatch.workflows.issue_to_pr import (
    build_issue_to_pr_workflow,
    issue_to_pr_demo_responses,
)
from runlatch.workflows.pr_review import (
    build_pr_review_workflow,
    pr_review_demo_responses,
)

__all__ = [
    "build_ci_triage_workflow",
    "build_dep_audit_workflow",
    "build_doc_sync_workflow",
    "build_issue_to_pr_workflow",
    "build_pr_review_workflow",
    "ci_triage_demo_responses",
    "dep_audit_demo_responses",
    "doc_sync_demo_responses",
    "issue_to_pr_demo_responses",
    "pr_review_demo_responses",
]
