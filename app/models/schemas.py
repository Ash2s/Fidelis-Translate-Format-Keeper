from pydantic import BaseModel
from typing import Optional

class GlossaryUploadResponse(BaseModel):
    glossary_id: str
    term_count: int
    filename: str

class CustomAPIConfig(BaseModel):
    """User-provided API credentials for translation."""
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"

class TranslateRequest(BaseModel):
    file_ids: list[str]
    glossary_id: Optional[str] = None
    custom_api: Optional[CustomAPIConfig] = None

class JobResponse(BaseModel):
    job_id: str
    status: str

class RevisionRequest(BaseModel):
    job_id: str
    feedback: str
    custom_api: CustomAPIConfig | None = None
