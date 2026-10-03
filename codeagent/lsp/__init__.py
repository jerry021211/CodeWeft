"""Workspace-owned language server support; servers are never auto-installed."""

from codeagent.lsp.service import LspService
from codeagent.lsp.registry import LspConfig, ServerDefinition

__all__ = ['LspService', 'LspConfig', 'ServerDefinition']
