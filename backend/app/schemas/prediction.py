from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PredictionCreate(BaseModel):
    risk_score: float
    risk_level: str
    prediction_horizon: int | None = None
    risk_factors: str | None = None


class PredictionResponse(BaseModel):
    id: int
    visit_id: int
    predicted_at: datetime
    risk_score: float
    risk_level: str
    prediction_horizon: int | None
    risk_factors: str | None

    model_config = ConfigDict(from_attributes=True)