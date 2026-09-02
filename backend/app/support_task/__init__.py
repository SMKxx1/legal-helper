"""support_task — backend document-generation plane.

Parallel to the Review (``/v1``) and CLM (``/api/clm``) areas. Owns NDA *generation*:
filling a tokenised template ``.docx`` with a token→value table and returning the
filled document. n8n stays the frontend (Slack/Tally) and delegates the actual
docx token-fill here, so all template/document data + logic live in one place
(see models_v2 template/template_version/token/document_blob).
"""

from .generator import fill_docx, normalize_codes, resolve_template_docx

__all__ = ["fill_docx", "normalize_codes", "resolve_template_docx"]
