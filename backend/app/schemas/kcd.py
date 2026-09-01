from pydantic import BaseModel, Field


class KcdCodeItem(BaseModel):
    code: str
    name: str
    name_en: str | None = None


class KcdSearchResponse(BaseModel):
    items: list[KcdCodeItem]
    total: int
    query: str
    limit: int = Field(ge=1, le=50)
